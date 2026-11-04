#!/usr/bin/env python3
"""Importa os modelos públicos da Mya Prata para o catálogo de encomendas Myranda.

Não solicita páginas privadas, não faz login e não captura preços.
A importação pública é executada a pedido da proprietária, que afirma ter
permissão da fornecedora para usar as fotos na revenda. Se a Mya alterar
seu site ou restringir o acesso, a importação falha sem sobrescrever o catálogo.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.myaprataatacado.com"
OUT = Path(__file__).resolve().parents[1] / "produtos.json"
TIMEOUT = 25
RETRIES = 3
MAX_PAGES = int(os.environ.get("MYA_MAX_PAGINAS", "70"))
MIN_PRODUCTS = int(os.environ.get("MYA_MIN_PRODUTOS", "100"))
IMAGE_HOSTS = {"imagedelivery.net", "www.myaprataatacado.com", "myaprataatacado.com"}


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MyrandaCatalogo/1.0; retail catalog)", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6", "Accept": "text/html,application/xhtml+xml"})
    retry = Retry(total=RETRIES, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_html(s, url):
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    if "text/html" not in r.headers.get("Content-Type", "text/html"):
        raise ValueError(f"Resposta nao e HTML: {url}")
    return r.text


def sku_from_href(url):
    if not url:
        return None
    full = urljoin(BASE, url)
    if urlparse(full).netloc.lower() != urlparse(BASE).netloc:
        return None
    qs = parse_qs(urlparse(full).query)
    sku = (qs.get("sku") or [""])[0].strip().upper()
    if "/produtos/ver" in urlparse(full).path and re.fullmatch(r"[A-Z0-9._-]{2,40}", sku):
        return sku
    return None


def picture_urls(tag):
    if not tag:
        return []
    urls = []
    for img in tag.find_all("img"):
        for attr in ("data-src", "data-original", "data-lazy", "src", "data-srcset", "srcset"):
            raw = img.get(attr)
            if not raw:
                continue
            for value in str(raw).split(","):
                candidate = value.strip().split(" ")[0]
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                else:
                    candidate = urljoin(BASE, candidate)
                parsed = urlparse(candidate)
                if parsed.scheme == "https" and parsed.netloc.lower() in IMAGE_HOSTS and ("/mya/produto/" in candidate or "produt" in candidate.lower()):
                    urls.append(candidate)
    return list(dict.fromkeys(urls))


def get_card_image(anchor):
    # Primeiro imagem no próprio link; depois o menor ancestral que contenha um
    # único produto e uma foto. Evita escolher a foto de um produto vizinho.
    own = picture_urls(anchor)
    if own:
        return own[0]
    node = anchor.parent
    for _ in range(7):
        if node is None:
            break
        links = node.find_all("a", href=True)
        skus = {sku_from_href(x.get("href")) for x in links}
        skus.discard(None)
        images = picture_urls(node)
        if len(skus) == 1 and images:
            return images[0]
        node = node.parent
    return None


def best_name(anchor):
    opts = [anchor.get_text(" ", strip=True), anchor.get("title", ""), anchor.get("aria-label", "")]
    for img in anchor.find_all("img"):
        opts.append(img.get("alt", ""))
    for txt in opts:
        txt = " ".join(str(txt or "").split())
        if len(txt) >= 6 and txt.lower() not in ("imagem", "ver detalhes", "entre para ver o preço", "comprar", "adicionar ao carrinho") and not txt.lower().startswith("entre para ver"):
            return txt[:180]
    return ""


def category(name):
    n = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode("ascii")
    if n.startswith(("argola", "click ")):
        return "Argolas"
    if n.startswith(("trio", "kit brincos", "kit de brincos")):
        return "Trios"
    if n.startswith(("brinco", "piercing", "ear cuff", "earcuff")):
        return "Brincos"
    if n.startswith(("anel", "alianca")):
        return "Anéis"
    if n.startswith(("pulseira", "bracelete")):
        return "Pulseiras"
    if n.startswith(("colar", "gargantilha", "choker", "corrente")):
        return "Colares"
    if n.startswith("escapulario"):
        return "Escapulários"
    if n.startswith("tornozeleira"):
        return "Tornozeleiras"
    if n.startswith("berloque"):
        return "Berloques"
    if n.startswith("pingente"):
        return "Pingentes"
    if n.startswith("conjunto"):
        return "Conjuntos"
    return "Outros"


def extract_listing(source):
    soup = BeautifulSoup(source, "html.parser")
    items = {}
    for a in soup.find_all("a", href=True):
        sku = sku_from_href(a["href"])
        if not sku:
            continue
        p = items.setdefault(sku, {"sku": sku, "nome": "", "imagem": ""})
        name = best_name(a)
        if len(name) > len(p["nome"]):
            p["nome"] = name
        if not p["imagem"]:
            p["imagem"] = get_card_image(a) or ""
    return items


def extract_detail(s, sku):
    url = f"{BASE}/produtos/ver?sku={sku}"
    source = get_html(s, url)
    soup = BeautifulSoup(source, "html.parser")
    name = ""
    for sel in ("h1", "[itemprop='name']", "meta[property='og:title']"):
        el = soup.select_one(sel)
        if el:
            name = el.get("content", "") if el.name == "meta" else el.get_text(" ", strip=True)
            if name:
                break
    image = ""
    for m in soup.select("meta[property='og:image'], meta[name='twitter:image']"):
        cand = (m.get("content") or "").strip()
        if "/mya/produto/" in cand:
            image = cand
            break
    if not image:
        # Fotos públicas com padrão conhecido da CDN da Mya.
        candidates = re.findall(r"https?://imagedelivery\.net/[A-Za-z0-9_-]+/mya/produto/\d+/[A-Za-z0-9_-]+", source.replace("\\/", "/"))
        image = next(iter(candidates), "")
    if not image:
        pics = picture_urls(soup)
        image = pics[0] if pics else ""
    return sku, name, image


def first_page_count(source):
    pages = [int(n) for n in re.findall(r"[?&]pagina=(\d+)", source)]
    if pages:
        # O último link de paginação costuma apontar para a última página.
        return min(MAX_PAGES, max(pages))
    return MAX_PAGES


def main():
    s = session()
    all_items = {}
    missing_pages = []
    page_1 = get_html(s, f"{BASE}/produtos/")
    count = first_page_count(page_1)
    print(f"Paginas detectadas: {count}", flush=True)
    for page in range(1, count + 1):
        try:
            text = page_1 if page == 1 else get_html(s, f"{BASE}/produtos/?pagina={page}")
            found = extract_listing(text)
            if not found:
                raise ValueError("Nenhum codigo de produto identificado")
            for sku, product in found.items():
                if sku not in all_items:
                    all_items[sku] = product
                else:
                    item = all_items[sku]
                    if not item["nome"] and product["nome"]:
                        item["nome"] = product["nome"]
                    if not item["imagem"] and product["imagem"]:
                        item["imagem"] = product["imagem"]
            print(f"Pagina {page}/{count}: {len(found)} produtos; unicos {len(all_items)}", flush=True)
        except Exception as exc:
            missing_pages.append(page)
            print(f"ERRO pagina {page}: {exc}", file=sys.stderr, flush=True)
        time.sleep(0.15)
    if missing_pages:
        raise RuntimeError(f"Paginas nao importadas: {missing_pages}. Arquivo antigo preservado.")
    if len(all_items) < MIN_PRODUCTS:
        raise RuntimeError(f"Somente {len(all_items)} itens detectados (minimo {MIN_PRODUCTS}). Nao substitui produtos.json.")
    missing = [sku for sku, row in all_items.items() if not row["imagem"] or not row["nome"]]
    print(f"Consultando {len(missing)} fichas para completar fotos e nomes ...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(extract_detail, session(), sku): sku for sku in missing}
        for fut in as_completed(futures):
            sku = futures[fut]
            try:
                _, name, image = fut.result()
                item = all_items[sku]
                if not item["nome"]:
                    item["nome"] = name
                if not item["imagem"]:
                    item["imagem"] = image
            except Exception as exc:
                print(f"Aviso: ficha {sku}: {exc}", file=sys.stderr)
    rows = []
    for sku, p in all_items.items():
        name = p["nome"].strip()
        pic = p["imagem"].strip()
        if not name:
            continue
        rows.append({"sku": sku, "nome": name, "categoria": category(name), "imagem": pic,
                     "descricao": "Prata 925. Consulte tamanho, disponibilidade e prazo de encomenda.",
                     "destaque": False})
    rows.sort(key=lambda p: (p["categoria"], p["nome"].lower(), p["sku"]))
    good = sum(bool(p["imagem"]) for p in rows)
    ratio = good / max(1, len(rows))
    print(f"Resumo: {len(rows)} modelos, {good} imagens ({ratio:.1%})", flush=True)
    if len(rows) < MIN_PRODUCTS or ratio < 0.70:
        raise RuntimeError(f"Qualidade insuficiente: {len(rows)} modelos, {ratio:.0%} das imagens. Arquivo antigo preservado.")
    # Não há preços no arquivo público da Myranda.
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SUCESSO: {OUT} atualizado. Valores de atacado nao importados.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FALHA: {error}", file=sys.stderr)
        sys.exit(1)

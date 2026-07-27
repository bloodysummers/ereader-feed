"""Descubre, valida y administra las fuentes de feeds.txt.

    python3 feeds.py add astralcodexten          # handle de Substack
    python3 feeds.py add noahpinion.blog         # dominio propio
    python3 feeds.py add https://sitio.com/rss   # URL directa
    python3 feeds.py add --lang es sitio.com      # prefiere traducción al español
    python3 feeds.py check                       # revalida todo feeds.txt
    python3 feeds.py list

Valida antes de agregar. Además avisa si los artículos del feed apuntan a un
dominio distinto al de la fuente: hay feeds viejos de Feedburner abandonados
que hoy sirven contenido de otro sitio.
"""

import os
import re
import sys
from urllib.parse import urljoin, urlparse

import feedparser
import requests

from sources import (FULL_TEXT_THRESHOLD, HTTP_TIMEOUT, USER_AGENT,
                     parse_feed_line, resolve_feed_url)

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(HERE, "feeds.txt")

# Rutas donde suele vivir un feed cuando el sitio no lo declara.
COMMON_PATHS = (
    "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml",
    "/feed/", "/blog/feed",
)


def _get(url):
    return requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT, allow_redirects=True
    )


def _base_domain(url_or_host):
    host = urlparse(url_or_host).netloc or url_or_host
    host = host.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def declared_feeds(page_url):
    """Feeds que la página declara en <link rel="alternate">."""
    try:
        response = _get(page_url)
        response.raise_for_status()
    except Exception:
        return []
    found = []
    for tag in re.findall(r"<link[^>]+>", response.text, re.I):
        if not re.search(r'rel=["\']?alternate', tag, re.I):
            continue
        if not re.search(r"application/(rss|atom)\+xml", tag, re.I):
            continue
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if href:
            found.append(urljoin(page_url, href.group(1)))
    return found


def inspect_feed(feed_url, origin=None):
    """Descarga un feed y reporta si sirve y si trae texto completo.

    Devuelve dict con: ok, title, items, full_text, avg_chars, sample_link,
    mismatch (el feed apunta a otro dominio) y error.
    """
    result = {
        "url": feed_url, "ok": False, "title": None, "items": 0,
        "full_text": False, "avg_chars": 0, "sample_link": None,
        "mismatch": None, "error": None,
    }
    try:
        response = _get(feed_url)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if not parsed.entries:
        result["error"] = str(parsed.get("bozo_exception", "sin artículos"))
        return result

    lengths = []
    for entry in parsed.entries[:5]:
        if entry.get("content"):
            body = max((c.get("value", "") for c in entry["content"]), key=len)
        else:
            body = entry.get("summary", "") or ""
        lengths.append(len(re.sub(r"<[^>]+>", "", body)))

    result["ok"] = True
    result["title"] = parsed.feed.get("title", "(sin título)")
    result["items"] = len(parsed.entries)
    result["avg_chars"] = sum(lengths) // len(lengths) if lengths else 0
    result["full_text"] = result["avg_chars"] >= FULL_TEXT_THRESHOLD
    result["sample_link"] = parsed.entries[0].get("link")

    # Solo tiene sentido comparar dominios cuando el usuario pidió un sitio
    # concreto. Un feed alojado en substack.com cuyos artículos apuntan a un
    # dominio propio es normal (Substack con dominio personalizado), no un
    # secuestro; sin un origen esperado no hay nada que contrastar.
    if origin and result["sample_link"]:
        expected, actual = _base_domain(origin), _base_domain(result["sample_link"])
        if expected and actual and expected != actual:
            result["mismatch"] = f"esperaba {expected}, los artículos van a {actual}"
    return result


def discover(target):
    """Encuentra un feed usable a partir de un handle, dominio o URL.

    Devuelve (feed_url, informe) o (None, lista_de_intentos).
    """
    candidates = []
    resolved = resolve_feed_url(target)
    candidates.append(resolved)

    # Si venía un dominio o URL de sitio, preguntar al HTML y probar rutas.
    if not target.startswith("substack:") and "." in target:
        site = target if target.startswith(("http://", "https://")) else f"https://{target}"
        site = site.rstrip("/")
        candidates.extend(declared_feeds(site))
        candidates.extend(site + path for path in COMMON_PATHS)

        # Los Substack con dominio propio a veces devuelven 404 en /feed pero
        # siguen sirviendo el feed en su subdominio original. Se prueba al
        # final: si el subdominio adivinado fuera de otro autor, el chequeo de
        # dominio lo descarta porque sus enlaces apuntarían a otro sitio.
        if not urlparse(site).path.strip("/"):
            label = _base_domain(site).split(".")[0]
            candidates.append(f"https://{label}.substack.com/feed")

    seen, attempts = set(), []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        report = inspect_feed(candidate, origin=target if "." in target else None)
        attempts.append(report)
        if report["ok"] and not report["mismatch"]:
            return candidate, report
    return None, attempts


def _describe(report):
    if not report["ok"]:
        return f"falla: {report['error'][:70]}"
    kind = "texto completo" if report["full_text"] else "solo resumen (se hará scraping)"
    # El dominio destino se muestra siempre: es la forma de notar a simple
    # vista un feed abandonado que hoy sirve contenido de otro sitio.
    target = _base_domain(report["sample_link"]) if report["sample_link"] else "?"
    note = f"\n      ¡OJO: {report['mismatch']}!" if report["mismatch"] else ""
    return (
        f"{report['title']} · {report['items']} items · ~{report['avg_chars']:,} chars"
        f" · {kind} · artículos en {target}{note}"
    )


def cmd_add(targets, lang=None):
    existing = set()
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, encoding="utf-8") as fh:
            existing = {
                parse_feed_line(line).url
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            }

    to_append, failures = [], []
    for target in targets:
        print(f"\n{target}")
        feed_url, outcome = discover(target)
        if not feed_url:
            print("  no encontré un feed usable. Intentos:")
            for attempt in outcome:
                print(f"    - {attempt['url']}\n        {_describe(attempt)}")
            failures.append(target)
            continue

        print(f"  feed: {feed_url}")
        print(f"  {_describe(outcome)}")
        if resolve_feed_url(target) in existing or feed_url in existing:
            print("  ya estaba en feeds.txt, no lo duplico.")
            continue
        to_append.append((target, feed_url, outcome["title"]))

    if to_append:
        with open(FEEDS_FILE, "a", encoding="utf-8") as fh:
            for target, feed_url, title in to_append:
                entry = target if resolve_feed_url(target) == feed_url else feed_url
                if lang:
                    entry += f" | lang={lang}"
                fh.write(f"\n# {title}\n{entry}\n")
        print(f"\nAgregadas {len(to_append)} fuentes a feeds.txt")

    return 1 if failures else 0


def cmd_check():
    from sources import read_feed_list

    specs = read_feed_list(FEEDS_FILE)
    if not specs:
        print("feeds.txt está vacío.")
        return 0
    problems = 0
    for spec in specs:
        url = spec.url
        report = inspect_feed(url)
        flag = "ok  " if report["ok"] and not report["mismatch"] else "MAL "
        if flag == "MAL ":
            problems += 1
        lang = f"  [traduce a {spec.lang}]" if spec.lang else ""
        print(f"{flag}{url}{lang}\n      {_describe(report)}")
    print(f"\n{len(specs)} fuentes, {problems} con problemas.")
    return 1 if problems else 0


def cmd_list():
    from sources import read_feed_list

    for spec in read_feed_list(FEEDS_FILE):
        print(spec)
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "add":
        lang = None
        if "--lang" in rest:
            i = rest.index("--lang")
            if i + 1 >= len(rest):
                print("--lang necesita un código de idioma, p.ej. --lang es")
                return 1
            lang = rest[i + 1]
            rest = rest[:i] + rest[i + 2 :]
        if not rest:
            print("uso: python3 feeds.py add [--lang es] <handle|dominio|url> [...]")
            return 1
        return cmd_add(rest, lang)
    if command == "check":
        return cmd_check()
    if command == "list":
        return cmd_list()
    print(f"comando desconocido: {command}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

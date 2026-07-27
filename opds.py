"""Catálogo OPDS 1.2 para que el e-reader descargue por su cuenta.

OPDS es un feed Atom donde cada entrada apunta al archivo con un enlace
`rel="http://opds-spec.org/acquisition"`. El lector navega el catálogo y baja
lo que quiera, así que no hace falta agente local ni almacenamiento: los EPUB
se generan al vuelo cuando el dispositivo pide la URL de descarga.

Estructura:

    /opds                      catálogo raíz (navegación)
      /opds/queued             lo apartado, empaquetado en un solo EPUB
      /opds/unread             artículos sin leer, uno por entrada
      /opds/source/{id}        artículos de una publicación

    /opds/epub/queued.epub     genera el digest de lo apartado
    /opds/epub/article/{id}.epub   genera el EPUB de un artículo

Autenticación HTTP Basic, que es lo que el firmware ofrece (usuario y
contraseña en su configuración). Cualquier usuario sirve; la contraseña debe
ser APP_TOKEN.
"""

import datetime as dt
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import store

router = APIRouter(prefix="/opds")

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB = "application/epub+zip"


def _rfc3339(value=None):
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base(request):
    """URL absoluta del sitio. Las relativas confunden a algunos lectores."""
    return str(request.base_url).rstrip("/")


def _feed(title, feed_id, base, self_href, entries, kind=NAV):
    """Atom deliberadamente mínimo: un solo namespace y etiquetas en una línea.

    El parser del firmware es embebido y tiene poco margen, así que se evita
    todo lo prescindible (namespaces extra, atributos partidos en varias
    líneas) aunque sea XML perfectamente válido.
    """
    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{escape(feed_id)}</id>
  <title>{escape(title)}</title>
  <updated>{_rfc3339()}</updated>
  <author><name>ereader-feed</name></author>
  <link rel="self" href="{base}{self_href}" type="{kind}"/>
  <link rel="start" href="{base}/opds" type="{NAV}"/>
{body}
</feed>
"""


def _xml(content, kind=NAV):
    # El Content-Type debe declarar el perfil OPDS: varios clientes lo validan
    # y descartan el feed si solo dice application/atom+xml.
    return Response(content, media_type=f"{kind};charset=utf-8")


def _nav_entry(base, title, href, summary, kind=ACQ):
    return f"""  <entry>
    <id>{base}{href}</id>
    <title>{escape(title)}</title>
    <updated>{_rfc3339()}</updated>
    <content type="text">{escape(summary)}</content>
    <link rel="subsection" href="{base}{href}" type="{kind}"/>
  </entry>"""


def _article_entry(base, row):
    minutos = round((row["chars"] or 0) / 1100) or 1
    resumen = f"{row['source'] or ''} - {minutos} min de lectura"
    fecha = _rfc3339(row["published"] or row["fetched_at"])
    href = f"/opds/epub/article/{row['id']}.epub"
    return f"""  <entry>
    <id>urn:ereader-feed:article:{row['id']}</id>
    <title>{escape(row['title'] or 'Sin título')}</title>
    <updated>{fecha}</updated>
    <author><name>{escape(row['source'] or 'Desconocido')}</name></author>
    <summary type="text">{escape(resumen)}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{base}{href}" type="{EPUB}"/>
  </entry>"""


@router.get("")
@router.get("/")
def root(request: Request):
    base = _base(request)
    counts = store.counts()
    entries = [
        _nav_entry(base, "Apartados", "/opds/queued",
                   f"{counts['queued']} artículos en un solo volumen"),
        _nav_entry(base, "Sin leer", "/opds/unread",
                   f"{counts['unread']} artículos, por separado"),
    ]
    for feed in store.list_feeds():
        pendientes = len(store.list_articles(status="unread", feed_id=feed["id"],
                                             limit=500))
        if pendientes:
            entries.append(_nav_entry(
                base, feed["title"] or feed["url"],
                f"/opds/source/{feed['id']}", f"{pendientes} sin leer",
            ))
    return _xml(_feed("La Gaceta", f"{base}/opds", base, "/opds", entries))


@router.get("/queued")
def queued(request: Request):
    """Un único volumen con todo lo apartado: menos descargas en el reader."""
    base = _base(request)
    rows = store.list_articles(status="queued", limit=500)
    entries = []
    if rows:
        entries.append(f"""  <entry>
    <id>urn:ereader-feed:queued:{len(rows)}</id>
    <title>Digest - {len(rows)} articulos apartados</title>
    <updated>{_rfc3339()}</updated>
    <author><name>La Gaceta</name></author>
    <summary type="text">{escape(' - '.join(r['title'][:40] for r in rows[:4]))}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{base}/opds/epub/queued.epub" type="{EPUB}"/>
  </entry>""")
    return _xml(_feed("Apartados", f"{base}/opds/queued", base, "/opds/queued",
                      entries, kind=ACQ), kind=ACQ)


@router.get("/unread")
def unread(request: Request):
    base = _base(request)
    rows = store.list_articles(status="unread", limit=100)
    entries = [_article_entry(base, r) for r in rows]
    return _xml(_feed("Sin leer", f"{base}/opds/unread", base, "/opds/unread",
                      entries, kind=ACQ), kind=ACQ)


@router.get("/source/{feed_id}")
def by_source(request: Request, feed_id: int):
    base = _base(request)
    rows = store.list_articles(status="unread", feed_id=feed_id, limit=100)
    nombre = next(
        (f["title"] or f["url"] for f in store.list_feeds() if f["id"] == feed_id),
        "Publicación",
    )
    entries = [_article_entry(base, r) for r in rows]
    return _xml(_feed(nombre, f"{base}/opds/source/{feed_id}", base,
                      f"/opds/source/{feed_id}", entries, kind=ACQ), kind=ACQ)


# ----------------------------------------------------------- descargas


def _epub_response(data, filename):
    return Response(data, media_type=EPUB, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(data)),
    })


@router.get("/epub/article/{article_id}.epub")
def article_epub(article_id: int):
    from app import build_epub

    row = store.get_article(article_id)
    if not row:
        raise HTTPException(404)
    data = build_epub([row], row["title"] or f"Artículo {article_id}")
    store.mark_sent([article_id])
    return _epub_response(data, f"{article_id}.epub")


@router.get("/epub/queued.epub")
def queued_epub():
    from app import build_epub, digest_title

    rows = store.list_articles(status="queued", limit=500)
    if not rows:
        raise HTTPException(404, "no hay artículos apartados")
    full = store.get_articles([r["id"] for r in rows])
    title = digest_title()
    data = build_epub(full, title)
    # Descargarlo cuenta como enviado: ya está en el dispositivo.
    store.mark_sent([r["id"] for r in rows])
    return _epub_response(data, f"{title}.epub")

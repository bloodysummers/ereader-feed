"""Interfaz web del lector de feeds. Entrypoint ASGI para Vercel y para local.

    uvicorn app:app --reload          # local, en http://127.0.0.1:8000

Variables de entorno:

    DATABASE_URL   Postgres de Neon en producción; sin ella usa SQLite local.
    APP_TOKEN      Token compartido. Si no se define, la app queda abierta:
                   NUNCA la despliegues en Vercel sin definirlo.
    SEND_MODE      'direct' sube al reader desde el propio proceso (local);
                   'queue' deja el envío para el agente local (nube).
                   Por omisión: direct en local, queue en Vercel.
    CROSSPOINT_HOST  nombre mDNS del reader (solo en modo direct).
"""

import datetime as dt
import os
import tempfile

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import epubgen
import ingest
import opds
import sources
import store
from db import init_db

HERE = os.path.dirname(os.path.abspath(__file__))
APP_TOKEN = os.environ.get("APP_TOKEN", "").strip()
ON_VERCEL = bool(os.environ.get("VERCEL"))
SEND_MODE = os.environ.get("SEND_MODE", "queue" if ON_VERCEL else "direct")
SITE_NAME = os.environ.get("SITE_NAME", "La Gaceta")

templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


class AuthMiddleware(BaseHTTPMiddleware):
    """Puerta de token compartido.

    Tres formas de presentarlo, según quién llama:
      - cookie          navegador
      - Bearer          agente local
      - Basic           el e-reader, que es lo único que su firmware ofrece
                        (cualquier usuario; la contraseña debe ser APP_TOKEN)
    """

    OPEN_PATHS = ("/login", "/health", "/favicon.ico")

    @staticmethod
    def _from_basic(header):
        import base64

        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return None
        return raw.split(":", 1)[1] if ":" in raw else None

    async def dispatch(self, request, call_next):
        if not APP_TOKEN or request.url.path in self.OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        lower = header.lower()
        if lower.startswith("bearer "):
            supplied = header[7:].strip()
        elif lower.startswith("basic "):
            supplied = self._from_basic(header)
        else:
            supplied = request.cookies.get("auth")

        if supplied == APP_TOKEN:
            return await call_next(request)

        # El lector no sabe de formularios ni de JSON: necesita el reto Basic
        # para mandar las credenciales que tiene configuradas.
        if request.url.path.startswith("/opds"):
            return Response("no autorizado", 401, headers={
                "WWW-Authenticate": 'Basic realm="La Gaceta"'
            })
        if request.url.path.startswith("/api/"):
            return Response('{"error":"no autorizado"}', 401,
                            media_type="application/json")
        return RedirectResponse("/login", status_code=303)


class OpdsDebugMiddleware(BaseHTTPMiddleware):
    """Traza las peticiones OPDS con su origen y cliente.

    Sirve para distinguir al e-reader de las pruebas hechas con curl, que fue
    justo lo que confundió el diagnóstico la primera vez. Se activa con
    OPDS_DEBUG=1.
    """

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/opds"):
            return await call_next(request)
        response = await call_next(request)
        client = request.client.host if request.client else "?"
        agent = request.headers.get("user-agent", "(sin UA)")
        auth = "sí" if request.headers.get("authorization") else "NO"
        print(
            f"[OPDS] {client}  {request.method} {request.url.path}"
            f"  -> {response.status_code}  auth={auth}  UA={agent[:60]}",
            flush=True,
        )
        return response


app = FastAPI(title="ereader-feed")
app.add_middleware(AuthMiddleware)
if os.environ.get("OPDS_DEBUG"):
    app.add_middleware(OpdsDebugMiddleware)
app.include_router(opds.router)


@app.on_event("startup")
def startup():
    init_db()


# ------------------------------------------------------------- utilidades


def to_article(row):
    """Convierte una fila de la base en el Article que espera epubgen."""
    return sources.Article(
        title=row["title"] or "Sin título",
        source=row["source"] or "",
        url=row["url"] or "",
        published=row["published"],
        html=row["html"] or "",
        uid=row["uid"],
    )


def build_epub(article_rows, title):
    """Arma el EPUB en /tmp (lo único escribible en Vercel) y devuelve bytes."""
    ordered = sorted(article_rows, key=lambda r: (
        r["source"] or "",
        -(r["published"].timestamp() if r["published"] else 0),
    ))
    articles = [to_article(r) for r in ordered]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "digest.epub")
        epubgen.build_digest(articles, title, path, group_by_source=True)
        with open(path, "rb") as fh:
            return fh.read()


def digest_title():
    """Título único por envío: dos envíos el mismo día no deben pisarse."""
    base = f"Digest {dt.date.today().isoformat()}"
    existing = {job["title"] for job in store.recent_jobs(50)}
    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def deliver_now(job):
    """Modo direct: sube el EPUB al reader desde este mismo proceso."""
    from crosspoint import DEFAULT_REMOTE_DIR, CrossPoint

    rows = store.get_articles(job["article_ids"])
    data = build_epub(rows, job["title"])
    client = CrossPoint(os.environ.get("CROSSPOINT_HOST") or "crosspoint.local")
    remote_dir = os.environ.get("CROSSPOINT_DIR", DEFAULT_REMOTE_DIR)
    client.ensure_dir(remote_dir)
    name = client.upload_bytes(data, f"{job['title']}.epub", remote_dir)
    store.finish_job(job["id"], ok=True, remote_name=f"{remote_dir}/{name}")
    return name


# ------------------------------------------------------------- navegador


@app.get("/health")
def health():
    return {"ok": True, "send_mode": SEND_MODE, "protegido": bool(APP_TOKEN)}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", cabecera())


@app.post("/login")
def login(token: str = Form(...)):
    if token != APP_TOKEN:
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "auth", token, httponly=True, max_age=60 * 60 * 24 * 365, samesite="lax"
    )
    return response


MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def fecha_larga(day=None):
    day = day or dt.date.today()
    return f"{DIAS[day.weekday()]} {day.day} de {MESES[day.month - 1]} de {day.year}"


def cabecera():
    """Contexto que la cabecera de periódico necesita en todas las vistas."""
    return {"site_name": SITE_NAME, "fecha": fecha_larga()}


def agrupar_por_fuente(rows, feeds_list):
    """Agrupa artículos en secciones, en el orden en que están las fuentes."""
    orden = {f["id"]: i for i, f in enumerate(feeds_list)}
    nombres = {f["id"]: (f["title"] or f["url"]) for f in feeds_list}
    grupos = {}
    for row in rows:
        grupos.setdefault(row["feed_id"], []).append(row)
    return [
        {
            "nombre": nombres.get(feed_id) or (items[0]["source"] or "Sin fuente"),
            "feed_id": feed_id,
            "articulos": items,
        }
        for feed_id, items in sorted(
            grupos.items(), key=lambda kv: orden.get(kv[0], 999)
        )
    ]


@app.get("/", response_class=HTMLResponse)
def index(request: Request, status: str = "unread", feed: int | None = None):
    feeds_list = store.list_feeds()
    rows = store.list_articles(status=status, feed_id=feed)
    return templates.TemplateResponse(request, "list.html", {
        "secciones": agrupar_por_fuente(rows, feeds_list),
        "total": len(rows),
        "counts": store.counts(),
        "status": status,
        "feeds": feeds_list,
        "feed_id": feed,
        "send_mode": SEND_MODE,
        "site_name": SITE_NAME,
        "fecha": fecha_larga(),
    })


@app.get("/article/{article_id}", response_class=HTMLResponse)
def read_article(request: Request, article_id: int):
    article = store.get_article(article_id)
    if not article:
        raise HTTPException(404)
    if not article["read_at"]:
        store.mark_read(article_id)  # solo este, nunca el lote
        article = store.get_article(article_id)
    return templates.TemplateResponse(request, "article.html", {
        "a": article, **cabecera(),
    })


@app.post("/article/{article_id}/unread")
def unread(article_id: int):
    store.mark_read(article_id, read=False)
    return RedirectResponse("/", status_code=303)


@app.post("/queue")
def queue(ids: list[int] = Form(default=[]), status: str = Form(default="unread")):
    store.set_queued(ids, True)
    return RedirectResponse(f"/?status={status}", status_code=303)


@app.post("/send")
def send(ids: list[int] = Form(default=[]), status: str = Form(default="unread")):
    """Crea el envío. En modo direct lo sube ya; en queue lo recoge el agente."""
    if not ids:
        return RedirectResponse(f"/?status={status}&error=vacio", status_code=303)

    store.set_queued(ids, True)
    job_id = store.create_job(digest_title(), ids)

    if SEND_MODE == "direct":
        job = store.get_job(job_id)
        try:
            deliver_now(job)
            return RedirectResponse(f"/?status={status}&ok=enviado", status_code=303)
        except Exception as exc:
            store.finish_job(job_id, ok=False, error=str(exc)[:500])
            return RedirectResponse(f"/?status={status}&error=envio", status_code=303)

    return RedirectResponse(f"/?status={status}&ok=encolado", status_code=303)


@app.post("/send-queued")
def send_queued():
    """Envía de una sola vez todo lo que fuiste apartando con «Encolar»."""
    ids = [row["id"] for row in store.list_articles(status="queued", limit=500)]
    return send(ids=ids, status="queued")


@app.get("/feeds", response_class=HTMLResponse)
def feeds_page(request: Request):
    return templates.TemplateResponse(request, "feeds.html", {
        "feeds": store.list_feeds(), **cabecera(),
    })


@app.post("/feeds/add")
def feeds_add(target: str = Form(...), lang: str = Form(default="")):
    import feeds as feedtools

    url, report = feedtools.discover(target.strip())
    if not url:
        return RedirectResponse("/feeds?error=nofeed", status_code=303)
    store.add_feed(url, title=report.get("title"), lang=lang.strip() or None)
    return RedirectResponse("/feeds?ok=1", status_code=303)


@app.post("/feeds/{feed_id}/delete")
def feeds_delete(feed_id: int):
    store.remove_feed(feed_id)
    return RedirectResponse("/feeds", status_code=303)


@app.post("/refresh")
def refresh(status: str = Form(default="unread")):
    summary = ingest.refresh_all()
    total = sum(item["new"] for item in summary)
    return RedirectResponse(f"/?status={status}&ok=nuevos-{total}", status_code=303)


# ------------------------------------------------- API para el agente local


@app.get("/api/jobs/pending")
def api_pending():
    job = store.pending_job()
    if not job:
        return {"job": None}
    return {"job": {"id": job["id"], "title": job["title"],
                    "articles": len(job["article_ids"])}}


@app.get("/api/jobs/{job_id}/epub")
def api_job_epub(job_id: int):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404)
    rows = store.get_articles(job["article_ids"])
    if not rows:
        raise HTTPException(409, "el envío no tiene artículos")
    data = build_epub(rows, job["title"])
    return Response(
        data,
        media_type="application/epub+zip",
        headers={
            "Content-Disposition": f'attachment; filename="{job["title"]}.epub"'
        },
    )


@app.post("/api/jobs/{job_id}/done")
def api_job_done(job_id: int, ok: bool = Form(default=True),
                 error: str = Form(default=""), remote_name: str = Form(default="")):
    job = store.finish_job(job_id, ok=ok, error=error or None,
                           remote_name=remote_name or None)
    if not job:
        raise HTTPException(404)
    return {"ok": True, "articulos_marcados": len(job["article_ids"]) if ok else 0}


@app.get("/api/refresh")
@app.post("/api/refresh")
def api_refresh():
    """Ingesta programada.

    Vercel invoca los cron con GET, así que se aceptan ambos verbos. La
    autenticación la resuelve el middleware: basta con poner CRON_SECRET al
    mismo valor que APP_TOKEN y el Bearer que manda Vercel encaja solo.
    """
    summary = ingest.refresh_all()
    return {"feeds": summary, "nuevos": sum(item["new"] for item in summary)}

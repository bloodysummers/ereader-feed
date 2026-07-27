"""Operaciones sobre la base. Toda la lógica de estados vive aquí.

Reglas que sostienen el modelo:

  - `upsert_article` nunca borra ni oculta: si el artículo ya existe conserva
    sus marcas (read_at, sent_at, queued) y solo refresca el contenido.
  - Consultar la lista no modifica nada.
  - Leer marca un artículo, no un lote.
  - Enviar al reader es una marca distinta de haber leído.
"""

import datetime as dt
import json

from sqlalchemy import delete, func, insert, select, update

from db import MAX_JOB_ATTEMPTS, articles, feeds, get_engine, jobs


def now():
    return dt.datetime.now(dt.timezone.utc)


def _aware(value):
    """SQLite devuelve datetimes sin zona; se asumen UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _row_to_dict(row):
    data = dict(row._mapping)
    for key in ("published", "fetched_at", "read_at", "sent_at", "created_at",
                "finished_at", "last_fetch_at"):
        if key in data:
            data[key] = _aware(data[key])
    return data


# --------------------------------------------------------------- feeds


def list_feeds(only_active=False):
    query = select(feeds).order_by(feeds.c.position, feeds.c.id)
    if only_active:
        query = query.where(feeds.c.active.is_(True))
    with get_engine().connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(query)]


def add_feed(url, title=None, lang=None, position=None):
    """Alta idempotente: si la URL ya existe, actualiza título e idioma."""
    with get_engine().begin() as conn:
        existing = conn.execute(
            select(feeds).where(feeds.c.url == url)
        ).first()
        if existing:
            conn.execute(
                update(feeds)
                .where(feeds.c.id == existing.id)
                .values(title=title or existing.title, lang=lang)
            )
            return existing.id
        if position is None:
            highest = conn.execute(select(func.max(feeds.c.position))).scalar()
            position = (highest or 0) + 1
        result = conn.execute(
            insert(feeds).values(
                url=url, title=title, lang=lang, position=position,
                active=True, created_at=now(),
            )
        )
        return result.inserted_primary_key[0]


def remove_feed(feed_id):
    with get_engine().begin() as conn:
        conn.execute(delete(articles).where(articles.c.feed_id == feed_id))
        conn.execute(delete(feeds).where(feeds.c.id == feed_id))


def record_fetch(feed_id, error=None, title=None):
    values = {"last_fetch_at": now(), "last_error": error}
    if title:
        values["title"] = title
    with get_engine().begin() as conn:
        conn.execute(update(feeds).where(feeds.c.id == feed_id).values(**values))


# ------------------------------------------------------------ artículos


def upsert_article(feed_id, article, lang=None):
    """Inserta o refresca un artículo SIN tocar sus marcas de estado.

    Devuelve (id, es_nuevo).
    """
    import re

    text_chars = len(re.sub(r"<[^>]+>", " ", article.html or ""))
    with get_engine().begin() as conn:
        existing = conn.execute(
            select(articles.c.id).where(articles.c.uid == article.uid)
        ).first()
        values = {
            "feed_id": feed_id,
            "source": article.source,
            "title": article.title,
            "url": article.url,
            "published": article.published,
            "html": article.html,
            "chars": text_chars,
            "lang": lang,
        }
        if existing:
            # read_at, sent_at y queued quedan intactos a propósito.
            conn.execute(
                update(articles).where(articles.c.id == existing.id).values(**values)
            )
            return existing.id, False
        values.update(uid=article.uid, fetched_at=now(), queued=False)
        result = conn.execute(insert(articles).values(**values))
        return result.inserted_primary_key[0], True


def known_uids(feed_id):
    """uids ya almacenados de un feed, para no reprocesarlos ni retraducirlos."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(articles.c.uid).where(articles.c.feed_id == feed_id)
        )
        return {r.uid for r in rows}


LIST_COLUMNS = (
    articles.c.id, articles.c.uid, articles.c.feed_id, articles.c.source,
    articles.c.title, articles.c.url, articles.c.published, articles.c.chars,
    articles.c.fetched_at, articles.c.read_at, articles.c.sent_at,
    articles.c.queued,
)


def list_articles(status="unread", feed_id=None, limit=200, offset=0):
    """Lista sin cargar el HTML. `status`: unread|unsent|queued|read|all."""
    query = select(*LIST_COLUMNS)
    if status == "unread":
        query = query.where(articles.c.read_at.is_(None))
    elif status == "unsent":
        query = query.where(articles.c.sent_at.is_(None))
    elif status == "queued":
        query = query.where(articles.c.queued.is_(True))
    elif status == "read":
        query = query.where(articles.c.read_at.is_not(None))
    if feed_id:
        query = query.where(articles.c.feed_id == feed_id)
    query = query.order_by(
        articles.c.published.desc().nullslast(), articles.c.id.desc()
    ).limit(limit).offset(offset)
    with get_engine().connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(query)]


def counts():
    """Totales para las pestañas de la interfaz."""
    with get_engine().connect() as conn:
        total = conn.execute(select(func.count()).select_from(articles)).scalar()
        unread = conn.execute(
            select(func.count()).select_from(articles).where(articles.c.read_at.is_(None))
        ).scalar()
        unsent = conn.execute(
            select(func.count()).select_from(articles).where(articles.c.sent_at.is_(None))
        ).scalar()
        queued = conn.execute(
            select(func.count()).select_from(articles).where(articles.c.queued.is_(True))
        ).scalar()
    return {"all": total, "unread": unread, "unsent": unsent, "queued": queued}


def get_article(article_id):
    with get_engine().connect() as conn:
        row = conn.execute(
            select(articles).where(articles.c.id == article_id)
        ).first()
        return _row_to_dict(row) if row else None


def get_articles(article_ids):
    """Artículos completos, en el orden pedido."""
    if not article_ids:
        return []
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(articles).where(articles.c.id.in_(article_ids))
        )
        by_id = {r.id: _row_to_dict(r) for r in rows}
    return [by_id[i] for i in article_ids if i in by_id]


def mark_read(article_id, read=True):
    with get_engine().begin() as conn:
        conn.execute(
            update(articles)
            .where(articles.c.id == article_id)
            .values(read_at=now() if read else None)
        )


def set_queued(article_ids, queued=True):
    if not article_ids:
        return 0
    with get_engine().begin() as conn:
        result = conn.execute(
            update(articles)
            .where(articles.c.id.in_(article_ids))
            .values(queued=queued)
        )
        return result.rowcount


def mark_sent(article_ids):
    """Marca enviados y los saca de la cola."""
    if not article_ids:
        return 0
    with get_engine().begin() as conn:
        result = conn.execute(
            update(articles)
            .where(articles.c.id.in_(article_ids))
            .values(sent_at=now(), queued=False)
        )
        return result.rowcount


# ---------------------------------------------------------------- jobs


def create_job(title, article_ids):
    with get_engine().begin() as conn:
        result = conn.execute(
            insert(jobs).values(
                title=title,
                article_ids=json.dumps(list(article_ids)),
                status="pending",
                created_at=now(),
            )
        )
        return result.inserted_primary_key[0]


def get_job(job_id):
    with get_engine().connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).first()
    if not row:
        return None
    job = _row_to_dict(row)
    job["article_ids"] = json.loads(job["article_ids"] or "[]")
    return job


def pending_job():
    """El envío pendiente más antiguo, que es lo que recoge el agente local."""
    with get_engine().connect() as conn:
        row = conn.execute(
            select(jobs)
            .where(jobs.c.status == "pending")
            .order_by(jobs.c.created_at, jobs.c.id)
            .limit(1)
        ).first()
    if not row:
        return None
    job = _row_to_dict(row)
    job["article_ids"] = json.loads(job["article_ids"] or "[]")
    return job


def finish_job(job_id, ok=True, error=None, remote_name=None):
    """Cierra un envío. Un fallo deja el envío pendiente para reintentarlo.

    Solo tras MAX_JOB_ATTEMPTS se da por perdido: así un reader dormido no
    obliga a reenviar a mano desde la interfaz.
    """
    job = get_job(job_id)
    if not job:
        return None

    if ok:
        with get_engine().begin() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == job_id).values(
                    status="done", finished_at=now(), error=None,
                    remote_name=remote_name,
                )
            )
        mark_sent(job["article_ids"])
        return job

    attempts = (job.get("attempts") or 0) + 1
    exhausted = attempts >= MAX_JOB_ATTEMPTS
    with get_engine().begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == job_id).values(
                status="error" if exhausted else "pending",
                attempts=attempts,
                finished_at=now() if exhausted else None,
                error=error,
            )
        )
    return job


def recent_jobs(limit=10):
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(jobs).order_by(jobs.c.id.desc()).limit(limit)
        )
        return [_row_to_dict(r) for r in rows]

"""Refresca las fuentes hacia la base. No marca ni oculta nada.

Es idempotente: correrlo dos veces seguidas no duplica artículos ni altera
lo que ya leíste o enviaste.
"""

import sources
import store


def refresh_feed(feed, since_days=7, max_items=25, strip_images=True):
    """Trae los artículos nuevos de un feed. Devuelve (nuevos, actualizados)."""
    known = store.known_uids(feed["id"])
    found = sources.fetch_feed(
        feed["url"],
        since_days=since_days,
        max_items=max_items,
        strip_images=strip_images,
        seen=known,  # evita volver a descargar y retraducir lo ya guardado
        prefer_lang=feed.get("lang"),
    )
    new_count = 0
    for article in found:
        _, is_new = store.upsert_article(feed["id"], article, lang=feed.get("lang"))
        new_count += 1 if is_new else 0

    title = found[0].source if found else None
    store.record_fetch(feed["id"], error=None, title=title)
    return new_count, len(found) - new_count


def refresh_all(since_days=7, max_items=25, strip_images=True, progress=None):
    """Refresca todas las fuentes activas. Devuelve un resumen por feed."""
    summary = []
    for feed in store.list_feeds(only_active=True):
        label = feed.get("title") or feed["url"]
        try:
            new_count, updated = refresh_feed(
                feed, since_days=since_days, max_items=max_items,
                strip_images=strip_images,
            )
            summary.append({"feed": label, "new": new_count, "updated": updated})
        except Exception as exc:
            store.record_fetch(feed["id"], error=str(exc)[:500])
            summary.append({"feed": label, "new": 0, "updated": 0, "error": str(exc)})
        if progress:
            progress(summary[-1])
    return summary


def import_feeds_file(path):
    """Migra un feeds.txt existente a la base. Idempotente."""
    added = 0
    for spec in sources.read_feed_list(path):
        store.add_feed(spec.url, lang=spec.lang)
        added += 1
    return added

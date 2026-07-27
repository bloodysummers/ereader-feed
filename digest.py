"""CLI sobre la misma base que usa la interfaz web.

    python3 digest.py refresh                 # trae artículos nuevos
    python3 digest.py list --status unsent
    python3 digest.py send --all-unsent       # empaqueta y sube al reader
    python3 digest.py send 12 15 18           # solo esos artículos
    python3 digest.py import-feeds feeds.txt  # siembra fuentes desde el archivo

Comparte estado con la app web: lo enviado aquí aparece enviado allá. El
antiguo state.json quedó obsoleto y ya no se usa.
"""

import argparse
import datetime as dt
import os
import sys

import ingest
import store
from crosspoint import DEFAULT_HOST, DEFAULT_REMOTE_DIR, CrossPoint
from db import init_db

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")


def cmd_refresh(args):
    summary = ingest.refresh_all(
        since_days=args.since_days, max_items=args.max_per_feed
    )
    if not summary:
        print("No hay fuentes. Usa: digest.py import-feeds feeds.txt")
        return 1
    for item in summary:
        note = f"  ! {item['error'][:60]}" if item.get("error") else ""
        print(f"  {item['new']:>3} nuevos  {item['feed']}{note}")
    print(f"\nTotal nuevo: {sum(i['new'] for i in summary)}")
    print("Conteos:", store.counts())
    return 0


def cmd_list(args):
    rows = store.list_articles(status=args.status, limit=args.limit)
    for row in rows:
        marks = "".join([
            "L" if row["read_at"] else "·",
            "E" if row["sent_at"] else "·",
            "Q" if row["queued"] else "·",
        ])
        date = row["published"].strftime("%m-%d") if row["published"] else "  -  "
        print(f"{row['id']:>5} [{marks}] {date}  {row['title'][:60]}")
    print(f"\n{len(rows)} artículos ({args.status}).  L=leído E=enviado Q=en cola")
    return 0


def cmd_send(args):
    if args.all_unsent:
        ids = [r["id"] for r in store.list_articles(status="unsent", limit=500)]
    else:
        ids = args.ids
    if not ids:
        print("Nada que enviar.")
        return 0

    rows = store.get_articles(ids)
    if not rows:
        print("Esos artículos no existen.", file=sys.stderr)
        return 1

    from app import build_epub, digest_title

    title = args.title or digest_title()
    data = build_epub(rows, title)

    os.makedirs(OUT_DIR, exist_ok=True)
    local_copy = os.path.join(OUT_DIR, f"{title}.epub")
    with open(local_copy, "wb") as fh:
        fh.write(data)
    print(f"{len(rows)} artículos -> {local_copy} ({len(data):,} bytes)")
    for row in rows:
        print(f"  · {row['title'][:70]}")

    if args.dry_run:
        print("\n--dry-run: no se sube ni se marca como enviado.")
        return 0

    job_id = store.create_job(title, [r["id"] for r in rows])
    client = CrossPoint(args.host)
    try:
        client.ensure_dir(args.remote_dir)
        name = client.upload_bytes(data, f"{title}.epub", args.remote_dir)
    except Exception as exc:
        store.finish_job(job_id, ok=False, error=str(exc)[:400])
        print(f"\nNo se pudo subir: {exc}", file=sys.stderr)
        print(f"Queda pendiente; el EPUB está en {local_copy}", file=sys.stderr)
        return 2

    remote = f"{args.remote_dir.rstrip('/')}/{name}"
    store.finish_job(job_id, ok=True, remote_name=remote)
    print(f"\nSubido a {remote}")
    return 0


def cmd_import(args):
    count = ingest.import_feeds_file(args.path)
    print(f"{count} fuentes en la base:")
    for feed in store.list_feeds():
        lang = f"  lang={feed['lang']}" if feed["lang"] else ""
        print(f"  {feed['id']:>3}  {feed['url']}{lang}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="trae artículos nuevos de las fuentes")
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--max-per-feed", type=int, default=25)
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("list", help="lista artículos")
    p.add_argument("--status", default="unread",
                   choices=("unread", "unsent", "queued", "read", "all"))
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("send", help="empaqueta en EPUB y sube al reader")
    p.add_argument("ids", nargs="*", type=int)
    p.add_argument("--all-unsent", action="store_true")
    p.add_argument("--title")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("import-feeds", help="siembra fuentes desde feeds.txt")
    p.add_argument("path", nargs="?", default=os.path.join(HERE, "feeds.txt"))
    p.set_defaults(func=cmd_import)

    args = ap.parse_args()
    init_db()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

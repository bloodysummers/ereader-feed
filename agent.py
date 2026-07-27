"""Agente local: la última milla entre la nube y el e-reader.

La app en Vercel no puede alcanzar crosspoint.local, así que este proceso corre
en tu red, pregunta si hay algo pendiente, baja el EPUB ya armado y lo sube al
reader. Solo hace conexiones salientes: nada queda expuesto hacia tu red.

    export FEED_URL=https://ereader-feed.vercel.app
    export APP_TOKEN=...
    python3 agent.py --once     # una pasada
    python3 agent.py            # se queda vigilando

Dependencias: solo `requests` (el EPUB llega hecho desde el servidor).
"""

import argparse
import datetime as dt
import os
import sys
import time

import requests

from crosspoint import DEFAULT_REMOTE_DIR, CrossPoint, Unreachable

POLL_SECONDS = 120
HTTP_TIMEOUT = 120


def log(message, error=False):
    """Con marca de tiempo: launchd concatena las corridas en un mismo archivo."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp}  {message}", file=sys.stderr if error else sys.stdout, flush=True)


class Agent:
    def __init__(self, base_url, token, host, remote_dir):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.reader = CrossPoint(host)
        self.remote_dir = remote_dir

    def pending(self):
        r = self.session.get(f"{self.base}/api/jobs/pending", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("job")

    def deliver(self, job):
        job_id = job["id"]
        log(f"envío #{job_id}: {job['title']} ({job['articles']} artículos)")

        r = self.session.get(
            f"{self.base}/api/jobs/{job_id}/epub", timeout=HTTP_TIMEOUT
        )
        r.raise_for_status()
        log(f"  EPUB recibido: {len(r.content):,} bytes")

        try:
            self.reader.ensure_dir(self.remote_dir)
            name = self.reader.upload_bytes(
                r.content, f"{job['title']}.epub", self.remote_dir
            )
        except Exception as exc:
            # Se reporta el fallo para que el servidor no marque los artículos
            # como enviados; el envío se reintenta en la próxima vuelta.
            self.report(job_id, ok=False, error=str(exc)[:400])
            raise

        remote = f"{self.remote_dir.rstrip('/')}/{name}"
        self.report(job_id, ok=True, remote_name=remote)
        log(f"  subido a {remote}")
        return remote

    def report(self, job_id, ok, error="", remote_name=""):
        self.session.post(
            f"{self.base}/api/jobs/{job_id}/done",
            data={"ok": str(ok).lower(), "error": error, "remote_name": remote_name},
            timeout=HTTP_TIMEOUT,
        ).raise_for_status()

    def tick(self):
        """Procesa todos los envíos pendientes. Devuelve cuántos entregó."""
        delivered = 0
        while True:
            job = self.pending()
            if not job:
                return delivered
            try:
                self.deliver(job)
                delivered += 1
            except Unreachable as exc:
                log(f"  reader no disponible: {exc}", error=True)
                return delivered
            except Exception as exc:
                log(f"  falló: {exc}", error=True)
                return delivered


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("FEED_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("APP_TOKEN", ""))
    ap.add_argument("--host", default=os.environ.get("CROSSPOINT_HOST", "crosspoint.local"))
    ap.add_argument("--dir", default=os.environ.get("CROSSPOINT_DIR", DEFAULT_REMOTE_DIR))
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    ap.add_argument("--once", action="store_true", help="una pasada y termina")
    args = ap.parse_args()

    agent = Agent(args.url, args.token, args.host, args.dir)

    if args.once:
        # Silencioso cuando no hay nada que hacer: launchd lo corre cada pocos
        # minutos y el log solo debe crecer cuando algo pasa de verdad.
        try:
            count = agent.tick()
        except requests.RequestException as exc:
            log(f"servidor no disponible: {type(exc).__name__}", error=True)
            return 0  # transitorio; se reintenta en la siguiente pasada
        if count:
            log(f"{count} envío(s) entregados.")
        return 0

    log(f"vigilando {agent.base} -> {args.host}{args.dir} cada {args.interval}s")
    while True:
        try:
            agent.tick()
        except requests.RequestException as exc:
            log(f"servidor no disponible: {type(exc).__name__}", error=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

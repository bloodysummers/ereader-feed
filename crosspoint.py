"""Cliente HTTP para un CrossPoint Reader (XTEANK X4) en la red local.

Protocolo obtenido por ingeniería inversa de la interfaz web del dispositivo
(/files) y verificado empíricamente contra firmware 1.4.1:

    POST /upload?path=<dir>   multipart/form-data, campo "file"  -> 200 / 400
    GET  /api/files?path=<dir>                                   -> JSON
    GET  /api/status                                             -> JSON
    GET  /download?path=<archivo>                                -> bytes
    POST /mkdir    multipart: name, path
    POST /delete   form-urlencoded: paths=<JSON array>
    POST /rename   multipart (ver interfaz web)
    POST /move     multipart (ver interfaz web)

Sin autenticación de ningún tipo.

El dispositivo también acepta subida por WebSocket en el puerto 81 (~55% más
rápido), pero requiere una máquina de estados START/READY/chunks/DONE y su
mensaje START usa ':' como separador, lo que rompe con nombres que lo
contengan. Para un pipeline desatendido HTTP es suficiente: el status code
basta como confirmación.
"""

import json
import os
import re
import time
import unicodedata

import requests

DEFAULT_HOST = "crosspoint.local"
# Los digests van en su propia carpeta para no perderse entre los libros.
DEFAULT_REMOTE_DIR = "/Digest"

# El reader duerme la radio WiFi y tarda en volver: conviene un timeout de
# conexión corto con reintentos, en vez de uno largo que cuelgue el cron.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 120
RETRIES = 4
RETRY_BACKOFF = 4  # segundos, creciente


class Unreachable(RuntimeError):
    """El dispositivo no respondió tras varios intentos."""

# FAT32 rechaza estos caracteres; el firmware responde 400 "Failed to create
# file on SD card". Los acentos y espacios sí funcionan (verificado).
FAT32_FORBIDDEN = r'[\\/:*?"<>|]'


def safe_filename(name, max_len=120):
    """Convierte un título de artículo en un nombre válido para la SD.

    Preserva acentos y espacios (el dispositivo los acepta) y sustituye los
    caracteres que FAT32 prohíbe.
    """
    name = unicodedata.normalize("NFC", name)
    name = re.sub(FAT32_FORBIDDEN, "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        root, ext = os.path.splitext(name)
        name = root[: max_len - len(ext)].strip() + ext
    return name or "sin-titulo"


class CrossPoint:
    def __init__(self, host=DEFAULT_HOST, timeout=None, retries=RETRIES):
        self.base = f"http://{host}"
        self.host = host
        self.timeout = timeout or (CONNECT_TIMEOUT, READ_TIMEOUT)
        self.retries = retries

    def _request(self, method, path, **kwargs):
        """Hace la petición reintentando si el dispositivo no contesta.

        Los cuerpos se pasan siempre como bytes, nunca como file object, para
        que un reintento no encuentre el descriptor ya consumido.
        """
        kwargs.setdefault("timeout", self.timeout)
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                return requests.request(method, f"{self.base}{path}", **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(RETRY_BACKOFF * attempt)
        raise Unreachable(
            f"{self.host} no respondió tras {self.retries} intentos "
            f"(¿el reader está dormido o fuera de la red?)"
        ) from last_error

    def status(self):
        r = self._request("GET", "/api/status")
        r.raise_for_status()
        return r.json()

    def list(self, path="/"):
        r = self._request("GET", "/api/files", params={"path": path})
        r.raise_for_status()
        return r.json()

    def upload(self, local_path, remote_dir="/", remote_name=None):
        """Sube un archivo. Devuelve el nombre final en el dispositivo.

        Lanza RuntimeError con el mensaje del firmware si rechaza el archivo.
        """
        with open(local_path, "rb") as fh:
            data = fh.read()
        return self.upload_bytes(
            data, remote_name or os.path.basename(local_path), remote_dir
        )

    def upload_bytes(self, data, remote_name, remote_dir="/"):
        """Sube bytes en memoria — útil para un EPUB recién generado."""
        name = safe_filename(remote_name)
        r = self._request(
            "POST",
            "/upload",
            params={"path": remote_dir},
            files={"file": (name, data, "application/octet-stream")},
        )
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {r.text.strip()}")
        return name

    def download(self, remote_path):
        r = self._request("GET", "/download", params={"path": remote_path})
        r.raise_for_status()
        return r.content

    def mkdir(self, name, parent="/"):
        r = self._request(
            "POST", "/mkdir", files={"name": (None, name), "path": (None, parent)}
        )
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {r.text.strip()}")

    def delete(self, *remote_paths):
        """Borra archivos o carpetas vacías.

        El firmware devuelve 500 "folder not empty" si la carpeta tiene
        contenido; usa delete_tree() para eso.
        """
        r = self._request(
            "POST", "/delete", data={"paths": json.dumps(list(remote_paths))}
        )
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {r.text.strip()}")

    def delete_tree(self, remote_dir):
        """Borra una carpeta y su contenido (el firmware no recursa)."""
        for f in self.list(remote_dir):
            child = remote_dir.rstrip("/") + "/" + f["name"]
            if f["isDirectory"]:
                self.delete_tree(child)
            else:
                self.delete(child)
        self.delete(remote_dir)

    def exists(self, name, remote_dir="/"):
        return any(f["name"] == name for f in self.list(remote_dir))

    def ensure_dir(self, remote_dir):
        """Crea la carpeta si falta. Acepta rutas anidadas ('/a/b')."""
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        parent = "/"
        for part in parts:
            if not any(
                f["name"] == part and f["isDirectory"] for f in self.list(parent)
            ):
                self.mkdir(part, parent)
            parent = parent.rstrip("/") + "/" + part
        return parent or "/"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Sube archivos a un CrossPoint Reader")
    ap.add_argument("files", nargs="*", help="archivos locales a subir")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--path", default="/", help="directorio destino")
    ap.add_argument("--ls", metavar="DIR", help="lista un directorio y termina")
    args = ap.parse_args()

    cp = CrossPoint(args.host)

    if args.ls is not None:
        for f in cp.list(args.ls):
            kind = "DIR " if f["isDirectory"] else "     "
            print(f"{kind}{f['size']:>10}  {f['name']}")
        raise SystemExit(0)

    st = cp.status()
    print(f"CrossPoint {st['device']} fw {st['version']} @ {st['ip']}")
    for f in args.files:
        try:
            print(f"  {f} -> {cp.upload(f, args.path)}")
        except Exception as exc:
            print(f"  {f} FALLÓ: {exc}")

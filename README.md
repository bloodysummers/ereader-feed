# ereader-feed

Plataforma para leer feeds y mandar lo que valga la pena a un **XTEANK X4 con
firmware CrossPoint**, empaquetado como EPUB con un capítulo por artículo.

## Cómo está partido

El reader vive en la LAN y una función de Vercel no puede alcanzarlo, así que
la última milla es local:

```
   NUBE (Vercel + Neon)                     TU RED
   ┌───────────────────────────┐
   │ app.py    UI + API        │
   │ ingest.py cron de fuentes │            ┌──────────────────┐
   │ Neon      artículos       │◀── poll ───│ agent.py         │
   │           y estados       │─── EPUB ──▶│   │              │
   └───────────────────────────┘            │   ▼              │
                                            │ crosspoint.local │
                                            └──────────────────┘
```

El agente solo hace conexiones salientes: nada de tu red queda expuesto. Corriendo
todo en local (`SEND_MODE=direct`) el agente no hace falta.

## Módulos

| Archivo | Qué hace |
|---|---|
| `app.py` | Interfaz web y API. Entrypoint ASGI que Vercel detecta solo. |
| `db.py` | Esquema. SQLite local o Postgres según `DATABASE_URL`. |
| `store.py` | Acceso a datos y **toda la lógica de estados**. |
| `ingest.py` | Refresca fuentes hacia la base. Idempotente. |
| `sources.py` | Lee RSS, limpia HTML y busca traducciones por artículo. |
| `epubgen.py` | Arma el EPUB con índice y TOC anidado. |
| `crosspoint.py` | Cliente del reader (protocolo obtenido por ingeniería inversa). |
| `agent.py` | Agente local: baja el EPUB y lo sube al reader. |
| `digest.py` | CLI sobre la misma base que la web. |
| `feeds.py` | Descubre y valida feeds antes de agregarlos. |

## Modelo de estados

Un artículo tiene marcas **independientes**, y esa es la parte importante:

| Campo | Significa | Lo escribe |
|---|---|---|
| `fetched_at` | está en el caché | la ingesta |
| `read_at` | lo leí en la web | abrir ese artículo |
| `sent_at` | llegó al e-reader | un envío exitoso |
| `queued` | apartado para el próximo envío | el botón «Apartar» |

La ingesta hace **upsert y nunca oculta nada**: consultar la lista no consume
artículos, y refrescar no borra lo que ya leíste. Reenviar algo es posible
porque "leído" y "enviado" no son la misma marca.

## Local

```bash
pip install -r requirements.txt
python3 digest.py import-feeds feeds.txt     # siembra fuentes (opcional)
uvicorn app:app --reload                     # http://127.0.0.1:8000
```

Sin `APP_TOKEN` la app queda **abierta**, lo cual está bien en localhost.
En local `SEND_MODE` es `direct`: el botón de enviar sube al reader al momento.

También hay CLI:

```bash
python3 digest.py refresh
python3 digest.py list --status unsent
python3 digest.py send --all-unsent
```

## El token de acceso

`APP_TOKEN` es un secreto compartido arbitrario, pero **el mismo valor viaja
por tres caminos**, así que conviene que sea ASCII y sin caracteres especiales:

| Quién | Cómo lo manda |
|---|---|
| Navegador | cookie `auth` |
| E-reader | HTTP Basic (usuario cualquiera, contraseña = el token) |
| Cron de Vercel | `Authorization: Bearer` con `CRON_SECRET` |

Evita `:` (separador de Basic), espacios, `;`, `,`, comillas y acentos. Genera
uno así:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Debe quedar idéntico en `APP_TOKEN`, en `CRON_SECRET` y en la contraseña del
catálogo OPDS del reader.

## Producción (pendiente de configurar)

1. Crear el proyecto en tu cuenta de Neon y copiar la cadena de conexión.
2. Configurar variables en Vercel, **en todos los entornos** (ver aviso abajo):

   ```bash
   vercel env add DATABASE_URL      # postgres de Neon
   vercel env add APP_TOKEN         # el token generado arriba
   vercel env add CRON_SECRET       # el MISMO valor que APP_TOKEN
   ```

3. `vercel deploy --prod`. Las tablas se crean solas al primer arranque.
4. En el reader, apuntar el catálogo OPDS a `https://<tu-app>/opds`, con
   cualquier usuario y el token como contraseña.
5. El cron diario ya está declarado en `vercel.json` (`/api/refresh`, 11:00 UTC
   ≈ 05:00 en CDMX). En plan Hobby solo se permite **una corrida al día** y
   Vercel la dispara en algún momento dentro de esa hora.

Con OPDS funcionando, `agent.py` deja de hacer falta: el reader descarga solo.

> **Aviso sobre los entornos.** Si defines las variables únicamente en
> Production, cada *preview deployment* correrá **sin `APP_TOKEN`** y con URL
> pública: la app quedaría abierta. Define `APP_TOKEN` también en Preview. Si
> falta `DATABASE_URL`, la app ahora falla con un mensaje claro en vez de
> intentar escribir en un disco de solo lectura.

**No despliegues sin `APP_TOKEN`.** Sin él cualquiera con la URL puede leer tus
artículos y disparar envíos a tu reader.

## Notas del dispositivo

- La SD es FAT32: rechaza `\ / : * ? " < > |` en los nombres. `safe_filename()`
  los sustituye y conserva acentos y espacios, que sí funcionan.
- El firmware **no tiene autenticación**. No lo expongas a internet.
- El reader duerme la radio WiFi; por eso hay reintentos tanto en el cliente
  HTTP como a nivel de envío (`MAX_JOB_ATTEMPTS`).
- Los EPUB van a `/Digest` para no perderse entre los libros.

"""Esquema y conexión. Corre igual en SQLite local y en Postgres (Neon).

El backend lo decide DATABASE_URL:

    (sin definir)                        -> sqlite:///feed.db  (local)
    postgresql+psycopg://user:pw@host/db -> Neon en producción

La clave del modelo es que los estados de un artículo son independientes:
consultar la lista no consume nada, leer marca solo ese artículo, y enviar al
reader es otra marca distinta. La ingesta hace upsert y nunca oculta nada.
"""

import os

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table,
    Text, UniqueConstraint, create_engine,
)

DEFAULT_SQLITE = "sqlite:///" + os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "feed.db"
)

metadata = MetaData()

feeds = Table(
    "feeds",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("url", String(500), nullable=False, unique=True),
    Column("title", String(300)),
    # Idioma preferido: se busca la traducción del artículo si existe.
    Column("lang", String(10)),
    Column("position", Integer, default=0),
    Column("active", Boolean, default=True, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("last_fetch_at", DateTime(timezone=True)),
    Column("last_error", Text),
)

articles = Table(
    "articles",
    metadata,
    Column("id", Integer, primary_key=True),
    # uid es el <guid> del feed: sobrevive a que se reordene o reedite.
    Column("uid", String(500), nullable=False),
    Column("feed_id", Integer, ForeignKey("feeds.id"), nullable=False),
    Column("source", String(300)),
    Column("title", Text),
    Column("url", Text),
    Column("published", DateTime(timezone=True)),
    Column("html", Text),
    Column("chars", Integer, default=0),
    Column("lang", String(10)),
    # --- estados independientes, este es el punto del rediseño ---
    Column("fetched_at", DateTime(timezone=True)),  # está en el caché
    Column("read_at", DateTime(timezone=True)),     # lo leí en la web
    Column("sent_at", DateTime(timezone=True)),     # llegó al e-reader
    Column("queued", Boolean, default=False, nullable=False),  # va en el próximo envío
    UniqueConstraint("uid", name="uq_articles_uid"),
)

# Un envío pendiente que el agente local debe recoger. El EPUB no se guarda:
# se arma al vuelo cuando el agente lo pide, así no hace falta blob storage.
jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(300)),
    Column("article_ids", Text),  # JSON: [1, 2, 3]
    Column("status", String(20), default="pending", nullable=False),  # pending|done|error
    Column("created_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("error", Text),
    Column("remote_name", Text),
    # Un reader dormido es una falla transitoria: el envío sigue pendiente y
    # se reintenta, hasta agotar MAX_JOB_ATTEMPTS.
    Column("attempts", Integer, default=0, nullable=False),
)

MAX_JOB_ATTEMPTS = 5

_engine = None


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        # En Vercel el sistema de archivos es de solo lectura salvo /tmp, así
        # que caer a SQLite daría un error críptico. Mejor decirlo claro.
        if os.environ.get("VERCEL"):
            raise RuntimeError(
                "Falta DATABASE_URL: en Vercel hace falta el Postgres de Neon "
                "(el disco es de solo lectura y SQLite no funciona)."
            )
        return DEFAULT_SQLITE
    # Neon y Vercel entregan la URL en formato 'postgres://', que SQLAlchemy
    # no reconoce; y sin driver explícito intentaría psycopg2.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        kwargs = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # Permite que el servidor de desarrollo use la conexión en hilos.
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


def init_db():
    """Crea las tablas que falten y agrega columnas nuevas. Idempotente.

    create_all() no altera tablas existentes, así que las columnas añadidas
    después se agregan aquí. ADD COLUMN funciona igual en SQLite y Postgres.
    """
    engine = get_engine()
    metadata.create_all(engine)

    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table in metadata.tables.values():
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            kind = column.type.compile(engine.dialect)
            default = ""
            if column.default is not None and column.default.is_scalar:
                literal = column.default.arg
                default = f" DEFAULT {int(literal) if isinstance(literal, bool) else literal!r}"
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {kind}{default}"
                ))
    return database_url()


if __name__ == "__main__":
    print("Base:", init_db())

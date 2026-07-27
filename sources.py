"""Ingesta de artículos. Por ahora RSS/Atom; la ingesta por email se añadirá aquí.

Substack incluye el artículo completo en <content:encoded>, así que para esas
fuentes no hace falta scraping. Para feeds que solo publican un resumen se cae
a trafilatura sobre el enlace original.
"""

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import feedparser
import requests
import trafilatura
from lxml import etree
from lxml import html as lhtml

# El Python de python.org en macOS no trae las raíces CA que usa urllib, y
# algunos servidores rechazan el User-Agent por omisión. Descargamos con
# requests (que usa certifi) y le pasamos los bytes a feedparser.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ereader-feed/1.0"
HTTP_TIMEOUT = 30

# Debajo de este número de caracteres asumimos que el feed dio un resumen
# y vale la pena ir por el artículo completo al enlace original.
FULL_TEXT_THRESHOLD = 1500

# Subárboles que se eliminan por completo.
JUNK_TAGS = (
    "script", "style", "iframe", "form", "button", "input", "select",
    "textarea", "noscript", "svg", "video", "audio", "object", "embed",
    "link", "meta",
)

# Clases/ids típicos de widgets de newsletter (Substack los usa mucho).
JUNK_PATTERN = re.compile(
    r"subscri|paywall|share|social|promo|poll|captioned-button|footer|"
    r"comment|pixel|tracking|embed-post|preview-link|meta-",
    re.I,
)

# Etiquetas que sobreviven; el resto se desenvuelve conservando el texto.
KEEP_TAGS = {
    "div", "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "ul", "ol",
    "li", "blockquote", "pre", "code", "em", "strong", "i", "b", "a",
    "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
    "sup", "sub", "img",
}

ALLOWED_ATTRS = {"a": {"href"}, "img": {"src", "alt"}}


# Códigos que se reconocen como segmento de idioma en una URL.
KNOWN_LANGS = {
    "en", "es", "pt", "pt-br", "fr", "de", "it", "ar", "ru", "tr", "hi",
    "bn", "zh", "ca", "eu", "gl",
}

# Palabras función para verificar que una traducción realmente cambió de
# idioma: hay sitios que devuelven 200 con la versión original cuando la
# traducción no existe.
STOPWORDS = {
    "es": (" de ", " que ", " la ", " el ", " los ", " las ", " con ", " para ",
           " una ", " del ", " se ", " por ", " no ", " su "),
    "en": (" the ", " of ", " and ", " to ", " that ", " with ", " for ",
           " is ", " in ", " on ", " it ", " as "),
    "pt": (" de ", " que ", " os ", " as ", " com ", " para ", " uma ", " do ",
           " da ", " não ", " se "),
    "fr": (" de ", " le ", " la ", " les ", " des ", " et ", " pour ", " une ",
           " du ", " que ", " pas "),
    "de": (" der ", " die ", " das ", " und ", " nicht ", " mit ", " für ",
           " ein ", " eine ", " von "),
    "it": (" di ", " che ", " il ", " la ", " le ", " con ", " per ", " una ",
           " del ", " non "),
}


def _normalize_url(url):
    """Colapsa las barras duplicadas que algunos feeds emiten en la ruta."""
    if not url:
        return url
    parsed = urlparse(url)
    return parsed._replace(path=re.sub(r"/{2,}", "/", parsed.path)).geturl()


def _lang_score(text, lang):
    sample = " " + " ".join(text.lower().split()[:400]) + " "
    return sum(sample.count(word) for word in STOPWORDS.get(lang, ()))


def translated_variants(url, lang):
    """URLs donde podría vivir la traducción del artículo.

    Cubre los patrones habituales: sufijo de idioma (.../en -> .../es),
    segmento intermedio (/en/... -> /es/...) y parámetro ?lang=.
    """
    parsed = urlparse(_normalize_url(url))
    variants = []
    segments = parsed.path.rstrip("/").split("/")

    for i, segment in enumerate(segments):
        if segment.lower() in KNOWN_LANGS and segment.lower() != lang:
            swapped = segments.copy()
            swapped[i] = lang
            variants.append(parsed._replace(path="/".join(swapped)).geturl())

    query = parse_qs(parsed.query)
    if "lang" in query:
        query["lang"] = [lang]
        variants.append(parsed._replace(query=urlencode(query, doseq=True)).geturl())

    seen = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def fetch_translation(url, lang, strip_images=True):
    """Busca el artículo en `lang`. Devuelve (título, html) o None.

    Verifica que el texto haya cambiado de idioma, porque un 200 no garantiza
    que la traducción exista.
    """
    for candidate in translated_variants(url, lang):
        try:
            response = requests.get(
                candidate, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT
            )
            if response.status_code != 200:
                continue
        except Exception:
            continue

        extracted = trafilatura.extract(
            response.text,
            output_format="html",
            include_comments=False,
            include_images=not strip_images,
            favor_precision=True,
        )
        if not extracted:
            continue

        text = re.sub(r"<[^>]+>", " ", extracted)
        if len(text.strip()) < FULL_TEXT_THRESHOLD:
            continue

        # ¿De verdad cambió de idioma? Algunos sitios responden 200 con la
        # versión original cuando la traducción no existe.
        if lang != "en" and _lang_score(text, lang) <= _lang_score(text, "en"):
            continue

        metadata = trafilatura.extract_metadata(response.text)
        title = metadata.title if metadata else None
        return title, extracted
    return None


@dataclass
class Article:
    title: str
    source: str
    url: str
    published: dt.datetime | None
    html: str  # ya limpio
    uid: str

    @property
    def date_str(self):
        return self.published.strftime("%Y-%m-%d") if self.published else "sin fecha"


def clean_html(raw, strip_images=True):
    """Deja solo estructura básica: encabezados, párrafos, listas, citas.

    Devuelve XHTML bien formado, apto para meter en un EPUB.
    """
    if not raw or not raw.strip():
        return "<div></div>"

    try:
        frag = lhtml.fragment_fromstring(raw, create_parent="div")
    except Exception:
        return "<div><p>" + lhtml.fromstring(f"<p>{raw}</p>").text_content() + "</p></div>"

    etree.strip_elements(frag, *JUNK_TAGS, with_tail=False)
    etree.strip_elements(frag, etree.Comment, with_tail=False)

    # Widgets de suscripción/compartir identificados por class o id.
    for el in frag.xpath("//*[@class or @id]"):
        signature = f"{el.get('class', '')} {el.get('id', '')}"
        if JUNK_PATTERN.search(signature):
            el.drop_tree()  # conserva el tail, a diferencia de remove()

    if strip_images:
        etree.strip_elements(frag, "img", "figure", "picture", "source", with_tail=False)

    for el in frag.iter():
        if not isinstance(el.tag, str):
            continue
        allowed = ALLOWED_ATTRS.get(el.tag, set())
        for attr in list(el.attrib):
            if attr not in allowed:
                del el.attrib[attr]

    present = {el.tag for el in frag.iter() if isinstance(el.tag, str)}
    unwrap = present - KEEP_TAGS
    if unwrap:
        etree.strip_tags(frag, *unwrap)

    # Párrafos que quedaron vacíos tras quitar widgets e imágenes.
    for el in frag.xpath(".//p | .//div | .//li"):
        if not el.text_content().strip() and len(el) == 0:
            el.drop_tree()

    return etree.tostring(frag, method="xml", encoding="unicode")


def _entry_html(entry):
    """Saca el HTML más completo que ofrezca la entrada del feed."""
    if entry.get("content"):
        return max((c.get("value", "") for c in entry["content"]), key=len)
    return entry.get("summary", "") or ""


def _entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return None


def fetch_feed(url, since_days=2, max_items=10, strip_images=True, seen=(), prefer_lang=None):
    """Devuelve los Article nuevos de un feed, del más reciente al más viejo."""
    response = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"feed ilegible: {parsed.get('bozo_exception', 'desconocido')}")

    source = parsed.feed.get("title", url)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    articles = []

    for entry in parsed.entries:
        uid = entry.get("id") or entry.get("link")
        if not uid or uid in seen:
            continue

        published = _entry_date(entry)
        if published and published < cutoff:
            continue

        raw = _entry_html(entry)
        link = _normalize_url(entry.get("link", ""))
        title = entry.get("title", "Sin título").strip()

        translated = (
            fetch_translation(link, prefer_lang, strip_images)
            if prefer_lang and link
            else None
        )
        if translated:
            translated_title, raw = translated
            if translated_title:
                title = translated_title
        else:
            # Si el feed solo dio un resumen, ir al artículo completo.
            text_len = len(re.sub(r"<[^>]+>", "", raw))
            if text_len < FULL_TEXT_THRESHOLD and link:
                downloaded = trafilatura.fetch_url(link)
                if downloaded:
                    extracted = trafilatura.extract(
                        downloaded,
                        output_format="html",
                        include_comments=False,
                        include_images=not strip_images,
                        favor_precision=True,
                    )
                    if extracted and len(extracted) > len(raw):
                        raw = extracted

        html = clean_html(raw, strip_images=strip_images)
        if len(re.sub(r"<[^>]+>", "", html).strip()) < 200:
            continue  # sin contenido aprovechable

        articles.append(
            Article(
                title=title,
                source=source,
                url=link,
                published=published,
                html=html,
                uid=uid,
            )
        )
        if len(articles) >= max_items:
            break

    return articles


def resolve_feed_url(entry):
    """Expande los atajos de feeds.txt a una URL de feed completa.

        astralcodexten            -> https://astralcodexten.substack.com/feed
        substack:astralcodexten   -> https://astralcodexten.substack.com/feed
        noahpinion.blog           -> https://noahpinion.blog/feed
        https://sitio.com/rss.xml -> tal cual
    """
    entry = entry.strip()
    if entry.startswith(("http://", "https://")):
        return entry
    if entry.startswith("substack:"):
        handle = entry.split(":", 1)[1].strip().strip("/")
        return f"https://{handle}.substack.com/feed"
    if "." in entry:  # dominio propio
        return f"https://{entry.strip('/')}/feed"
    return f"https://{entry}.substack.com/feed"


@dataclass
class FeedSpec:
    """Una fuente de feeds.txt con sus opciones."""

    url: str
    lang: str | None = None  # idioma preferido; se buscan traducciones
    options: dict = field(default_factory=dict)

    def __str__(self):
        return self.url + (f"  (lang={self.lang})" if self.lang else "")


def parse_feed_line(line):
    """Interpreta 'entrada | clave=valor, clave=valor' y devuelve un FeedSpec.

        https://progressive.international/rss.xml | lang=es
        astralcodexten
    """
    entry, _, raw_options = line.strip().partition("|")
    options = {}
    for chunk in raw_options.split(","):
        key, _, value = chunk.partition("=")
        if key.strip() and value.strip():
            options[key.strip().lower()] = value.strip()
    return FeedSpec(
        url=resolve_feed_url(entry),
        lang=options.pop("lang", None),
        options=options,
    )


def read_feed_list(path):
    """Lee feeds.txt: una entrada por línea, ignora vacías y comentarios."""
    with open(path, encoding="utf-8") as fh:
        return [
            parse_feed_line(line)
            for line in fh
            if line.strip() and not line.lstrip().startswith("#")
        ]


class State:
    """Recuerda qué artículos ya se enviaron, para no repetirlos."""

    MAX_PER_FEED = 500

    def __init__(self, path):
        self.path = path
        self.seen = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.seen = json.load(fh)

    def for_feed(self, url):
        return set(self.seen.get(url, []))

    def mark(self, url, uids):
        existing = self.seen.setdefault(url, [])
        existing.extend(u for u in uids if u not in existing)
        if len(existing) > self.MAX_PER_FEED:
            self.seen[url] = existing[-self.MAX_PER_FEED :]

    def save(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.seen, fh, ensure_ascii=False, indent=1)

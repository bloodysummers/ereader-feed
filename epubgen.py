"""Genera un EPUB tipo periódico: un capítulo por artículo, con índice."""

import hashlib
import html
from itertools import groupby

from ebooklib import epub

# Estilos sobrios pensados para e-ink: sin colores, buen interlineado,
# y control de desbordes en tablas y bloques de código.
CSS = """
body { font-family: serif; line-height: 1.5; margin: 0 1em; }
h1 { font-size: 1.4em; line-height: 1.25; margin: 0 0 .2em; }
h2 { font-size: 1.15em; margin: 1.2em 0 .3em; }
h3 { font-size: 1.05em; margin: 1em 0 .3em; }
p { margin: 0 0 .8em; text-align: justify; }
.meta { font-size: .85em; font-style: italic; margin: 0 0 1.2em; }
.source-link { font-size: .8em; margin-top: 2em; word-wrap: break-word; }
blockquote { margin: .8em 1.2em; font-style: italic; }
pre, code { white-space: pre-wrap; word-wrap: break-word; font-size: .85em; }
img, table { max-width: 100%; }
table { table-layout: fixed; }
hr { border: 0; border-top: 1px solid #999; margin: 1.5em 0; }
"""

CHAPTER = """<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>{title}</h1>
<p class="meta">{source} · {date}</p>
{content}
<p class="source-link">{url}</p>
</body>
</html>
"""

TOC_PAGE = """<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>{title}</h1>
<p class="meta">{count} artículos</p>
{items}
</body>
</html>
"""


def build_digest(
    articles, title, out_path, language="es", author="Feed Digest",
    group_by_source=True,
):
    """Escribe un EPUB con un capítulo por artículo. Devuelve out_path.

    Con group_by_source, el índice y el TOC se organizan en secciones por
    publicación, tipo periódico. `articles` debe venir ya ordenado.
    """
    if not articles:
        raise ValueError("no hay artículos para el digest")

    book = epub.EpubBook()
    fingerprint = hashlib.sha1(
        (title + "".join(a.uid for a in articles)).encode()
    ).hexdigest()
    book.set_identifier(f"crosspoint-digest-{fingerprint[:16]}")
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)

    style = epub.EpubItem(
        uid="style", file_name="style.css", media_type="text/css", content=CSS
    )
    book.add_item(style)

    chapters = []
    for i, art in enumerate(articles, 1):
        filename = f"ch{i:03d}.xhtml"
        chapter = epub.EpubHtml(
            title=art.title, file_name=filename, lang=language, uid=f"ch{i:03d}"
        )
        chapter.content = CHAPTER.format(
            title=html.escape(art.title),
            source=html.escape(art.source),
            date=art.date_str,
            content=art.html,
            url=html.escape(art.url),
        )
        chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    def entry(art, chapter):
        return (
            f'<li><a href="{chapter.file_name}">{html.escape(art.title)}</a>'
            f" <br/><small>{art.date_str}</small></li>"
        )

    if group_by_source:
        # Secciones por publicación, como las de un periódico.
        toc_html, toc_tree = [], []
        pairs = list(zip(articles, chapters))
        for source, group in groupby(pairs, key=lambda pair: pair[0].source):
            group = list(group)
            toc_html.append(f"<h2>{html.escape(source)}</h2>\n<ol>")
            toc_html.extend(entry(art, ch) for art, ch in group)
            toc_html.append("</ol>")
            toc_tree.append(
                (epub.Section(source), tuple(ch for _, ch in group))
            )
        toc_body = "\n".join(toc_html)
    else:
        toc_body = "<ol>\n" + "\n".join(
            f'<li><a href="{ch.file_name}">{html.escape(art.title)}</a>'
            f" <br/><small>{html.escape(art.source)} · {art.date_str}</small></li>"
            for art, ch in zip(articles, chapters)
        ) + "\n</ol>"
        toc_tree = list(chapters)

    # Índice navegable propio: el visor del dispositivo no siempre expone
    # el nav del EPUB, así que se incluye como primera página.
    index = epub.EpubHtml(title="Índice", file_name="index.xhtml", lang=language, uid="index")
    index.content = TOC_PAGE.format(
        title=html.escape(title), count=len(articles), items=toc_body
    )
    index.add_item(style)
    book.add_item(index)

    book.toc = [epub.Link("index.xhtml", "Índice", "index")] + toc_tree
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [index] + chapters

    epub.write_epub(out_path, book)
    return out_path

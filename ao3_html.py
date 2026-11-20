"""Read an AO3 "Download -> HTML" file and write it out as an EPUB or a PDF.

AO3's HTML downloads are laid out like this:

- ``#preface`` holds the work's metadata: a ``dl.tags`` list (rating, fandom, tags,
  stats), the title, the byline, and the summary and notes
- ``#chapters`` holds one ``div.meta.group`` per chapter with an ``h2.heading``
  ("Chapter 3" or "Chapter 3: Title"), followed by a ``div.userstuff`` with the text.
  A one-chapter work has no heading.
- ``#afterword`` holds the end notes and the closing "please comment" message

Everything copied out of the file is passed through ``sanitize`` first. It keeps a
small set of formatting tags and drops scripts, remote images, classes, and most
inline styles, so the same fragment is safe in an EPUB's XHTML and in the PDF
renderer, which only understands a subset of HTML.
"""

from __future__ import annotations

import copy
import html
import io
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import lxml.html
import pymupdf
from ebooklib import epub
from lxml import etree

LABELS = {
    "Summary",
    "Notes",
    "End Notes",
    "Chapter Summary",
    "Chapter Notes",
    "Chapter End Notes",
}
# Tags removed along with everything inside them.
DROP_TREE = {
    "script", "style", "iframe", "object", "embed", "audio", "video", "source",
    "form", "input", "button", "select", "textarea", "noscript", "svg", "canvas",
    "head", "link", "meta", "title",
}
# Tags that become another tag.
RENAME = {
    "i": "em", "b": "strong", "strike": "del", "s": "del", "tt": "code",
    "h1": "h3", "h2": "h3", "h4": "h3", "h5": "h3", "h6": "h3",
}
KEEP = {
    "p", "br", "hr", "em", "strong", "u", "del", "ins", "sub", "sup", "small",
    "blockquote", "ul", "ol", "li", "a", "h3", "pre", "code", "div", "span", "q",
    "cite", "abbr", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "caption",
}
VOID = {"br", "hr", "img"}
SAFE_STYLE_PROPS = {"text-align", "font-style", "font-weight", "text-decoration"}
LINK_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)
WORK_URL_RE = re.compile(r"https?://[^/\s]*archiveofourown\.org/works/\d+", re.IGNORECASE)
PUBLISHED_RE = re.compile(r"Published:\s*(\d{4}-\d{2}-\d{2})")
SUBJECT_LABELS = {"Fandom", "Fandoms", "Relationship", "Relationships", "Character",
                  "Characters", "Additional Tags"}

PDF_CSS = """
body { font-family: serif; font-size: 11pt; line-height: 1.4; }
h1 { font-size: 20pt; font-weight: normal; text-align: center; margin: 0 0 16pt 0;
     page-break-before: always; }
h1.first { page-break-before: avoid; }
h2 { font-size: 13pt; font-weight: normal; text-align: center; margin: 14pt 0 6pt 0; }
h3 { font-size: 12pt; margin: 12pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
p.byline { text-align: center; font-style: italic; margin-bottom: 14pt; }
p.noindent { text-align: center; font-size: 9pt; }
blockquote { margin: 4pt 0 8pt 18pt; }
dl { margin: 10pt 0; }
dt { font-weight: bold; margin-top: 5pt; }
dd { margin: 0 0 0 0; }
hr { margin: 10pt 0; }
a { color: #1a3f7a; }
""".strip()

PAGE_MARGIN = 64  # points; a little under 0.9in


class WorkError(Exception):
    """The file is not an AO3 HTML download, or has nothing to convert."""


@dataclass
class Chapter:
    title: str
    html: str


@dataclass
class Work:
    title: str
    authors: list[str]
    language: str  # as AO3 prints it, e.g. "English"
    summary: str
    subjects: list[str]
    url: str
    published: str
    preface_html: str
    chapters: list[Chapter]
    afterword_html: str = ""

    @property
    def author(self) -> str:
        return ", ".join(self.authors)

    @property
    def sections(self) -> list[Chapter]:
        sections = [Chapter("Preface", self.preface_html), *self.chapters]
        if self.afterword_html:
            sections.append(Chapter("Afterword", self.afterword_html))
        return sections


# --- cleaning --------------------------------------------------------------


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def has_class(el: lxml.html.HtmlElement, name: str) -> bool:
    return name in (el.get("class") or "").split()


def safe_style(value: str) -> str:
    kept = []
    for declaration in value.split(";"):
        prop, _, val = declaration.partition(":")
        prop, val = prop.strip().lower(), val.strip().lower()
        if prop in SAFE_STYLE_PROPS and re.fullmatch(r"[a-z\- ]+", val):
            kept.append(f"{prop}: {val}")
    return "; ".join(kept)


def sanitize(root: lxml.html.HtmlElement, labels: bool = False) -> None:
    """Reduce everything under root to the small tag set in KEEP, in place.

    With labels=True, a paragraph that only says "Summary" or "Notes" becomes a
    heading, which is how AO3 marks those blocks.
    """
    for el in list(root.iterdescendants()):
        if not isinstance(el.tag, str):  # comments and processing instructions
            el.drop_tree()
            continue
        tag = el.tag.lower()
        if tag in DROP_TREE or has_class(el, "endnote-link"):
            el.drop_tree()
            continue
        if tag == "img":
            alt = normalize(el.get("alt") or "")
            el.attrib.clear()
            if not alt:
                el.drop_tree()
                continue
            el.tag, el.text = "span", alt
            continue
        if labels and tag == "p" and normalize(el.text_content()) in LABELS:
            tag = "h2"
        style = el.get("style") or ""
        attrs = dict(el.attrib)
        if tag == "center":
            tag, style = "div", f"text-align: center; {style}"
        tag = RENAME.get(tag, tag)
        if tag not in KEEP and tag != "h2":
            el.drop_tag()
            continue
        el.tag = tag
        el.attrib.clear()
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            if not LINK_RE.match(href):
                el.drop_tag()  # in-page anchors would dangle once the file is split
                continue
            el.set("href", href)
        elif tag in {"td", "th"}:
            for name in ("colspan", "rowspan"):
                if (attrs.get(name) or "").isdigit():
                    el.set(name, attrs[name])
        elif tag == "ol" and (attrs.get("start") or "").isdigit():
            el.set("start", attrs["start"])
        cleaned = safe_style(style)
        if cleaned and tag not in {"a", "br", "hr"}:
            el.set("style", cleaned)

    # An empty <p/> reads as an open tag to HTML parsers, so always write <p></p>.
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag not in VOID and len(el) == 0 and not el.text:
            el.text = ""


def inner_xml(el: lxml.html.HtmlElement) -> str:
    parts = [html.escape(el.text or "", quote=False)]
    parts.extend(etree.tostring(child, encoding="unicode", method="xml") for child in el)
    return "".join(parts)


def clean_fragment(el: lxml.html.HtmlElement, labels: bool = False) -> str:
    """Sanitized markup for el, or "" when nothing readable is left."""
    wrapper = lxml.html.Element("div")
    wrapper.append(copy.deepcopy(el))
    sanitize(wrapper, labels=labels)
    markup = inner_xml(wrapper)
    return markup if normalize(wrapper.text_content()) else ""


def stats_on_separate_lines(dl: lxml.html.HtmlElement) -> None:
    """Turn the multi-line text in a dd (the Stats block) into lines split by <br/>."""
    for dd in dl.xpath(".//dd"):
        if len(dd) or "\n" not in (dd.text or ""):
            continue
        lines = [line.strip() for line in dd.text.splitlines() if line.strip()]
        if not lines:
            continue
        dd.text = lines[0]
        for line in lines[1:]:
            br = etree.SubElement(dd, "br")
            br.tail = line


# --- parsing ---------------------------------------------------------------


def first(doc: lxml.html.HtmlElement, xpath: str) -> lxml.html.HtmlElement | None:
    found = doc.xpath(xpath)
    return found[0] if found else None


def parse_work(path: Path) -> Work:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        doc = lxml.html.document_fromstring(text)
    except (etree.ParserError, ValueError) as exc:
        raise WorkError(f"{path} could not be read as HTML: {exc}") from exc

    chapters_el = first(doc, '//*[@id="chapters"]')
    if chapters_el is None:
        raise WorkError(f"{path} does not look like an AO3 HTML download (no #chapters)")

    preface = first(doc, '//*[@id="preface"]')
    meta = None
    if preface is not None:
        meta = first(preface, './/div[contains(concat(" ", normalize-space(@class), " "), " meta ")]')

    title = ""
    authors: list[str] = []
    language = "English"
    summary = ""
    published = ""
    subjects: list[str] = []
    url = ""
    preface_blocks: list[str] = []

    if meta is not None:
        h1 = first(meta, ".//h1")
        title = normalize(h1.text_content()) if h1 is not None else ""
        byline = first(meta, './/div[contains(concat(" ", normalize-space(@class), " "), " byline ")]')
        if byline is not None:
            authors = [normalize(a.text_content()) for a in byline.xpath(".//a")]
            authors = [name for name in authors if name]
            if not authors:
                authors = [re.sub(r"^by\s+", "", normalize(byline.text_content()), flags=re.I)]
        dl = first(meta, './/dl[contains(concat(" ", normalize-space(@class), " "), " tags ")]')
        if dl is not None:
            for dt in dl.xpath("./dt"):
                dd = dt.getnext()
                if dd is None or dd.tag != "dd":
                    continue
                label = normalize(dt.text_content()).rstrip(":")
                value = normalize(dd.text_content())
                if label == "Language" and value:
                    language = value
                elif label == "Stats":
                    found = PUBLISHED_RE.search(value)
                    published = found.group(1) if found else ""
                elif label in SUBJECT_LABELS:
                    subjects.extend(normalize(a.text_content()) for a in dd.xpath(".//a"))
            stats_on_separate_lines(dl)
        summary_label = next(
            (p for p in meta.xpath(".//p") if normalize(p.text_content()) == "Summary"), None
        )
        quote = summary_label.getnext() if summary_label is not None else None
        if quote is not None:
            paragraphs = [normalize(p.text_content()) for p in quote.xpath(".//p")]
            summary = "\n\n".join(p for p in paragraphs if p) or normalize(quote.text_content())

        found_url = next(
            (a.get("href") for a in preface.xpath(".//a[@href]") if WORK_URL_RE.match(a.get("href"))),
            "",
        )
        url = found_url

        preface_blocks.append(f"<h1>{html.escape(title)}</h1>" if title else "")
        if authors:
            preface_blocks.append(f'<p class="byline">by {html.escape(", ".join(authors))}</p>')
        if url:
            safe_url = html.escape(url)
            preface_blocks.append(
                '<p class="noindent">Posted originally on the Archive of Our Own at '
                f'<a href="{safe_url}">{safe_url}</a>.</p>'
            )
        for child in meta:
            if child is dl or child is h1 or child is byline:
                if child is dl:
                    preface_blocks.append(clean_fragment(child))
                continue
            preface_blocks.append(clean_fragment(child, labels=True))

    if not title:
        head_title = first(doc, "//title")
        title = normalize(head_title.text_content()).split(" - ")[0] if head_title is not None else ""
    title = title or path.stem.replace("_", " ")
    if not preface_blocks:
        preface_blocks = [f"<h1>{html.escape(title)}</h1>"]

    chapters = parse_chapters(chapters_el, title)
    if not chapters:
        raise WorkError(f"{path} has no chapter text")

    afterword_html = ""
    afterword = first(doc, '//*[@id="afterword"]')
    if afterword is not None:
        pieces = [
            clean_fragment(child, labels=True)
            for child in afterword
            if isinstance(child.tag, str) and not has_class(child, "toc-heading")
        ]
        pieces = [piece for piece in pieces if piece]
        if pieces:
            afterword_html = "\n".join(["<h1>Afterword</h1>", *pieces])

    return Work(
        title=title,
        authors=authors,
        language=language,
        summary=summary[:4000],
        subjects=list(dict.fromkeys(subjects)),
        url=url,
        published=published,
        preface_html="\n".join(block for block in preface_blocks if block),
        chapters=chapters,
        afterword_html=afterword_html,
    )


def parse_chapters(chapters_el: lxml.html.HtmlElement, work_title: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    current_title: str | None = None
    pieces: list[str] = []

    def close() -> None:
        body = "\n".join(piece for piece in pieces if piece)
        if body:
            chapters.append(
                Chapter(current_title or work_title, f"<h1>{html.escape(current_title or work_title)}</h1>\n{body}")
            )

    for child in chapters_el:
        if not isinstance(child.tag, str):
            continue
        heading = first(child, './/h2[contains(concat(" ", normalize-space(@class), " "), " heading ")]')
        if heading is not None:
            close()
            current_title, pieces = normalize(heading.text_content()) or None, []
            heading.getparent().remove(heading)
        pieces.append(clean_fragment(child, labels=has_class(child, "meta")))
    close()
    return chapters


# --- EPUB ------------------------------------------------------------------


def write_epub(work: Work, dest: Path, css: str, language: str) -> list[str]:
    book = epub.EpubBook()
    identity = work.url or f"{work.title}\n{work.author}"
    book.set_identifier(str(uuid.uuid5(uuid.NAMESPACE_URL, identity)))
    book.set_title(work.title)
    book.set_language(language)
    for author in work.authors:
        book.add_author(author)
    if work.summary:
        book.add_metadata("DC", "description", work.summary)
    for subject in work.subjects:
        book.add_metadata("DC", "subject", subject)
    if work.published:
        book.add_metadata("DC", "date", work.published)
    if work.url:
        book.add_metadata("DC", "source", work.url)

    style = epub.EpubItem(
        uid="style", file_name="style/default.css", media_type="text/css", content=css
    )
    book.add_item(style)

    sections: list[epub.EpubHtml] = []
    for index, section in enumerate(work.sections):
        slug = re.sub(r"[^a-z0-9]+", "-", section.title.casefold()).strip("-") or f"section-{index}"
        page = epub.EpubHtml(
            title=section.title, file_name=f"text/{index:02d}-{slug[:40]}.xhtml", lang=language
        )
        page.content = section.html
        page.add_item(style)
        book.add_item(page)
        sections.append(page)

    book.toc = sections
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *sections]
    dest.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(dest, book)
    return [section.title for section in work.sections]


# --- PDF -------------------------------------------------------------------


def write_pdf(work: Work, dest: Path) -> list[str]:
    body = []
    for index, section in enumerate(work.sections):
        markup = section.html
        if index == 0:
            markup = markup.replace("<h1>", '<h1 class="first">', 1)
        body.append(markup)
    story = pymupdf.Story(
        html="<html><body>" + "\n".join(body) + "</body></html>", user_css=PDF_CSS
    )

    page_rect = pymupdf.paper_rect("letter")
    content_rect = page_rect + (PAGE_MARGIN, PAGE_MARGIN, -PAGE_MARGIN, -PAGE_MARGIN)
    outline: list[list] = []
    page_number = 0

    def record(position: pymupdf.ElementPosition) -> None:
        if position.heading == 1 and position.open_close & 1:
            outline.append([1, normalize(position.text), position.page_num + 1])

    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    more = True
    while more:
        device = writer.begin_page(page_rect)
        more, _filled = story.place(content_rect)
        story.element_positions(record, {"page_num": page_number})
        story.draw(device)
        writer.end_page()
        page_number += 1
    writer.close()

    doc = pymupdf.open("pdf", buffer.getvalue())
    try:
        if outline:
            doc.set_toc(outline)
        doc.set_metadata(
            {
                "title": work.title,
                "author": work.author,
                "subject": work.summary[:500],
                "keywords": ", ".join(work.subjects),
            }
        )
        add_page_numbers(doc)
        dest.parent.mkdir(parents=True, exist_ok=True)
        doc.save(dest, garbage=3, deflate=True)
    finally:
        doc.close()
    return [section.title for section in work.sections]


def add_page_numbers(doc: pymupdf.Document) -> None:
    """Centered page numbers in the bottom margin, except on the first page."""
    for index, page in enumerate(doc):
        if index == 0:
            continue
        label = str(index + 1)
        width = pymupdf.get_text_length(label, fontname="tiro", fontsize=9)
        page.insert_text(
            ((page.rect.width - width) / 2, page.rect.height - PAGE_MARGIN / 2),
            label,
            fontname="tiro",
            fontsize=9,
        )

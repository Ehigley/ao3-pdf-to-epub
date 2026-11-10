#!/usr/bin/env python3
"""Convert an Archive of Our Own PDF download into an EPUB with one file per chapter.

AO3's "Download → PDF" files (the Chromium/Skia export) are the expected input.
They look like this:

- a preface page of work metadata (rating, tags, stats)
- a title page with the author byline and summary
- each chapter on its own pages, opened by a large bold "Chapter N" banner
  and a smaller bold chapter title
- a one-line footer asking readers to comment on the Archive

Chapter boundaries come from those bold banners, so the EPUB table of contents
matches the PDF outline. Lines that the PDF wrapped are joined back into
paragraphs, and italic and bold runs are kept.

A PDF with no large bold headings falls back to a single chapter.

A directory of PDFs can be converted in one run. Each PDF keeps its path
relative to the source directory and is written into the target directory
with a .epub suffix. The batch is split across processes and tracked with
a progress bar.
"""

from __future__ import annotations

import argparse
import html
import multiprocessing
import os
import re
import statistics
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import epub
from tqdm import tqdm
import pymupdf

FOOTER_START = "Please drop by the Archive and comment"
# AO3 labels that are headings in the text but are the same size as body copy.
STANDALONE_HEADINGS = {"Summary", "Notes", "End Notes"}
LANGUAGE_CODES = {
    "english": "en",
    "spanish": "es",
    "français": "fr",
    "french": "fr",
    "deutsch": "de",
    "german": "de",
    "português": "pt",
    "portuguese": "pt",
    "русский": "ru",
    "russian": "ru",
    "中文": "zh",
    "chinese": "zh",
    "italiano": "it",
    "italian": "it",
    "polski": "pl",
    "polish": "pl",
    "日本語": "ja",
    "japanese": "ja",
}

CSS = """
body {
  font-family: Georgia, "Times New Roman", serif;
  line-height: 1.45;
  margin: 1em 5%;
}
h1 {
  font-size: 1.5em;
  font-weight: normal;
  text-align: center;
  margin: 1.4em 0 1em;
  line-height: 1.2;
}
h2 {
  font-size: 1.15em;
  font-weight: normal;
  text-align: center;
  margin: 1.4em 0 0.6em;
}
p {
  margin: 0;
  text-indent: 1.2em;
}
p + p { margin-top: 0.15em; }
p.noindent, p.booktitle, p.byline, p.scene, h1 + p, h2 + p {
  text-indent: 0;
}
p.booktitle {
  text-align: center;
  font-size: 1.25em;
  margin: 0.4em 0 0.8em;
}
p.byline {
  text-align: center;
  font-style: italic;
  margin: 0 0 1.2em;
}
p.scene {
  text-align: center;
  margin: 1em 0;
  letter-spacing: 0.3em;
}
dl { margin: 1.2em 0; }
dt { font-weight: bold; margin-top: 0.7em; }
dd { margin: 0.15em 0 0 0; }
a { color: inherit; }
""".strip()

URL_RE = re.compile(r"https?://[^\s<>]+")
BYLINE_RE = re.compile(r"^by\s+\S", re.IGNORECASE)


class ConversionError(Exception):
    """A PDF could not be converted. A batch keeps going after one of these."""


@dataclass
class Run:
    text: str
    italic: bool = False
    bold: bool = False


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    runs: list[Run]
    raw: str

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)

    @property
    def plain(self) -> str:
        """Readable text with PDF artifacts turned into normal spaces."""
        return normalize_space(self.raw.replace("\n", " ").replace("\xa0", " "))

    @property
    def bold_line(self) -> bool:
        meaningful = [run for run in self.runs if run.text.strip(" \t\n\xa0")]
        return bool(meaningful) and all(run.bold for run in meaningful)

    @property
    def ends_with_break(self) -> bool:
        return self.raw.endswith("\n")

    @property
    def starts_with_indent(self) -> bool:
        return self.raw.lstrip("\n").startswith("\xa0")


@dataclass
class Section:
    title: str
    lines: list[Line] = field(default_factory=list)
    nav_title: str | None = None

    @property
    def label(self) -> str:
        return self.nav_title or self.title


def normalize_space(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_title(text: str) -> str:
    return normalize_space(text.replace("\xa0", " ").replace("\n", " "))


def load_lines(doc: pymupdf.Document) -> list[Line]:
    lines: list[Line] = []
    for page_index, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                runs: list[Run] = []
                raw_parts: list[str] = []
                size = 0.0
                for span in line["spans"]:
                    raw_parts.append(span["text"])
                    size = max(size, span["size"])
                    flags = span["flags"]
                    font = span["font"]
                    runs.append(
                        Run(
                            text=span["text"],
                            italic="Italic" in font or "Oblique" in font or bool(flags & 2),
                            bold="Bold" in font or bool(flags & 16),
                        )
                    )
                raw = "".join(raw_parts)
                if not raw.strip(" \t\n\xa0"):
                    # A line of only non-breaking spaces is a blank paragraph the
                    # author inserted, which we turn into a scene break.
                    if raw.strip("\n"):
                        x0, y0, x1, y1 = line["bbox"]
                        lines.append(
                            Line(page_index, x0, y0, x1, y1, size or 12, runs, raw)
                        )
                    continue
                x0, y0, x1, y1 = line["bbox"]
                lines.append(Line(page_index, x0, y0, x1, y1, size, runs, raw))
    lines.sort(key=lambda ln: (ln.page, ln.y0, ln.x0))
    return lines


def body_size(lines: list[Line]) -> float:
    sizes = [ln.size for ln in lines if ln.plain]
    if not sizes:
        return 12.0
    return statistics.median(sizes)


def is_banner(line: Line, body: float) -> bool:
    """Large bold line AO3 uses for the work title and 'Chapter N'."""
    return line.bold_line and line.size >= body * 1.3 and len(line.plain) < 120


def is_subhead(line: Line, body: float) -> bool:
    """Smaller bold line AO3 uses for the author's chapter title."""
    return (
        line.bold_line
        and body * 1.08 <= line.size < body * 1.3
        and len(line.plain) < 160
    )


def is_label(line: Line) -> bool:
    text = line.plain
    return line.x0 < 140 and text.endswith(":") and len(text) < 40


def page_is_meta(lines: list[Line]) -> bool:
    return sum(1 for line in lines if is_label(line)) >= 3


def split_sections(lines: list[Line], book_title: str) -> list[Section]:
    body = body_size(lines)
    sections: list[Section] = []
    current = Section("Preface")

    for line in lines:
        if is_banner(line, body):
            title = clean_title(line.raw)
            if current.lines:
                sections.append(current)
                current = Section(title)
            elif not sections:
                # Nothing before the first banner: this banner opens the book.
                current = Section(title)
            else:
                current.title = title
            continue
        if is_subhead(line, body) and not current.lines:
            current.title = clean_title(line.raw)
            continue
        current.lines.append(line)

    if current.lines or not sections:
        sections.append(current)

    for section in sections:
        if section.title.casefold() == book_title.casefold() and any(
            line.plain == "Summary" for line in section.lines
        ):
            section.nav_title = "Summary"
    return sections


def gap_threshold(lines: list[Line], body: float) -> float:
    return max(6.0, body * 0.5)


def starts_new_paragraph(prev: Line | None, line: Line, threshold: float) -> bool:
    if prev is None:
        return True
    if FOOTER_START in line.raw or FOOTER_START in prev.plain:
        return True
    if line.starts_with_indent or prev.ends_with_break:
        return True
    if prev.page != line.page:
        return False
    gap = line.y0 - prev.y1
    if gap < 0:
        return True
    return gap > threshold


def prepared_runs(line: Line) -> list[Run]:
    """Drop the newline characters Chromium leaves inside italic runs.

    A newline between two words stands in for a missing space ("hates\\nit").
    A newline before punctuation is just noise ("friend\\n.").
    """
    runs: list[Run] = []
    for run in line.runs:
        text = run.text.replace("\xa0", " ").replace("\n", " ")
        if text:
            runs.append(Run(text, run.italic, run.bold))
    return runs


def paragraph_runs(lines: list[Line]) -> list[Run]:
    runs: list[Run] = []
    for index, line in enumerate(lines):
        piece = prepared_runs(line)
        if not piece:
            continue
        if runs and needs_space(runs[-1].text, piece[0].text):
            runs.append(Run(" "))
        runs.extend(piece)
    return cleanup_runs(runs)


def needs_space(prev: str, nxt: str) -> bool:
    if not prev or not nxt:
        return False
    if prev[-1].isspace() or nxt[0].isspace():
        return False
    if nxt[0] in ",.;:!?)]}\u2019\u201d'\"»":
        return False
    if prev[-1] in "\u2014\u2013-(":
        return False
    return True


def cleanup_runs(runs: list[Run]) -> list[Run]:
    chars: list[tuple[str, bool, bool]] = []
    for run in runs:
        for ch in run.text:
            chars.append((ch, run.italic, run.bold))

    kept: list[tuple[str, bool, bool]] = []
    for index, (ch, italic, bold) in enumerate(chars):
        if ch == " " and index + 1 < len(chars) and chars[index + 1][0] in ",.;:!?)]}\u2019\u201d":
            # "singing ," -> "singing,"
            nxt = chars[index + 1][0]
            if nxt in ",.;:!?)]":
                continue
        if ch == " " and kept and kept[-1][0] == " ":
            continue
        kept.append((ch, italic, bold))

    while kept and kept[0][0] == " ":
        kept.pop(0)
    while kept and kept[-1][0] == " ":
        kept.pop()

    # Move boundary spaces out of italic/bold so tags don't wrap the space.
    for index, (ch, italic, bold) in enumerate(kept):
        if ch != " ":
            continue
        if italic or bold:
            kept[index] = (" ", False, False)

    merged: list[Run] = []
    for ch, italic, bold in kept:
        if merged and merged[-1].italic == italic and merged[-1].bold == bold:
            merged[-1].text += ch
        else:
            merged.append(Run(ch, italic, bold))
    return merged


def runs_to_html(runs: list[Run]) -> str:
    parts: list[str] = []
    for run in runs:
        text = linkify(html.escape(run.text))
        if run.italic and run.bold:
            text = f"<strong><em>{text}</em></strong>"
        elif run.italic:
            text = f"<em>{text}</em>"
        elif run.bold:
            text = f"<strong>{text}</strong>"
        parts.append(text)
    return "".join(parts)


def linkify(escaped: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        trimmed = url.rstrip(".,);")
        suffix = url[len(trimmed):]
        return f'<a href="{trimmed}">{trimmed}</a>{suffix}'

    return URL_RE.sub(replace, escaped)


def is_blank_paragraph(lines: list[Line]) -> bool:
    return not normalize_space(
        "".join(line.raw for line in lines).replace("\n", " ").replace("\xa0", " ")
    )


def render_prose(lines: list[Line], body: float) -> list[str]:
    threshold = gap_threshold(lines, body)
    blocks: list[str] = []
    group: list[Line] = []

    def flush() -> None:
        if not group:
            return
        if is_blank_paragraph(group):
            if blocks and blocks[-1] != '<p class="scene">* * *</p>':
                blocks.append('<p class="scene">* * *</p>')
        else:
            plain = normalize_space(
                "".join(line.raw for line in group).replace("\n", " ").replace("\xa0", " ")
            )
            if plain in STANDALONE_HEADINGS:
                blocks.append(f"<h2>{html.escape(plain)}</h2>")
            elif BYLINE_RE.match(plain):
                blocks.append(f'<p class="byline">{html.escape(plain)}</p>')
            else:
                css = ' class="noindent"' if not blocks else ""
                blocks.append(f"<p{css}>{runs_to_html(paragraph_runs(group))}</p>")
        group.clear()

    for line in lines:
        if group and starts_new_paragraph(group[-1], line, threshold):
            flush()
        group.append(line)
    flush()

    while blocks and blocks[0] == '<p class="scene">* * *</p>':
        blocks.pop(0)
    while blocks and blocks[-1] == '<p class="scene">* * *</p>':
        blocks.pop()
    return blocks


def render_meta(lines: list[Line], book_title: str) -> list[str]:
    ordered = sorted(lines, key=lambda ln: (ln.y0, ln.x0))
    html_parts: list[str] = []
    index = 0
    while index < len(ordered):
        line = ordered[index]
        if is_label(line):
            label = line.plain[:-1].strip()
            index += 1
            values: list[Line] = []
            while index < len(ordered) and not is_label(ordered[index]) and ordered[index].x0 > 150:
                values.append(ordered[index])
                index += 1
            html_parts.append(
                f"<dt>{html.escape(label)}</dt><dd>{format_meta_value(values)}</dd>"
            )
            continue
        plain = line.plain
        if plain.casefold() == book_title.casefold():
            html_parts.append(f'<p class="booktitle">{html.escape(plain)}</p>')
        else:
            html_parts.append(f'<p class="noindent">{linkify(html.escape(plain))}</p>')
        index += 1

    rebuilt: list[str] = []
    pending: list[str] = []
    for part in html_parts:
        if part.startswith("<dt>"):
            pending.append(part)
        else:
            if pending:
                rebuilt.append("<dl>" + "".join(pending) + "</dl>")
                pending = []
            rebuilt.append(part)
    if pending:
        rebuilt.append("<dl>" + "".join(pending) + "</dl>")
    return rebuilt


def format_meta_value(lines: list[Line]) -> str:
    logical: list[str] = []
    for line in lines:
        pieces = [normalize_space(piece.replace("\xa0", " ")) for piece in line.raw.split("\n")]
        pieces = [piece for piece in pieces if piece]
        if not pieces:
            continue
        if not logical:
            logical.extend(pieces)
            continue
        logical[-1] = normalize_space(logical[-1] + " " + pieces[0])
        logical.extend(pieces[1:])
    return "<br/>".join(linkify(html.escape(piece)) for piece in logical)


def section_html(section: Section, body: float, book_title: str) -> str:
    if page_is_meta(section.lines):
        by_page: dict[int, list[Line]] = {}
        for line in section.lines:
            by_page.setdefault(line.page, []).append(line)
        blocks: list[str] = []
        for page in sorted(by_page):
            page_lines = by_page[page]
            if page_is_meta(page_lines):
                blocks.extend(render_meta(page_lines, book_title))
            else:
                blocks.extend(render_prose(page_lines, body))
    else:
        blocks = render_prose(section.lines, body)

    # The summary page's visible heading is the book title; the nav label is "Summary".
    visible = book_title if section.nav_title == "Summary" else section.title
    if section.label == "Preface":
        visible = "Preface"
    heading = f"<h1>{html.escape(visible)}</h1>"
    return heading + "\n" + "\n".join(blocks)


def language_from_preface(lines: list[Line]) -> str:
    ordered = sorted(lines, key=lambda ln: (ln.y0, ln.x0))
    for index, line in enumerate(ordered):
        if line.plain.lower().startswith("language"):
            for follow in ordered[index + 1:index + 3]:
                if follow.x0 > 150:
                    return LANGUAGE_CODES.get(follow.plain.casefold(), "en")
    return "en"


def summary_text(section: Section) -> str:
    paragraphs: list[str] = []
    seen_heading = False
    threshold = gap_threshold(section.lines, body_size(section.lines) if section.lines else 12)
    group: list[Line] = []

    def take() -> None:
        if not group or is_blank_paragraph(group):
            return
        plain = normalize_space(
            "".join(line.raw for line in group).replace("\n", " ").replace("\xa0", " ")
        )
        if plain == "Summary":
            nonlocal seen_heading
            seen_heading = True
            return
        if seen_heading and plain and not BYLINE_RE.match(plain):
            paragraphs.append(plain)

    for line in section.lines:
        if group and starts_new_paragraph(group[-1], line, threshold):
            take()
            group = []
        group.append(line)
    take()
    return "\n\n".join(paragraphs)[:4000]


def build_epub(
    sections: list[Section],
    title: str,
    author: str,
    language: str,
    description: str,
    subjects: list[str],
) -> epub.EpubBook:
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{title}\n{author}")))
    book.set_title(title)
    book.set_language(language)
    if author:
        book.add_author(author)
    if description:
        book.add_metadata("DC", "description", description)
    for subject in subjects:
        book.add_metadata("DC", "subject", subject)

    style = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=CSS,
    )
    book.add_item(style)

    body = body_size([line for section in sections for line in section.lines]) or 12
    chapters: list[epub.EpubHtml] = []
    for index, section in enumerate(sections):
        slug = re.sub(r"[^a-z0-9]+", "-", section.label.casefold()).strip("-") or f"section-{index}"
        chapter = epub.EpubHtml(
            title=section.label,
            file_name=f"text/{index:02d}-{slug}.xhtml",
            lang=language,
        )
        chapter.content = section_html(section, body, title)
        chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    return book


def convert(pdf_path: Path, epub_path: Path) -> list[str]:
    doc = pymupdf.open(pdf_path)
    try:
        if doc.is_encrypted:
            raise ConversionError(
                f"{pdf_path} is encrypted; open it with a password first."
            )
        meta = doc.metadata or {}
        lines = load_lines(doc)
    finally:
        doc.close()

    if not lines:
        raise ConversionError(f"{pdf_path} has no extractable text.")

    title = clean_title(meta.get("title") or "") or clean_title(lines[0].raw) or pdf_path.stem
    author = clean_title(meta.get("author") or "")
    sections = split_sections(lines, title)

    if sections and not page_is_meta(sections[-1].lines):
        footer_at = next(
            (
                index
                for index, line in enumerate(sections[-1].lines)
                if FOOTER_START in line.raw or FOOTER_START in line.plain
            ),
            None,
        )
        if footer_at is not None:
            footer_lines = sections[-1].lines[footer_at:]
            del sections[-1].lines[footer_at:]
            if footer_lines:
                sections.append(Section("Afterword", footer_lines))

    language = "en"
    description = ""
    for section in sections:
        if section.label == "Preface":
            language = language_from_preface(section.lines)
        if section.nav_title == "Summary":
            description = summary_text(section)
            if not author:
                for line in section.lines:
                    match = BYLINE_RE.match(line.plain)
                    if match:
                        author = line.plain[3:].strip()
                        break

    keywords = [part.strip() for part in (meta.get("keywords") or "").split(",") if part.strip()]
    book = build_epub(sections, title, author, language, description, keywords)
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(epub_path, book)
    return [section.label for section in sections]


@dataclass
class JobResult:
    pdf_path: Path
    epub_path: Path
    labels: list[str] = field(default_factory=list)
    error: str | None = None


def find_pdfs(source: Path) -> list[Path]:
    """Every PDF under source, skipping hidden files and directories."""
    if source.is_file():
        return [source]
    pdfs = [
        path
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix.lower() == ".pdf"
        and not any(part.startswith(".") for part in path.relative_to(source).parts)
    ]
    return sorted(pdfs)


def destination_for(source: Path, pdf: Path, target: Path | None) -> Path:
    if source.is_file():
        if target is None:
            return pdf.with_suffix(".epub")
        if target.is_dir():
            return target / f"{pdf.stem}.epub"
        return target
    if target is None:
        raise ConversionError("a target directory is required when source is a directory")
    return target / pdf.relative_to(source).with_suffix(".epub")


def _convert_job(job: tuple[Path, Path]) -> JobResult:
    pdf_path, epub_path = job
    try:
        labels = convert(pdf_path, epub_path)
    except ConversionError as exc:
        return JobResult(pdf_path, epub_path, error=str(exc))
    except Exception as exc:
        return JobResult(pdf_path, epub_path, error=f"{exc.__class__.__name__}: {exc}")
    return JobResult(pdf_path, epub_path, labels=labels)


def worker_count(requested: int | None, jobs: int) -> int:
    if jobs <= 1:
        return 1
    available = os.cpu_count() or 1
    chosen = available if requested is None else requested
    return max(1, min(chosen, jobs))


def convert_all(jobs: list[tuple[Path, Path]], workers: int) -> list[JobResult]:
    """Convert every job, using a process pool when there is more than one worker."""
    results: list[JobResult] = []
    with tqdm(total=len(jobs), unit="pdf", desc="Converting") as bar:
        def take(result: JobResult) -> None:
            results.append(result)
            if result.error:
                bar.write(f"failed: {result.pdf_path.name}: {result.error}")
            bar.set_postfix_str(result.pdf_path.name, refresh=False)
            bar.update()

        if workers <= 1:
            for job in jobs:
                take(_convert_job(job))
            return results

        context = multiprocessing.get_context("spawn")
        with context.Pool(processes=workers) as pool:
            for result in pool.imap_unordered(_convert_job, jobs, chunksize=1):
                take(result)
    return results


def report_one(result: JobResult) -> int:
    if result.error:
        print(result.error, file=sys.stderr)
        return 1
    print(f"Wrote {result.epub_path}")
    print("Contents:")
    for label in result.labels:
        print(f"  {label}")
    return 0


def report_batch(results: list[JobResult], target: Path) -> int:
    failed = sorted(
        (result for result in results if result.error),
        key=lambda result: result.pdf_path,
    )
    written = len(results) - len(failed)
    print(f"Wrote {written} of {len(results)} to {target}")
    if not failed:
        return 0
    print("Failed:", file=sys.stderr)
    for result in failed:
        print(f"  {result.pdf_path.name}: {result.error}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert AO3 PDF downloads to EPUBs, one chapter per section."
    )
    parser.add_argument(
        "source",
        type=Path,
        help="PDF file, or a directory of PDFs (subfolders included)",
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        help=(
            "EPUB file, or a directory to write one EPUB per PDF. "
            "Required for a directory. A lone PDF is written beside itself."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="EPUB path for a single PDF (an alternative to passing target)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="worker processes for a directory (default: one per CPU)",
    )
    args = parser.parse_args(argv)

    if args.output and args.target:
        parser.error("pass the output either as target or with -o, not both")
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")

    source = args.source.expanduser().resolve()
    target = (args.target or args.output)
    if target is not None:
        target = target.expanduser().resolve()

    if not source.exists():
        print(f"No such file or directory: {source}", file=sys.stderr)
        return 1
    if source.is_file() and source.suffix.lower() != ".pdf":
        print(f"Not a PDF: {source}", file=sys.stderr)
        return 1
    if source.is_dir():
        if target is None:
            parser.error("a target directory is required when source is a directory")
        if target.exists() and not target.is_dir():
            print(f"Target is not a directory: {target}", file=sys.stderr)
            return 1
        if not target.exists() and target.suffix.lower() == ".epub":
            print(
                f"Target must be a directory when source is a directory: {target}",
                file=sys.stderr,
            )
            return 1
        target.mkdir(parents=True, exist_ok=True)

    pdfs = find_pdfs(source)
    if not pdfs:
        print(f"No PDFs found in {source}", file=sys.stderr)
        return 1

    jobs = [(pdf, destination_for(source, pdf, target)) for pdf in pdfs]
    if source.is_file():
        return report_one(_convert_job(jobs[0]))

    if target is None:
        parser.error("a target directory is required when source is a directory")
    workers = worker_count(args.jobs, len(jobs))
    print(f"Converting {len(jobs)} PDFs with {workers} workers")
    results = convert_all(jobs, workers)
    return report_batch(results, target)


if __name__ == "__main__":
    raise SystemExit(main())

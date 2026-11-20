# ao3-pdf-to-epub

A simple project for converting AO3 downloads to EPUBs, or AO3 HTML downloads to PDFs. It works on a single file or a directory.

| Input | Default output | With `--pdf` |
| --- | --- | --- |
| AO3 HTML (`.html`, `.htm`) | EPUB | PDF |
| AO3 PDF (`.pdf`) | EPUB | not supported (skipped in a folder run) |

HTML is the better source when you have a choice: the chapters, tags, summary, and author come straight from the file instead of being rebuilt from page layout.

## Install

You need Python 3. From the project directory, install the packages in `requirements.txt`:

```bash
python3 -m pip install -r requirements.txt
```

## Convert one file

Pass the path. The output is written next to the input, with the same name:

```bash
python3 pdf_to_epub.py story.html     # creates story.epub
python3 pdf_to_epub.py story.pdf      # creates story.epub
```

To write a PDF from an HTML file, add `--pdf` (or `-p`):

```bash
python3 pdf_to_epub.py story.html --pdf    # creates story.pdf
```

The PDF has an outline with one entry per chapter, page numbers, and the work's title, author, and tags in its document properties. Passing `--pdf` with a PDF input is an error, since there is nothing to convert.

To choose a different output path, pass it as the second argument or with `-o`. If the name ends in `.epub` or `.pdf`, it has to match the format you asked for:

```bash
python3 pdf_to_epub.py story.html story.epub
python3 pdf_to_epub.py story.html -o ~/Books/story.pdf --pdf
```

## Convert a folder

Pass a directory and a directory to write the results into. The output directory is required, and it is created if it does not exist. Files in subfolders are included. Each output keeps its path relative to the source folder, with a `.epub` (or `.pdf`) suffix.

```bash
python3 pdf_to_epub.py fics/ epubs/
python3 pdf_to_epub.py fics/ pdfs/ --pdf
```

A file at `fics/series/story.html` is written to `epubs/series/story.epub`, or `pdfs/series/story.pdf` with `--pdf`. A folder can mix PDFs and HTML files. With `--pdf`, only the HTML files are converted and PDFs are left out.

If a folder has both `story.html` and `story.pdf`, they would produce the same `story.epub`. The HTML is used, and the PDF is skipped with a message.

The batch runs one worker process per CPU and shows a progress bar. To use a set number of workers, pass `-j` or `--jobs`:

```bash
python3 pdf_to_epub.py fics/ epubs/ --jobs 4
```

## Notes on HTML conversion

- Formatting is kept for italics, bold, underline, strikethrough, links, block quotes, lists, and tables. Scripts, remote images (an image's alt text is kept), classes, and most inline styles are dropped.
- Links within the page (such as "see the end notes") are turned into plain text, since the EPUB is split into one file per chapter.
- The EPUB has a section for the preface (tags, stats, summary, notes), one for each chapter, and one for the afterword. A work with a single chapter and no chapter heading gets one section titled with the work's name.
- PDFs are US Letter, set in a serif font. Accented Latin, Greek, Cyrillic, CJK, and Arabic text display correctly, but emoji do not: they come out as the wrong glyph or an empty box. The EPUB keeps them as written, so use it if a fic relies on emoji.

# ao3-pdf-to-epub

A simple project for converting AO3-formatted PDFs to EPUBs. It works on a single file or a directory.

## Install

You need Python 3. From the project directory, install the packages in `requirements.txt`:

```bash
python3 -m pip install -r requirements.txt
```

## Convert one PDF

Pass the PDF path. The EPUB is written next to the PDF, with the same name:

```bash
python3 pdf_to_epub.py story.pdf
```

That creates `story.epub` in the same folder. To choose a different output path, pass it as the second argument or with `-o`:

```bash
python3 pdf_to_epub.py story.pdf story.epub
python3 pdf_to_epub.py story.pdf -o ~/Books/story.epub
```

## Convert a folder

Pass a directory of PDFs and a directory to write the EPUBs into. The output directory is required, and it is created if it does not exist. PDFs in subfolders are included. Each EPUB keeps its path relative to the source folder, with a `.epub` suffix.

```bash
python3 pdf_to_epub.py pdfs/ epubs/
```

A file at `pdfs/series/story.pdf` is written to `epubs/series/story.epub`. The batch runs one worker process per CPU and shows a progress bar. To use a set number of workers, pass `-j` or `--jobs`:

```bash
python3 pdf_to_epub.py pdfs/ epubs/ --jobs 4
```

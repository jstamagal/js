Convert a source file into text or a vault media embed for ingestion.

Usage:
- Text and code are read directly.
- PDFs use `pdftotext`; office and ebook formats use available converters.
- Images and audio/video can be copied into vault assets when `vault` is supplied.
- The model still decides what pages to write; this tool only converts bytes.

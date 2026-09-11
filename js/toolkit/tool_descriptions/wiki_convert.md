Convert a source file into text or a vault media embed for ingestion.

Usage:
- Text, code, and structured text (JSON, JSONL, CSV, TSV, YAML, XML) are read
  directly.
- PDFs use `pdftotext`; office and ebook formats use whichever of pandoc or
  `soffice` is installed. A spreadsheet comes back as CSV with every sheet in
  it, so nothing past the first sheet is dropped.
- Images and audio/video can be copied into vault assets when `vault` is
  supplied; the vault directory must already exist.
- The model still decides what pages to write; this tool only converts bytes.

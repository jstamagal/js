Convert one file to text or a vault embed: text, code, PDF, image (OCR),
office/spreadsheet, and audio/video.

Usage:
- Text, code, and structured text (JSON, JSONL, CSV, TSV, YAML, XML) are read
  directly.
- PDFs use `pdftotext`; office and ebook formats use whichever of pandoc or
  `soffice` is installed. A spreadsheet comes back as CSV with every sheet in
  it, so nothing past the first sheet is dropped.
- Images come back as an embed plus any text `tesseract` reads from them. A
  missing or failed tesseract is reported as OCR unavailable, not as an image
  with no text.
- Images and audio/video are copied into `<vault>/assets/` when `vault` is
  supplied, or into the nearest ancestor directory holding a `PURPOSE.md`. With
  neither, the embed is returned and nothing is copied. An explicit `vault`
  must already exist.
- The model still decides what pages to write; this tool only converts input
  and copies media into assets.

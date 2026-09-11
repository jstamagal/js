"""wiki_convert: turn any file into text, or a media embed."""
from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from ..core import ToolContext
from .helpers import run, read_text, resolve_vault, find_vault, copy_to_assets

TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".srt", ".vtt", ".log", ".toml", ".ini", ".cfg"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".sh", ".bash", ".c", ".h", ".cpp", ".hpp", ".java", ".rb", ".php", ".lua", ".sql", ".css"}
STRUCTURED_TEXT_EXT = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".yaml", ".yml", ".xml"}
PANDOC_EXT = {".docx", ".odt", ".rtf", ".epub", ".pptx", ".html", ".htm"}
SOFFICE_EXT = {".doc", ".ppt", ".xls", ".xlsx", ".docx", ".odt", ".rtf", ".pptx", ".html", ".htm"}
OFFICE_EXT = PANDOC_EXT | SOFFICE_EXT
_SOFFICE_ONLY_EXT = SOFFICE_EXT - PANDOC_EXT
_SPREADSHEET_EXT = {".xls", ".xlsx"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AV_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".mov", ".webm", ".avi"}


def _which(binary: str) -> str | None:
    return shutil.which(binary)


def _convert_with_soffice(p: Path, ext: str, cap: int, context: ToolContext) -> str:
    # LibreOffice has no plain-text export filter for spreadsheets, so a sheet
    # goes through the CSV filter and comes back as text.
    target = "csv" if ext in _SPREADSHEET_EXT else "txt"
    with TemporaryDirectory(prefix="js-wiki-") as tmp:
        rc, out, err = run(
            ["soffice", "--headless", "--convert-to", target, "--outdir", tmp, str(p)],
            context,
        )
        converted = Path(tmp) / f"{p.stem}.{target}"
        if rc == 0 and converted.is_file():
            return read_text(converted, cap)
    return f"ERROR soffice: {err or out}"


def _convert_office(p: Path, ext: str, cap: int, context: ToolContext) -> str:
    """Convert an office or ebook file with whichever converter is installed.

    pandoc handles the markup and ebook formats; soffice handles those too plus
    the legacy and spreadsheet formats pandoc cannot read. pandoc is preferred
    when both are present, and a failed pandoc run falls back to soffice.
    """
    pandoc = _which("pandoc") if ext in PANDOC_EXT else None
    soffice = _which("soffice")
    if pandoc:
        rc, out, err = run(["pandoc", str(p), "-t", "markdown"], context)
        if rc == 0:
            return out[:cap]
        if not soffice:
            return f"ERROR pandoc: {err}"
    if soffice:
        return _convert_with_soffice(p, ext, cap, context)
    if ext in _SOFFICE_ONLY_EXT:
        return (
            f"ERROR: {ext} needs LibreOffice (`soffice`) to convert; install it or "
            "convert the file another way"
        )
    return f"ERROR: no converter installed for {ext}: install pandoc or LibreOffice (`soffice`)"


def wiki_convert(path: str, vault: str = "", context: ToolContext = None) -> str:
    assert context is not None
    p = context.resolve_path(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    ext = p.suffix.lower()
    cap = context.max_tool_result_bytes

    if ext in TEXT_EXT or ext in CODE_EXT or ext in STRUCTURED_TEXT_EXT:
        return read_text(p, cap)
    if ext == ".pdf":
        rc, out, err = run(["pdftotext", str(p), "-"], context)
        if rc == 0 and out.strip():
            return out[:cap]
        return (f"NOTE: pdftotext got no text (scanned PDF?). OCR it then re-convert:\n"
                f"  ocrmypdf '{p}' /tmp/ocr.pdf && pdftotext /tmp/ocr.pdf -\n{err}")
    if ext in OFFICE_EXT:
        return _convert_office(p, ext, cap, context)

    # media → copy to vault assets, return an Obsidian embed
    vault_path = resolve_vault(vault, context) if vault else find_vault(p)
    if ext in IMG_EXT:
        embed = "(pass vault= to copy into assets/)"
        if vault_path:
            copied = copy_to_assets(p, vault_path)
            if isinstance(copied, str):
                return copied
            embed = f"![[{copied.name}]]"
        rc, out, err = run(["tesseract", str(p), "stdout"], context)
        ocr = f"\n--- OCR (tesseract) ---\n{out.strip()}" if rc == 0 and out.strip() else ""
        return f"MEDIA image. embed: {embed}{ocr}"
    if ext in AV_EXT:
        embed = "(pass vault= to copy into assets/)"
        if vault_path:
            copied = copy_to_assets(p, vault_path)
            if isinstance(copied, str):
                return copied
            embed = f"![[{copied.name}]]"
        rc, out, err = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format=size", "-of", "default=nw=1", str(p)], context)
        return (f"MEDIA audio/video. embed: {embed}\n{out.strip()}\n"
                f"NOTE transcribe: whisper '{p}' --model small --output_format txt --output_dir /tmp")

    # fallback
    rc, out, err = run(["file", str(p)], context)
    # `file` prints "<path>: <description>" — test only the description, else a
    # binary living under a path containing "text" (e.g. .../context/...) reads as text.
    desc = out.split(":", 1)[1] if ":" in out else out
    if "text" in desc.lower():
        return read_text(p, cap)
    return f"UNREADABLE/binary: {out.strip()}  (quarantine to inbox/_skipped/ if nothing reads it)"

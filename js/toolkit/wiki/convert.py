"""wiki_convert: turn any file into text, or a media embed."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from ...capped_process import truncation_marker
from ...text_bytes import cap_text
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


# LibreOffice has no plain-text export filter for spreadsheets; the CSV filter
# takes a trailing -1 to write every sheet instead of only the first, one
# `<stem>-<sheet>.csv` each.
_SPREADSHEET_FILTER = "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true,false,false,-1"
_SHEET_LOG = re.compile(r"^Writing sheet (?P<name>.+?) -> (?P<path>.+)$", re.MULTILINE)


def _soffice_sheets(out: str, tmp: Path) -> list[tuple[str, Path]]:
    """(sheet name, csv path) for every sheet soffice wrote, in sheet order.

    LibreOffice names each sheet it writes on stdout. Whenever that log
    accounts for fewer files than soffice actually wrote — a sheet name the
    log parse cannot split, or a runner that prints nothing — the CSV files on
    disk are the source of truth, so no sheet is silently dropped.
    """
    sheets = [
        (match.group("name").strip(), Path(match.group("path").strip()))
        for match in _SHEET_LOG.finditer(out)
    ]
    sheets = [(name, path) for name, path in sheets if path.is_file()]
    written = sorted(tmp.glob("*.csv"))
    if len(sheets) == len(written):
        return sheets
    return [(path.stem, path) for path in written]


def _spreadsheet_text(out: str, tmp: Path, cap: int) -> str:
    sheets = _soffice_sheets(out, tmp)
    if not sheets:
        return "ERROR soffice: wrote no sheet"
    blocks = []
    for name, path in sheets:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: {exc}"
        blocks.append(f"--- sheet {name} ---\n{text}" if len(sheets) > 1 else text)
    return cap_text("\n".join(blocks), cap, truncation_marker(cap, "limits.max_tool_result_bytes"))


def _convert_with_soffice(p: Path, ext: str, cap: int, context: ToolContext) -> str:
    spreadsheet = ext in _SPREADSHEET_EXT
    target = _SPREADSHEET_FILTER if spreadsheet else "txt"
    with TemporaryDirectory(prefix="js-wiki-") as tmp:
        rc, out, err = run(
            ["soffice", "--headless", "--convert-to", target, "--outdir", tmp, str(p)],
            context,
        )
        if spreadsheet and rc == 0:
            return _spreadsheet_text(out, Path(tmp), cap)
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

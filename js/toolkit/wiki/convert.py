"""wiki_convert: turn any file into text, or a media embed."""
from __future__ import annotations

from pathlib import Path

from ..core import ToolContext
from .helpers import run, read_text, resolve_vault, find_vault, copy_to_assets

TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".srt", ".vtt", ".log", ".toml", ".ini", ".cfg"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".sh", ".bash", ".c", ".h", ".cpp", ".hpp", ".java", ".rb", ".php", ".lua", ".sql", ".css"}
PANDOC_EXT = {".docx", ".odt", ".rtf", ".epub", ".pptx", ".html", ".htm"}
SOFFICE_EXT = {".doc", ".ppt", ".xls", ".xlsx"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AV_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".mov", ".webm", ".avi"}


def _peek(path: Path, n: int, cap: int) -> str:
    try:
        lines, total = [], 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                total = i + 1
                if i < n:
                    lines.append(line.rstrip("\n"))
        return ("\n".join(lines) + f"\n--- ({total} lines total; first {n} shown) ---")[:cap]
    except OSError as exc:
        return f"ERROR: {exc}"


def wiki_convert(path: str, vault: str = "", context: ToolContext = None) -> str:
    assert context is not None
    p = context.resolve_path(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    ext = p.suffix.lower()
    cap = context.max_tool_result_bytes

    if ext in TEXT_EXT or ext in CODE_EXT:
        return read_text(p, cap)
    if ext in {".jsonl", ".ndjson"}:
        return _peek(p, 5, cap)
    if ext in {".json", ".csv", ".tsv", ".yaml", ".yml", ".xml"}:
        return _peek(p, 40, cap)
    if ext == ".pdf":
        rc, out, err = run(["pdftotext", str(p), "-"], context)
        if rc == 0 and out.strip():
            return out[:cap]
        return (f"NOTE: pdftotext got no text (scanned PDF?). OCR it then re-convert:\n"
                f"  ocrmypdf '{p}' /tmp/ocr.pdf && pdftotext /tmp/ocr.pdf -\n{err}")
    if ext in PANDOC_EXT:
        rc, out, err = run(["pandoc", str(p), "-t", "markdown"], context)
        return out[:cap] if rc == 0 else f"ERROR pandoc: {err}"
    if ext in SOFFICE_EXT:
        rc, out, err = run(["soffice", "--headless", "--convert-to", "txt", "--outdir", "/tmp", str(p)], context)
        txt = Path("/tmp") / (p.stem + ".txt")
        if txt.is_file():
            return read_text(txt, cap)
        return f"ERROR soffice: {err or out}"

    # media → copy to vault assets, return an Obsidian embed
    vault_path = resolve_vault(vault, context) if vault else find_vault(p)
    if ext in IMG_EXT:
        embed = f"![[{copy_to_assets(p, vault_path).name}]]" if vault_path else "(pass vault= to copy into assets/)"
        rc, out, err = run(["tesseract", str(p), "stdout"], context)
        ocr = f"\n--- OCR (tesseract) ---\n{out.strip()}" if rc == 0 and out.strip() else ""
        return f"MEDIA image. embed: {embed}{ocr}"
    if ext in AV_EXT:
        embed = f"![[{copy_to_assets(p, vault_path).name}]]" if vault_path else "(pass vault= to copy into assets/)"
        rc, out, err = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format=size", "-of", "default=nw=1", str(p)], context)
        return (f"MEDIA audio/video. embed: {embed}\n{out.strip()}\n"
                f"NOTE transcribe: whisper '{p}' --model small --output_format txt --output_dir /tmp")

    # fallback
    rc, out, err = run(["file", str(p)], context)
    if "text" in out.lower():
        return read_text(p, cap)
    return f"UNREADABLE/binary: {out.strip()}  (quarantine to inbox/_skipped/ if nothing reads it)"

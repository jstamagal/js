#!/usr/bin/env python3
"""Derive forge's `forge` agent prompt from js's shipped defaultagent prompt.

js's prompts/defaultagent/01-prompt.md is the source of truth. forge gets the
same text minus the js-harness-only sections, with forge's agent front-matter.

  derive-forge.py --ship FORK/crates/forge_repo/src/agents/forge.md
      Shipping prompt: every `!{file ~/.js/custom/<section>.md}` slot dropped.
      Front-matter is kept from the existing file at that path.

  derive-forge.py --personal ~/.forge/agents/forge.md
      Personal prompt: each slot replaced in place by that custom file, so
      personal add-ins land in the section they belong to. Front-matter is
      taken from --frontmatter-from (default: the --ship path, else the
      existing personal file).

Both may be given in one run. Never edit the outputs by hand: edit the js
prompt or ~/.js/custom/*.md and re-run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

JS_ROOT = Path(__file__).resolve().parents[1]
SOURCE = JS_ROOT / "prompts" / "defaultagent" / "01-prompt.md"
SLOT = re.compile(r"^!\{file (?P<path>[^}]+)\}\n", re.MULTILINE)
# Sections that only make sense inside the js harness.
JS_ONLY = ("## Headless mode", "## Reminders", "## `js`")
SKILLS_PARTIAL = "{{#if skills}}\n{{> forge-partial-skill-instructions.md}}\n{{else}}\n{{/if}}\n"


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise SystemExit("front-matter source has no leading --- block")
    end = text.index("\n---\n", 4)
    return text[: end + 5], text[end + 5 :]


def drop_sections(body: str) -> str:
    # Sections are separated by "\n---\n"; drop any chunk whose heading is js-only.
    chunks = body.split("\n---\n")
    kept = [c for c in chunks if not c.strip().startswith(JS_ONLY)]
    return "\n---\n".join(kept)


def forgeify(body: str) -> str:
    body = re.sub(r"\A# js — defaultagent\n\n(<!--.*?-->\n\n)?", "", body, flags=re.DOTALL)
    swaps = [
        ("\nHeadless run: body uses Headless shape (see Headless mode). Brass tacks + GATE phrase still last two lines.\n", ""),
        ("You are **js** —", "You are **forgecode** —"),
        ("`$HOME/.js/tmp`. Cleared of anything a day old.", "`/tmp`."),
        ("`$HOME/.js/work`.", "`$HOME/.forge/work`."),
        ("gh issue create -R jstamagal/js", "gh issue create -R jstamagal/forgecode"),
    ]
    for old, new in swaps:
        if old not in body:
            raise SystemExit(f"derive-forge: source text moved, update swap: {old[:60]!r}")
        body = body.replace(old, new)
    return body


def fill_slots(body: str, keep: bool) -> str:
    def sub(m: re.Match) -> str:
        if not keep:
            return ""
        path = Path(os.path.expanduser(m.group("path").strip()))
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if "{{" in text:
            raise SystemExit(f"derive-forge: {path} has '{{{{' which forge's handlebars would eat")
        return text.rstrip("\n") + "\n" if text.strip() else ""

    return SLOT.sub(sub, body)


def build(keep_custom: bool, frontmatter: str) -> str:
    body = SOURCE.read_text(encoding="utf-8")
    if "{{" in body:
        raise SystemExit("derive-forge: js prompt has '{{' which forge's handlebars would eat")
    body = fill_slots(forgeify(drop_sections(body)), keep_custom)
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")
    return f"{frontmatter}\n{body}\n\n{SKILLS_PARTIAL}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ship", type=Path, help="fork's crates/forge_repo/src/agents/forge.md")
    ap.add_argument("--personal", type=Path, help="usually ~/.forge/agents/forge.md")
    ap.add_argument("--frontmatter-from", type=Path, help="file whose --- block heads --personal")
    a = ap.parse_args()
    if not a.ship and not a.personal:
        ap.error("give --ship and/or --personal")
    if a.ship:
        fm, _ = split_frontmatter(a.ship.read_text(encoding="utf-8"))
        a.ship.write_text(build(False, fm), encoding="utf-8")
        print(f"ship     -> {a.ship}")
    if a.personal:
        src = a.frontmatter_from or a.ship or a.personal
        fm, _ = split_frontmatter(src.read_text(encoding="utf-8"))
        a.personal.parent.mkdir(parents=True, exist_ok=True)
        a.personal.write_text(build(True, fm), encoding="utf-8")
        print(f"personal -> {a.personal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

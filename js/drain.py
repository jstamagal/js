"""Mini TUI: drain a wiki inbox into the wiki, sized to what the model can hold.

A local distill (Qwen 3.6 35B A3B and friends) goes feral well before its
advertised context fills. So the unit isn't a folder and isn't even strictly a
file — it's a *budget of work*. One per-job budget drives both directions:

  - tiny files get PACKED together (2-3 little jsonls in one job), and
  - a big file gets SHATTERED into as many line-aligned pieces as it takes
    (a 10MB jsonl might become ~20 jobs).

You don't pick the budget. It's auto-derived from the model's context window
(a conservative fraction of it), with --budget to override if you ever care.
Files are walked recursively with NO filtering — `*.jsonl.deleted.*` and other
openclaw `/new` renames are real sources, not junk.

Jobs run SEQUENTIALLY: ingest mutates shared vault state (index.md, log.md,
overview.md), so parallel runs clobber each other.

Originals are LEFT WHERE THEY ARE by default — drain ingests, nothing moves.
Pass -a/--archive to move each drained original into <vault>/Clippings/ once
every job covering it has succeeded (bundles cover many files; splits cover one
file across pieces). Leave-mode is enforced on both sides: drain doesn't move
anything, and the js subprocess is told (JS_WIKI_NO_ARCHIVE) not to either.

    js-drain creative                 # ingest creative inbox, leave files in place
    js-drain creative -a              # ...and archive drained originals to Clippings/
    js-drain creative -f ~/dump       # drain an arbitrary folder, not the inbox
    js-drain general  -b 6000         # force a 6000-token/job source budget
    js-drain creative -R              # dumb: one whole file at a time, no math
    js-drain creative -n              # simulate, watch the bar, no LLM calls
"""
from __future__ import annotations

import argparse
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from . import colors as C
from . import model_metadata
from .config import from_env
from .toolkit import ToolContext
from .toolkit.wiki.helpers import resolve_vault
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BAR_WIDTH = 28
_CHARS_PER_TOKEN = 4          # rough, tokenizer-free estimate for budgeting
_FERAL_FRACTION = 0.35        # of context window — distills wander past ~half
_FALLBACK_BUDGET_TOKENS = 8000  # used only when the model context is unknown


@dataclass
class Job:
    srcs: list[Path]                  # inbox file(s) this job covers
    feed: Path                        # path handed to js (an inbox file or a temp file)
    drainer_archives: bool            # True for bundles/pieces; False when js archives
    part: int = 1
    parts: int = 1
    status: str = "pending"           # pending | running | done | failed
    rc: int | None = None
    err: str = ""
    seconds: float = 0.0

    def label(self, inbox: Path) -> str:
        head = _rel(self.srcs[0], inbox)
        if self.parts > 1:
            return f"{head} ({self.part}/{self.parts})"
        if len(self.srcs) > 1:
            return f"{head} +{len(self.srcs) - 1}"
        return head


def _rel(path: Path, inbox: Path) -> str:
    try:
        return path.relative_to(inbox).as_posix()
    except ValueError:
        return path.name


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------
def auto_budget_tokens(model: str, provider_id: str | None = None) -> int:
    """A conservative per-job source budget.

    Uses models.dev context-window metadata when available. If the active model
    is unknown to the catalog, fall back to a small conservative budget.
    ``--budget`` always overrides.
    """
    context_window = model_metadata.context_window(model, provider_id)
    if context_window is None or context_window <= 0:
        return _FALLBACK_BUDGET_TOKENS
    return max(1, int(context_window * _FERAL_FRACTION))


# --------------------------------------------------------------------------
# Job planning: walk files, pack small ones, split big ones
# --------------------------------------------------------------------------

def _readable_text(path: Path) -> str | None:
    try:
        return path.read_text("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _split_lines(text: str, budget: int) -> list[str]:
    """Greedy line-aligned split: a new piece begins once one would exceed
    budget chars. Piece count falls out of the file size — nothing fixed."""
    pieces: list[list[str]] = [[]]
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur and cur + len(line) > budget:
            pieces.append([])
            cur = 0
        pieces[-1].append(line)
        cur += len(line)
    return ["".join(p) for p in pieces if p]


def plan_jobs(inbox: Path, budget_chars: int, ralph: bool, staging: Path | None) -> list[Job]:
    if not inbox.is_dir():
        raise SystemExit(f"{C.ORANGE}no inbox at {inbox}{C.RESET}")
    files = sorted(p for p in inbox.rglob("*") if p.is_file())
    jobs: list[Job] = []

    if ralph:                       # dumb mode: every file, whole, js archives it
        return [Job(srcs=[f], feed=f, drainer_archives=False) for f in files]

    assert staging is not None
    bundle: list[Path] = []
    bundle_chars = 0
    seq = 0

    def flush() -> None:
        nonlocal bundle, bundle_chars, seq
        if not bundle:
            return
        if len(bundle) == 1:        # lone small file — let js archive from inbox
            jobs.append(Job(srcs=[bundle[0]], feed=bundle[0], drainer_archives=False))
        else:                       # several small files concatenated into one job
            cp = staging / f"bundle-{seq:04d}.md"
            seq += 1
            cp.write_text("\n\n".join(
                f"<<<< SOURCE: {_rel(f, inbox)} >>>>\n{_readable_text(f) or ''}"
                for f in bundle), "utf-8")
            jobs.append(Job(srcs=list(bundle), feed=cp, drainer_archives=True))
        bundle = []
        bundle_chars = 0

    for f in files:
        size = f.stat().st_size
        text = _readable_text(f) if size > budget_chars else None

        if text is not None and len(text) > budget_chars:   # big text: shatter
            flush()
            pieces = _split_lines(text, budget_chars)
            n = len(pieces)
            for i, piece in enumerate(pieces, 1):
                cp = staging / f"{seq:04d}-{f.stem}.{i}of{n}{f.suffix or '.txt'}"
                cp.write_text(piece, "utf-8")
                jobs.append(Job(srcs=[f], feed=cp, drainer_archives=True, part=i, parts=n))
            seq += 1     # per-source prefix: same stem in two subdirs must not collide
            continue

        if size > budget_chars:      # big binary (or undecodable): feed whole, js archives
            flush()
            jobs.append(Job(srcs=[f], feed=f, drainer_archives=False))
            continue

        if bundle_chars + size > budget_chars:   # would overflow the current bundle
            flush()
        bundle.append(f)
        bundle_chars += size

    flush()
    return jobs


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def bar(done: int, total: int, width: int = _BAR_WIDTH) -> str:
    frac = 1.0 if total == 0 else done / total
    fill = int(round(frac * width))
    return "[" + "█" * fill + "░" * (width - fill) + "]"


class Screen:
    """In-place repaint on a tty; line-per-event fallback when piped."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self._lines = 0

    def paint(self, text: str) -> None:
        if not self.tty:
            return
        if self._lines:
            sys.stdout.write(f"\033[{self._lines}A")
        sys.stdout.write("\033[J")
        sys.stdout.write(text)
        sys.stdout.flush()
        self._lines = text.count("\n")


def render(jobs: list[Job], inbox: Path, vault_name: str, modes: str, spin: str) -> str:
    total = len(jobs)
    done = sum(j.status == "done" for j in jobs)
    failed = sum(j.status == "failed" for j in jobs)
    finished = done + failed
    pct = 100 if total == 0 else int(round(100 * finished / total))
    running = next((j for j in jobs if j.status == "running"), None)

    now = ""
    if running is not None:
        now = f"{C.MAGENTA}{spin}{C.RESET} {running.label(inbox)} {C.GREY}{running.seconds:0.0f}s{C.RESET}"

    out = [
        f"{C.CYAN}draining {vault_name} inbox{C.RESET} {C.GREY}· {modes}{C.RESET}",
        "",
        f"  {bar(finished, total)}  {C.WHITE}{finished}/{total}{C.RESET}  {pct}%",
        f"  {C.GREEN}✓ {done}{C.RESET}   {C.ORANGE}✗ {failed}{C.RESET}   {now}",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _run_one(job: Job, vault: Path, modes: str, model: str | None,
             timeout: int | None, screen: Screen, inbox: Path, jobs: list[Job],
             env: dict | None = None) -> None:
    cmd = [sys.executable, "-m", "js.cli", "--wiki", modes,
           "--vault", str(vault), "-n", str(job.feed)]
    if model:
        cmd += ["-m", model]
    job.status = "running"
    spin = itertools.cycle(_SPINNER)
    start = time.time()
    with tempfile.TemporaryFile("w+b") as cap:
        proc = subprocess.Popen(cmd, stdout=cap, stderr=subprocess.STDOUT, env=env)
        while proc.poll() is None:
            job.seconds = time.time() - start
            screen.paint(render(jobs, inbox, vault.name, modes, next(spin)))
            time.sleep(0.12)
            if timeout and job.seconds > timeout:
                proc.kill()
                break
        job.seconds = time.time() - start
        job.rc = proc.returncode
        cap.seek(0)
        lines = cap.read().decode("utf-8", "replace").splitlines()
    job.err = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    job.status = "done" if job.rc == 0 else "failed"


def _simulate(job: Job, screen: Screen, inbox: Path, vault_name: str,
              modes: str, jobs: list[Job]) -> None:
    """--dry-run: fake work so you can watch the bar without LLM calls."""
    job.status = "running"
    spin = itertools.cycle(_SPINNER)
    start = time.time()
    while time.time() - start < 0.35:
        job.seconds = time.time() - start
        screen.paint(render(jobs, inbox, vault_name, modes, next(spin)))
        time.sleep(0.05)
    job.rc = 0 if hash(job.feed.name) % 6 else 1
    job.status = "done" if job.rc == 0 else "failed"
    if job.status == "failed":
        job.err = "(dry-run simulated failure)"


def _final_commit(vault: Path, modes: str) -> None:
    """One vault-level git commit at the end of a drain run. Ingest defers
    per-unit commits to dodge the parallel git-index race, so this captures
    everything the run produced. Serialized across concurrent drains by the
    wiki vault lock; no-op on a non-repo vault or when nothing is staged."""
    if not (vault / ".git").exists():
        return
    from .toolkit.wiki.helpers import vault_lock
    with vault_lock(vault):
        subprocess.run(["git", "-C", str(vault), "add", "-A"], capture_output=True)
        staged = subprocess.run(["git", "-C", str(vault), "diff", "--cached", "--quiet"])
        if staged.returncode != 0:
            subprocess.run(["git", "-C", str(vault), "commit", "-m", f"drain: {modes}"],
                           capture_output=True)


def archive_done(jobs: list[Job], inbox: Path, vault: Path) -> None:
    """Move an original into Clippings/ once every drainer-owned job covering
    it has succeeded (bundles cover many files; splits cover one across pieces)."""
    cover: dict[Path, list[Job]] = {}
    for j in jobs:
        if j.drainer_archives:
            for s in j.srcs:
                cover.setdefault(s, []).append(j)
    for src, group in cover.items():
        if not src.exists() or not all(j.status == "done" for j in group):
            continue
        try:
            dest = vault / "Clippings" / src.relative_to(inbox)
        except ValueError:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def mark_drainer_archives(jobs: list[Job], inbox: Path, force: bool = False) -> None:
    """Mark jobs whose originals must be moved by drain, not by js --wiki.

    Top-level inbox files can be archived by wiki_finish_ingest. Bundles, split
    pieces, arbitrary source folders, and nested inbox files cannot rely on that
    path, so drain owns their final move when --archive is enabled.
    """
    for job in jobs:
        if job.drainer_archives or force:
            job.drainer_archives = True
            continue
        for src in job.srcs:
            try:
                rel = src.relative_to(inbox)
            except ValueError:
                continue
            if len(rel.parts) > 1:
                job.drainer_archives = True
                break


def summarize(jobs: list[Job], inbox: Path, vault: Path, modes: str, interrupted: bool) -> int:
    done = [j for j in jobs if j.status == "done"]
    failed = [j for j in jobs if j.status == "failed"]
    left = [j for j in jobs if j.status in {"pending", "running"}]

    tail = f"  {C.GREY}· {len(left)} not reached (interrupted){C.RESET}" if left else ""
    print(f"\n{C.GREEN}✓ {len(done)} done{C.RESET}   "
          f"{C.ORANGE}✗ {len(failed)} failed{C.RESET}{tail}")
    for j in failed:
        print(f"  {C.ORANGE}✗{C.RESET} {j.label(inbox)} {C.GREY}(rc={j.rc}){C.RESET}: {j.err[:160]}")
        print(f"    {C.GREY}retry: js --wiki={modes} --vault={vault} {j.feed} -d{C.RESET}")
    return 1 if failed or interrupted else 0


def main(argv: list[str] | None = None) -> int:
    epilog = """\
examples:
  js-drain creative                     drain the creative inbox; leave files in place
  js-drain creative -a                  ...and archive drained originals to Clippings/
  js-drain creative -f ~/dump           drain an arbitrary folder, not the vault inbox
  js-drain creative -f ~/dump -a        ...and move those originals to Clippings/
  js-drain general  -b 6000             force a 6000-token/job source budget
  js-drain creative -R                  dumb mode: one whole file per job, no math
  js-drain creative -n                  simulate (no LLM calls), just watch the TUI
  js-drain creative -l 3                smoke test: run only the first 3 jobs
  js-drain ~/vaults/notes -M ingest,synthesize -t 600 -a

archiving:
  By DEFAULT drain leaves every drained file exactly where it was — it ingests,
  nothing moves. Pass -a/--archive to move each drained original into
  <vault>/Clippings/ once all jobs covering it succeed. Leave-mode is enforced
  on both sides: drain moves nothing AND the js subprocess is told not to either.

sizing:
  Local distills go feral well before their context fills, so the unit of work
  is a per-job token BUDGET, not a file. Tiny files get packed together; a big
  file gets shattered into line-aligned pieces. Budget auto-derives from the
  model's context window; -b/--budget overrides. -R/--ralph-wiggum skips all
  the math and feeds one whole file per job.
"""
    ap = argparse.ArgumentParser(
        prog="js-drain",
        description="Drain a wiki inbox (or any folder) into the wiki, sized to "
                    "what the model can actually hold.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vault", help="creative | general | path to a vault")
    ap.add_argument("-f", "--from", dest="source", metavar="DIR",
                    help="drain THIS folder instead of <vault>/inbox (walked recursively)")
    ap.add_argument("-a", "--archive", action="store_true",
                    help="move drained originals into <vault>/Clippings/ "
                         "(default: leave every file in place)")
    ap.add_argument("-M", "--modes", default="ingest", metavar="CSV",
                    help="comma list passed to --wiki (default: ingest)")
    ap.add_argument("-R", "--ralph-wiggum", action="store_true",
                    help="dumb mode: one whole file per job, no packing or splitting")
    ap.add_argument("-b", "--budget", type=int, metavar="TOKENS",
                    help="per-job source budget in tokens (default: auto from local model table)")
    ap.add_argument("-m", "--model", metavar="ID", help="override configured/env model for every job")
    ap.add_argument("-t", "--timeout", type=int, metavar="SEC",
                    help="per-job timeout in seconds (kills a hung job)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="simulate jobs (no LLM calls) to preview the TUI")
    ap.add_argument("-l", "--limit", type=int, metavar="N",
                    help="only run the first N jobs (smoke test; leaves the rest behind)")
    args = ap.parse_args(argv)
    cfg = from_env(save_session=False)
    _wiki = (getattr(cfg, "settings", {}) or {}).get("wiki")
    _aliases = _wiki.get("aliases", {}) if isinstance(_wiki, dict) and isinstance(_wiki.get("aliases"), dict) else {}
    vault = resolve_vault(args.vault, ToolContext(vault_aliases=_aliases))
    inbox = Path(args.source).expanduser().resolve() if args.source else vault / "inbox"
    staging = None if args.ralph_wiggum else Path(tempfile.mkdtemp(prefix="jsdrain-"))
    env = dict(os.environ)
    if not args.archive:
        env["JS_WIKI_NO_ARCHIVE"] = "1"
    model = args.model or cfg.model
    budget_tokens = args.budget if args.budget else auto_budget_tokens(model, cfg.provider_id)
    budget_chars = max(1, budget_tokens) * _CHARS_PER_TOKEN
    src = "set" if args.budget else "auto"
    try:
        all_jobs = plan_jobs(inbox, budget_chars, args.ralph_wiggum, staging)
        if args.archive:
            mark_drainer_archives(all_jobs, inbox, force=bool(args.source))
        if not all_jobs:
            print(f"{C.GREEN}{inbox} empty — nothing to drain.{C.RESET}")
            return 0
        jobs = all_jobs[:args.limit] if args.limit else all_jobs

        mode = "ralph-wiggum" if args.ralph_wiggum else f"~{budget_tokens} tok/job ({src})"
        arch = f"{C.ORANGE}archive→Clippings{C.RESET}" if args.archive else f"{C.GREY}leave-in-place{C.RESET}"
        print(f"{C.GREY}{len(jobs)} jobs · {mode} · {model} · {C.RESET}{arch}")

        screen = Screen()
        interrupted = False
        screen.paint(render(jobs, inbox, vault.name, args.modes, _SPINNER[0]))
        try:
            for job in jobs:
                if args.dry_run:
                    _simulate(job, screen, inbox, vault.name, args.modes, jobs)
                else:
                    _run_one(job, vault, args.modes, model, args.timeout,
                             screen, inbox, jobs, env)
                screen.paint(render(jobs, inbox, vault.name, args.modes, " "))
                if not screen.tty:
                    mark = f"{C.GREEN}✓{C.RESET}" if job.status == "done" else f"{C.ORANGE}✗{C.RESET}"
                    print(f"{mark} {job.label(inbox)} {C.GREY}{job.seconds:0.0f}s{C.RESET}"
                          + (f": {job.err[:120]}" if job.status == "failed" else ""))
        except KeyboardInterrupt:
            interrupted = True

        if args.archive and not args.dry_run:
            archive_done(all_jobs, inbox, vault)
        if not args.dry_run:
            _final_commit(vault, args.modes)
        return summarize(jobs, inbox, vault, args.modes, interrupted)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

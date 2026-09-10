#!/usr/bin/env bash
# Stand up a disposable clone of the real js repo for the tool exercise.
#
# Real code, real open bugs, real git history — but a clone, so a turn that
# writes, patches, or deletes cannot touch ~/js. The 16 benchmark turns are
# genuine audit work drawn from .scratch/tool-audit-2026-09-09.md; each one
# also happens to require a different corner of the tool surface.
set -euo pipefail

ROOT="${1:?usage: fixture.sh /path/to/workdir}"
SRC="${JS_SRC:-$HOME/js}"

trash "$ROOT" 2>/dev/null || rm -rf "$ROOT" 2>/dev/null || true
mkdir -p "$(dirname "$ROOT")"

# Clone the working tree as it stands, including uncommitted state, so the
# agent audits what is actually on disk rather than the last commit.
git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null
mkdir -p "$ROOT"
git -C "$SRC" ls-files -z | tar -C "$SRC" --null -T - -cf - | tar -C "$ROOT" -xf -
cp -r "$SRC/.git" "$ROOT/.git"

# No remotes in the clone: it carries real history and a real origin, and a
# turn that decides to push should have nowhere to push to.
for r in $(git -C "$ROOT" remote); do git -C "$ROOT" remote remove "$r"; done

# Strip .scratch from the clone. It holds tool-audit-2026-09-09.md and the
# 2026-08 issue reports -- i.e. the answers to most of the 28 audit turns. An
# agent that greps it "discovers" findings it was handed, and the run measures
# nothing. The harness itself lives there too and is not needed inside.
trash "$ROOT/.scratch" 2>/dev/null || true

# The venv is not tracked and the agent needs an interpreter to run tests.
# This is a symlink to the real one, so it is the single shared thing in an
# otherwise disposable clone: a turn that reinstalls packages would touch
# ~/js/.venv. Reading and running tests through it is safe.
if [ -d "$SRC/.venv" ]; then
  ln -s "$SRC/.venv" "$ROOT/.venv"
fi

# A PDF and a CRLF file, because two open items need them and neither is in
# the repo. Small, and clearly fixtures.
mkdir -p "$ROOT/.probe"
printf 'line one\r\nline two\r\nline three\r\n' > "$ROOT/.probe/crlf.txt"
printf 'plain\nunix\nfile\n' > "$ROOT/.probe/lf.txt"
"$SRC/.venv/bin/python" - "$ROOT/.probe/sample.pdf" <<'PY' 2>/dev/null || true
import sys, zlib
path = sys.argv[1]
body = b"BT /F1 24 Tf 72 700 Td (probe pdf) Tj ET"
objs = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
    b"/Resources << /Font << /F1 5 0 R >> >> >>",
    b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
]
out = bytearray(b"%PDF-1.4\n")
offs = []
for i, o in enumerate(objs, 1):
    offs.append(len(out))
    out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
xref = len(out)
out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
for off in offs:
    out += b"%010d 00000 n \n" % off
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
open(path, "wb").write(bytes(out))
PY

echo "$ROOT"

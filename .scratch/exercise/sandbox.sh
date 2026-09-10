#!/usr/bin/env bash
# Run a command under bubblewrap with a real filesystem jail.
#
#   sandbox.sh <workdir> <sandbox-home> <command...>
#
# The lesson this exists for: `-C` sets a working directory, it is not a
# sandbox. A bench "isolated" only by cwd still runs `read`, `shell` and
# `kernel` with full user permissions over $HOME — which is how a run confined
# to /tmp read ~/.zshrc and shipped its contents to a remote inference API.
#
# What the jailed process can see:
#   rw   the work dir, and a throwaway HOME created per run
#   ro   /usr /etc, the js source under audit, the uv tool install
#   none everything else under /home, including ~/.ssh, ~/.config/js/logins.toml,
#        ~/.zshrc, ~/.aws, browser profiles, other checkouts
set -euo pipefail

WORK="${1:?workdir}"; shift
SBHOME="${1:?sandbox home}"; shift

JS_SRC="${JS_SRC:-$HOME/js}"
UV_TOOL="$HOME/.local/share/uv/tools/js"
# The tool venv's bin/python is a symlink into uv's managed interpreters,
# and ~/js/.venv points there too. Bind it or nothing executes.
UV_PY="$HOME/.local/share/uv/python"

[ -d "$JS_SRC" ]  || { echo "sandbox: no js source at $JS_SRC" >&2; exit 2; }
[ -d "$UV_TOOL" ] || { echo "sandbox: no uv tool install at $UV_TOOL" >&2; exit 2; }

mkdir -p "$SBHOME"

exec bwrap \
  --ro-bind /usr /usr \
  --ro-bind /etc /etc \
  --symlink usr/bin  /bin \
  --symlink usr/bin  /sbin \
  --symlink usr/lib  /lib \
  --symlink usr/lib32 /lib32 \
  --symlink usr/lib  /lib64 \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --tmpfs /run \
  --tmpfs /var \
  --tmpfs /home \
  --bind "$WORK" "$WORK" \
  --bind "$SBHOME" "$SBHOME" \
  --ro-bind "$JS_SRC" "$JS_SRC" \
  --ro-bind "$UV_TOOL" "$UV_TOOL" \
  --ro-bind "$UV_PY" "$UV_PY" \
  --setenv HOME "$SBHOME" \
  --setenv XDG_CONFIG_HOME "$SBHOME/.config" \
  --setenv XDG_DATA_HOME "$SBHOME/.local/share" \
  --setenv XDG_CACHE_HOME "$SBHOME/.cache" \
  --setenv TMPDIR "$WORK/.tmp" \
  --unsetenv DEEPSEEK_API_KEY \
  --unsetenv OPENROUTER_API_KEY \
  --unsetenv OPENCODE_GO_API_KEY \
  --unsetenv NINEROUTER_API_KEY \
  --unsetenv HF_TOKEN \
  --unsetenv GH_TOKEN \
  --unsetenv GITHUB_TOKEN \
  --share-net \
  --die-with-parent \
  --new-session \
  --chdir "$WORK" \
  "$@"

#!/usr/bin/env bash
# Run one command in the js-toolbench image with the current directory mounted
# at /workspace. RepoRacer calls this for the install, agent, and test steps of
# every task, so all three see the same toolchains and the same worktree.
#
#   sandbox.sh [--prompt FILE] [--telemetry DIR] -- COMMAND [ARGS...]
#
# Env in:  TOOLBENCH_IMAGE, TOOLBENCH_CPUS, TOOLBENCH_MEMORY; the JS_* model
# settings and TOOLBENCH_HIDE_FILES are forwarded into the container.
set -euo pipefail
image="${TOOLBENCH_IMAGE:-js-toolbench:latest}"
cpus="${TOOLBENCH_CPUS:-2}"
memory="${TOOLBENCH_MEMORY:-4g}"
prompt=""
telemetry=""
while [ $# -gt 0 ]; do
    case "$1" in
        --prompt) prompt="$2"; shift 2 ;;
        --telemetry) telemetry="$2"; shift 2 ;;
        --) shift; break ;;
        *) echo "sandbox.sh: unknown option $1" >&2; exit 2 ;;
    esac
done
[ $# -gt 0 ] || { echo "sandbox.sh: no command" >&2; exit 2; }

name="js-toolbench-$$-$RANDOM"
args=(run --rm --init --name "$name"
      --cap-drop ALL --security-opt no-new-privileges --pids-limit 1024
      --cpus "$cpus" --memory "$memory"
      --user "$(id -u):$(id -g)"
      -v "$PWD:/workspace" -w /workspace
      -v js-toolbench-cache:/cache)
# A RepoRacer worktree's .git is a file pointing at the parent clone's .git
# directory. Mount that directory at its own path so git works in the sandbox.
if [ -f "$PWD/.git" ]; then
    gitdir=$(sed -n 's/^gitdir: //p' "$PWD/.git")
    common="${gitdir%/worktrees/*}"
    if [ -d "$common" ]; then args+=(-v "$common:$common"); fi
fi
if [ -n "$prompt" ]; then
    args+=(-v "$(realpath "$prompt"):/prompt.md:ro")
fi
if [ -n "$telemetry" ]; then
    mkdir -p "$telemetry"
    args+=(-v "$(realpath "$telemetry"):/telemetry")
fi
for var in JS_PROVIDER JS_BASE_URL JS_API_KEY JS_MODEL JS_REASONING TOOLBENCH_HIDE_FILES; do
    if [ -n "${!var:-}" ]; then args+=(-e "$var"); fi
done

# RepoRacer enforces its timeout by killing this process; take the container
# down with it so a hung agent does not keep the model busy.
trap 'docker kill "$name" >/dev/null 2>&1 || true' TERM INT HUP
docker "${args[@]}" "$image" "$@" &
wait $!

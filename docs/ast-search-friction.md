Found while using `ast_search` for a real read-only job: comparing four C++/CUDA engines (Strata, basalt, capy-flash, infernix) on darkstar, 2026-11-12, ast-grep 0.45.3. Paths are under `/home/ronald_rump/engines`.

**Scoreboard, 30 calls:** 13 returned what was asked, 13 returned `(no matches)` when matches exist, 1 returned `(no matches)` correctly, 3 returned junk that looked like results. A false empty is indistinguishable from a true one.

## Wrong or missing results

1. **`.h` C++ headers are parsed as C when `lang` is omitted.**
   Input: `pattern="enum class $NAME : $T { $$$M }"`, `path="infernix/include/infernix/types.h"`.
   Expected: the file's scoped enums. Got: about 450 lines of "matches" at lines 381, 1057, 1207, 1333, 1865, 2104, 2375 that run far past each enum (one is 303 lines), with lines 2104-2166 printed twice. `lang="Cpp"` on the same file is correct.

2. **`lang="Cpp"` on a directory skips every file whose extension ast-grep does not map to C++** (`.h`, `.cuh`, `.inc`).
   Input: same pattern, `path="infernix/include/infernix"`, `lang="Cpp"`. Expected: enums from every header. Got: `(no matches)`.

3. **Unknown extensions are never searched in a directory.**
   Input: `pattern="__shfl_xor_sync($$$A)"`, `path="infernix/src/ops/kernel"` (33 `.cuh` files), with and without `lang="Cpp"`.
   Expected: 5 hits in 3 files (`rg`). Got: `(no matches)` both times. A single `.cuh` file with `lang="Cpp"` works; without `lang` it is `(no matches)`.

4. **A pattern that parses to ERROR, or to another node kind, returns `(no matches)` with no warning.**
   - `__global__ void $NAME($$$ARGS) { $$$BODY }`, `lang="Cpp"`: expected 13 kernels, got nothing. `--debug-query=ast` shows `declaration` + `ERROR`.
   - `std::getenv($A)`, `std::getenv($$$A)`, `std::getenv($A);` on a `.hpp`: expected 2 calls, got nothing three times. The pattern parses as a `declaration`. The description documents this trap for `lang="C"` only, and its semicolon fix does not help here.
   The description says ast-grep's warnings are shown above the matches; none appeared.

5. **Mixed-language directory, no `lang`:** `pattern="def _ast_argv"`, `path="/home/ronald_rump/js/js"` returned three prose paragraphs from `skills/wayfinder/SKILL.md`. With `lang="Python"` it found `toolkit/fs.py:1674`.

## Output

6. No cap on one match: the whole node is printed (one enum was 214 lines; finding 1 was about 450).
7. Results come in walk order, interleaved across directories; truncation keeps an arbitrary subset and does not say how many were omitted.
8. A one-line hit prints two lines (absolute `path:line` heading, then `line|code`).
9. Captured metavariables are not shown.

## Missing

10. No count / files-only / captures-only output; one `path` per call; no include or exclude globs (`_ast_argv` already takes `excludes`).
11. No `kind`, no metavariable regex, no `selector`/`context`: "every function whose name contains accept" or "any call to a qualified function" cannot be asked.

## Description

It explains a Python `def` retry, NFS automounts and `--one-file-system`, and does not say which extensions map to which language, that `lang` filters a directory by extension, or that an unrecognised extension is not searched.

## What would have fixed most of it

- A status line on every result: `searched 596 files as Cpp, skipped 882 (.h as C: 596, .cuh unknown: 286)`.
- When `lang` is set, parse everything under `path` as that language (stdin per file, or a generated `sgconfig.yml` with `languageGlobs`).
- Check the pattern with `--debug-query=ast` first and report an `ERROR` root, or a `declaration` root for text shaped like a call.
- Cap each match with `... +N lines`; sort and group by file; relative paths; `40 of 213`.

## Where it worked

- Call sites across `.cpp`, `.hpp` and `.cu`: `cudaGraphInstantiate($$$A)` per engine gave 32 / 7 / 1 / 1 sites with no comment or log-string noise.
- `cudaGraphConditionalHandleCreate($$$A)` with `lang="Cpp"` over all four repos found exactly two files.
- `.cu` files are picked up as C++ with no help.
- A single file with an unknown extension and `lang` set is searched correctly.

Strings and inline assembly are invisible by design (PTX `mma.sync`, names inside `getenv("...")`); the description could say to use `rg` for those.

Not tested: `rewrite` / `apply`.

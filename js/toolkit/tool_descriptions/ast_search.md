Search source code structurally with ast-grep 0.45.1.

Use `ast_search` instead of `fs_search` when syntax matters: it matches parsed
code across whitespace and line breaks and does not mistake comments or string
literals for code; use `fs_search` for exact text, regexes, filenames, and globs.

A metavariable is a placeholder inside a pattern. `$NAME` captures one syntax
node, and `$$$ARGS` captures zero or more nodes, so `call($$$ARGS)` finds calls
with any number of arguments. Reusing a metavariable name requires the captured
syntax to be equal. Metavariables must be uppercase.

`path` is a file or directory and defaults to the current working directory.
ast-grep infers the language from source extensions. Set `lang` for an override,
especially for `.txt` files; supported values are Bash, C, Cpp, CSharp, Css,
Dart, Elixir, Go, Haskell, Hcl, Html, Java, JavaScript, Json, Kotlin, Lua,
Markdown, Nix, Php, Python, Ruby, Rust, Scala, Solidity, Swift, Tsx, TypeScript,
and Yaml.

Search results have an absolute `path:line` heading followed by read-compatible
anchored source lines such as `12ab|code`. `max_results` defaults to 100 matches.

Rewriting is deliberately two-step. Set `rewrite` to request a structural
replacement; the default `apply=false` is a dry run that shows a unified diff
and changes nothing. Set `apply=true` to mutate after reviewing the diff. An
apply snapshots every affected file for `undo`, clears cached searches, and
refuses to run if the match set exceeds `max_results`.

Worked examples (captured from this repository):

1. A call with one metavariable:

   `pattern="context.snapshot($TARGET)", path="js/toolkit/fs.py", lang="Python", max_results=1`

   ```text
   /home/ronald_rump/oldinbox/wt-astgrep/js/toolkit/fs.py:84
   84:aa|    context.snapshot(target)
   ```

2. A call with any argument count:

   `pattern="fs_search($$$ARGS)", path="tests/test_fs_search.py", lang="Python", max_results=2`

   ```text
   /home/ronald_rump/oldinbox/wt-astgrep/tests/test_fs_search.py:47
   47e0|    actual = fs_search("TaskRunner", path=".", output_mode="content", context=context)
   /home/ronald_rump/oldinbox/wt-astgrep/tests/test_fs_search.py:58
   58cc|    actual = fs_search("needle", path=".", context=context)
   ```

3. A dry-run structural rewrite:

   `pattern="int($VALUE)", path="js/toolkit/sanitize.py", lang="Python", rewrite="float($VALUE)", max_results=2`

   ```text
   DRY RUN: no files changed. Pass apply=true to apply.
   --- /home/ronald_rump/oldinbox/wt-astgrep/js/toolkit/sanitize.py
   +++ /home/ronald_rump/oldinbox/wt-astgrep/js/toolkit/sanitize.py
   @@ -15,7 +15,7 @@
        if raw is None or isinstance(raw, bool):
            return default
        try:
   -        value = int(raw)
   +        value = float(raw)
        except (TypeError, ValueError):
            return default
        if minimum is not None and value < minimum:
   ```

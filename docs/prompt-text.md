# Model-facing text catalogs

`js/model_text/*.json` holds exact text for the parts of js. Each catalog is
loaded by `js.prompt_text.load_catalog` and shared with its owning module.
An assembly can replace catalog entries before importing the owning part;
shipped files provide the defaults. Agent discovery ignores these catalogs
because the directory contains neither Markdown persona text nor a manifest.
The existing `prompts` package-data rule includes the catalogs in wheels.

Literal entries preserve whitespace. Templates use Python `str.format` fields;
the owning part supplies the data, and inserted values are never expanded
again. Protocol keys, parsing markers and control flow remain in Python.
Changing a template does not change when it is sent or the policy that selects it.

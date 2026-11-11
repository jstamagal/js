Change one cell of a Jupyter notebook (.ipynb) by its id: `replace` its
source (the default), `insert` a new cell after it, or `delete` it. Read the
notebook first; `read` shows each cell's id as `[cell N id=ID type]`. A notebook
that changed on disk since the read is refused; read it again.

`new_source` is the cell's whole new source. `insert` needs `cell_type`; with
no `cell_id` it inserts at the top. A replaced code cell loses its outputs and
execution count, since they no longer match its source. A notebook older than
nbformat 4.5 initially uses `cell-0`, `cell-1`, ... by position (with a suffix
if a name is already taken). The first edit persists those ids and upgrades
nbformat 4 to minor version 5, so later edits keep addressing the same cells. `undo` restores
the notebook as it was before the edit.

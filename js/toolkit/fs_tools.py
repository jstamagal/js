"""The fs suite: the file tools' schemas wired to their handlers, and the
path-skill offer that read, write and patch results carry."""

from __future__ import annotations

import functools
import os
from pathlib import Path

from .core import Tool
from .descriptions import load_description, load_parameter_description
from .file_search import fs_search
from .fs import fs_read, prepare_read_arguments, remove, undo, write
from .patch import patch
from .presentation import Presentation


def _offering_path_skills(handler):
    """Wrap a read, patch or write handler so a successful call on a file that
    matches a skill's ``paths:`` starts with that skill's one-time offer.

    The offer goes first because the runtime keeps the head of a result it
    spills or clips; the offer is spent when made, so it must be delivered."""

    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        result = handler(*args, **kwargs)
        context = kwargs.get("context")
        raw = kwargs.get("file_path") or kwargs.get("path")
        registry = None if context is None else context.tool_registry
        if not raw or registry is None or not isinstance(result, str) or result.startswith("ERROR"):
            return result
        path = Path(os.path.expanduser(str(raw)))
        reminder = registry.offer_path_skills(path if path.is_absolute() else context.cwd / path, context.cwd)
        return f"{reminder}\n{result}" if reminder else result

    return wrapped


def tools(*, result_wrapper=_offering_path_skills) -> tuple[Tool, ...]:
    return (
        Tool(
            "read",
            load_description("read"),
            result_wrapper(fs_read),
            {
                "file_path": {"type": "string", "description": load_parameter_description('read', 'file_path')},
                "start_line": {"type": "integer", "description": load_parameter_description('read', 'start_line')},
                "end_line": {"type": "integer", "description": load_parameter_description('read', 'end_line')},
                "show_line_numbers": {"type": "boolean", "default": True, "description": load_parameter_description('read', 'show_line_numbers')},
                "start_byte": {"type": "integer", "description": load_parameter_description('read', 'start_byte')},
                "end_byte": {"type": "integer", "description": load_parameter_description('read', 'end_byte')},
            },
            required=("file_path",),
            read_only=True,
            prepare_args=prepare_read_arguments,
            presentation=Presentation(unlisted=("range", "start_line", "end_line", "start_byte", "end_byte"),
                                      note="range", result="file", fold_noun="file"),
        ),
        Tool(
            "write",
            load_description("write"),
            result_wrapper(write),
            {
                "file_path": {"type": "string", "description": load_parameter_description('write', 'file_path')},
                "content": {"type": "string", "description": load_parameter_description('write', 'content')},
                "overwrite": {"type": "boolean", "default": False, "description": load_parameter_description('write', 'overwrite')},
                "expected_hash": {"type": "string", "description": load_parameter_description('write', 'expected_hash')},
                "from_held": {"type": "string", "description": load_parameter_description('write', 'from_held')},
                "append": {"type": "boolean", "default": False, "description": load_parameter_description('write', 'append')},
            },
            required=("file_path",),
            presentation=Presentation(body="content", body_is_file=True, note="lines", kind="diff"),
        ),
        Tool(
            "fs_search",
            load_description("fs_search"),
            fs_search,
            {
                "pattern": {"type": "string", "description": load_parameter_description('fs_search', 'pattern')},
                "path": {"type": "string", "description": load_parameter_description('fs_search', 'path')},
                "glob": {"type": "string", "description": load_parameter_description('fs_search', 'glob')},
                "output_mode": {"type": "string", "enum": ["files", "content", "files_with_matches", "count"], "default": "files_with_matches", "description": load_parameter_description('fs_search', 'output_mode')},
                # The handler also takes ripgrep's short spellings (-B/-A/-C/-n/-i and
                # `type`) through **rg_flags, but only the readable names are declared:
                # the schema is what the model reads, and declaring both doubled it.
                "before_context": {"type": "integer", "description": load_parameter_description('fs_search', 'before_context')},
                "after_context": {"type": "integer", "description": load_parameter_description('fs_search', 'after_context')},
                "context_lines": {"type": "integer", "description": load_parameter_description('fs_search', 'context_lines')},
                "show_line_numbers": {"type": "boolean", "default": True, "description": load_parameter_description('fs_search', 'show_line_numbers')},
                "case_insensitive": {"type": "boolean", "default": False, "description": load_parameter_description('fs_search', 'case_insensitive')},
                "file_type": {"type": "string", "description": load_parameter_description('fs_search', 'file_type')},
                "head_limit": {"type": "integer", "description": load_parameter_description('fs_search', 'head_limit')},
                "offset": {"type": "integer", "description": load_parameter_description('fs_search', 'offset')},
                "multiline": {"type": "boolean", "default": False, "description": load_parameter_description('fs_search', 'multiline')},
            },
            required=("pattern",),
            read_only=True,
            presentation=Presentation(key=("pattern",), fold_noun="search"),
        ),
        Tool("remove", load_description("remove"), remove, {"path": {"type": "string", "description": load_parameter_description('remove', 'path')}, "permanent": {"type": "boolean", "default": False, "description": load_parameter_description('remove', 'permanent')}}, required=("path",)),
        Tool(
            "patch",
            load_description("patch"),
            result_wrapper(patch),
            {
                "file_path": {"type": "string", "description": load_parameter_description('patch', 'params.file_path')},
                "old_string": {"type": "string", "description": load_parameter_description('patch', 'params.old_string')},
                "new_string": {"type": "string", "description": load_parameter_description('patch', 'params.new_string')},
                "replace_all": {"type": "boolean", "default": False, "description": load_parameter_description('patch', 'params.replace_all')},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string", "description": load_parameter_description('patch', 'params.edits.items.old_string')},
                            "new_string": {"type": "string", "description": load_parameter_description('patch', 'params.edits.items.new_string')},
                            "replace_all": {"type": "boolean", "default": False, "description": load_parameter_description('patch', 'params.edits.items.replace_all')},
                        },
                        "required": ["old_string", "new_string"],
                        "additionalProperties": False,
                    },
                    "description": load_parameter_description('patch', 'params.edits'),
                },
            },
            input_schema={
                # Flat, not oneOf: llama.cpp's schema-to-grammar path emits {} for a
                # bare oneOf with no top-level properties (same lesson as the task schema in subagents._task_params).
                # patch() rejects mixing the scalar and batch forms.
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": load_parameter_description('patch', 'file_path')},
                    "old_string": {"type": "string", "description": load_parameter_description('patch', 'old_string')},
                    "new_string": {"type": "string", "description": load_parameter_description('patch', 'new_string')},
                    "replace_all": {"type": "boolean", "default": False, "description": load_parameter_description('patch', 'replace_all')},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string", "description": load_parameter_description('patch', 'edits.items.old_string')},
                                "new_string": {"type": "string", "description": load_parameter_description('patch', 'edits.items.new_string')},
                                "replace_all": {"type": "boolean", "default": False, "description": load_parameter_description('patch', 'edits.items.replace_all')},
                            },
                            "required": ["old_string", "new_string"],
                            "additionalProperties": False,
                        },
                        "description": load_parameter_description('patch', 'edits'),
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
            presentation=Presentation(unlisted=("old_string", "new_string", "edits"), note="edits",
                                      kind="diff", result="diff"),
        ),
        Tool("undo", load_description("undo"), undo, {"path": {"type": "string", "description": load_parameter_description('undo', 'path')}}, required=("path",)),
    )

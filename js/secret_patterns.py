"""The shared secret-patterns file: which tokens are secrets.

envctx's history scrubber (``tools/envctx.c``) and the ``-u`` key detector
both read it, so a rule added there teaches both. The format is documented
at the top of ``defaults/secret-patterns``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import paths

FILE_NAME = "secret-patterns"
# The file envctx reads; js exports it before expanding a prompt.
ENV_VAR = "JS_SECRET_PATTERNS"


class SecretPatternsError(ValueError):
    """A secret-patterns file js cannot read, naming the file and line."""


@dataclass(frozen=True)
class PrefixRule:
    prefix: str
    min: int
    max: int
    chars: frozenset[str]


@dataclass(frozen=True)
class EntropyRule:
    min: int
    kinds: int
    chars: frozenset[str]


@dataclass(frozen=True)
class ExemptRule:
    length: int
    chars: frozenset[str]


@dataclass(frozen=True)
class SecretPatterns:
    prefixes: tuple[PrefixRule, ...]
    entropy: tuple[EntropyRule, ...]
    exempt: tuple[ExemptRule, ...]

    def prefix(self, token: str) -> str | None:
        """The longest prefix rule matching ``token``, or None."""
        best: str | None = None
        for rule in self.prefixes:
            if best is not None and len(rule.prefix) <= len(best):
                continue
            if not token.startswith(rule.prefix):
                continue
            rest = token[len(rule.prefix):]
            if rule.min <= len(rest) <= rule.max and set(rest) <= rule.chars:
                best = rule.prefix
        return best

    def high_entropy(self, token: str) -> bool:
        chars = set(token)
        if any(len(token) == rule.length and chars <= rule.chars for rule in self.exempt):
            return False
        if not any(c.isdigit() for c in token):
            return False
        kinds = (
            any(c.islower() for c in token)
            + any(c.isupper() for c in token)
            + any(c.isdigit() for c in token)
        )
        return any(
            len(token) >= rule.min and kinds >= rule.kinds and chars <= rule.chars
            for rule in self.entropy
        )

    def is_secret(self, token: str) -> bool:
        return self.prefix(token) is not None or self.high_entropy(token)


def patterns_file() -> Path:
    """``~/.js/secret-patterns`` when it exists, else the stock defaults file."""
    return paths.home_or_default(FILE_NAME)


def export_env() -> None:
    """Name the file for envctx, which runs as a child of js."""
    os.environ[ENV_VAR] = str(patterns_file())


def _charset(spec: str) -> frozenset[str]:
    out: set[str] = set()
    i = 0
    while i < len(spec):
        if i + 2 < len(spec) and spec[i + 1] == "-":
            lo, hi = spec[i], spec[i + 2]
            if hi < lo:
                raise ValueError(f"backwards range: {spec}")
            out.update(chr(c) for c in range(ord(lo), ord(hi) + 1))
            i += 3
        else:
            out.add(spec[i])
            i += 1
    return frozenset(out)


def parse(text: str, source: str) -> SecretPatterns:
    classes: dict[str, frozenset[str]] = {}
    prefixes: list[PrefixRule] = []
    entropy: list[EntropyRule] = []
    exempt: list[ExemptRule] = []

    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue

        def fail(what: str) -> SecretPatternsError:
            return SecretPatternsError(f"{source}:{number}: {what}")

        def num(value: str) -> int:
            if not (value.isascii() and value.isdigit()):
                raise fail(f"not a number: {value}")
            return int(value)

        def cls(name: str) -> frozenset[str]:
            if name not in classes:
                raise fail(f"unknown class: {name}")
            return classes[name]

        kind, args = fields[0], fields[1:]
        shapes = {"class": 2, "prefix": 4, "entropy": 3, "exempt": 2}
        if kind not in shapes:
            raise fail(f"unknown rule: {kind}")
        if len(args) != shapes[kind]:
            raise fail(f"{kind} takes {shapes[kind]} fields, got {len(args)}")
        if kind == "class":
            try:
                classes[args[0]] = _charset(args[1])
            except ValueError as exc:
                raise fail(str(exc)) from None
        elif kind == "prefix":
            prefixes.append(PrefixRule(args[0], num(args[1]), num(args[2]), cls(args[3])))
        elif kind == "entropy":
            entropy.append(EntropyRule(num(args[0]), num(args[1]), cls(args[2])))
        else:
            exempt.append(ExemptRule(num(args[0]), cls(args[1])))

    return SecretPatterns(tuple(prefixes), tuple(entropy), tuple(exempt))


_cache: dict[tuple[str, int], SecretPatterns] = {}


def load(path: Path | None = None) -> SecretPatterns:
    path = path or patterns_file()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns)
        if key not in _cache:
            _cache[key] = parse(path.read_text(encoding="utf-8"), str(path))
        return _cache[key]
    except OSError as exc:
        raise SecretPatternsError(f"{path}: cannot read the secret-patterns file: {exc.strerror}") from None


def is_secret(token: str) -> bool:
    return load().is_secret(token)

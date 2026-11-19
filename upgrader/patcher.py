"""Surgical source patching: replace ONE function's body, keep every other byte identical.

The Upgrader never rewrites a file. It locates the target function with the AST,
swaps the lines of its body (docstring included) and leaves the signature, the
rest of the module, comments and trailing whitespace untouched. The result is a
minimal unified diff a human can review in the Beekeeper's approval modal.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from dataclasses import dataclass

MAX_DIFF_LINES = 50  # guardrail from the design doc: "one diff per upgrade, under ~50 lines"


class PatchError(ValueError):
    """The patch cannot be produced safely (function missing, syntax error, diff too big...)."""


@dataclass(frozen=True)
class FunctionSite:
    """Where a top-level function lives in the source (1-based, inclusive line numbers)."""

    name: str
    def_line: int        # first line of `def ...` (after decorators)
    body_start: int      # first line of the body (docstring or first statement)
    end_line: int        # last line of the function
    indent: str          # indentation of the body
    arg_name: str        # name of the first positional parameter
    docstring: str | None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as e:  # never patch a file we cannot parse
        raise PatchError(f"source does not parse: {e}") from e


def locate_function(source: str, name: str, tree: ast.Module | None = None) -> FunctionSite:
    """Find a *module-level* function by name. Raises PatchError if absent or ambiguous."""
    tree = tree or parse(source)
    hits = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not hits:
        raise PatchError(f"function {name}() not found at module level")
    if len(hits) > 1:
        raise PatchError(f"function {name}() is defined {len(hits)} times; refusing to guess")
    fn = hits[0]
    first = fn.body[0]
    if first.lineno == fn.lineno:
        raise PatchError(f"{name}() is a one-line def; refusing to rewrite its signature")
    lines = source.splitlines(keepends=True)
    body_line = lines[first.lineno - 1]
    indent = body_line[: len(body_line) - len(body_line.lstrip())]
    args = fn.args.posonlyargs + fn.args.args
    if not args:
        raise PatchError(f"{name}() takes no positional argument")
    return FunctionSite(name=name, def_line=fn.lineno, body_start=first.lineno, end_line=fn.end_lineno,
                        indent=indent, arg_name=args[0].arg, docstring=ast.get_docstring(fn))


def replace_body(source: str, site: FunctionSite, new_body: str) -> str:
    """Return `source` with lines body_start..end_line replaced by `new_body`.

    `new_body` is written without indentation; each non-empty line is indented with
    the function's own body indent. Everything outside that line range is untouched.
    """
    lines = source.splitlines(keepends=True)
    old_tail = lines[site.end_line - 1]
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"  # keep the file's line endings
    body = [(site.indent + ln if ln.strip() else "") + newline for ln in new_body.strip("\n").split("\n")]
    if not old_tail.endswith(("\n", "\r")):  # file ended without a newline: keep it that way
        body[-1] = body[-1].rstrip("\r\n")
    patched = "".join(lines[: site.body_start - 1] + body + lines[site.end_line:])
    parse(patched)  # must still be valid Python
    return patched


def unified_diff(before: str, after: str, path: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


def diff_line_count(diff: str) -> int:
    return len(diff.splitlines())


def changed_outside(before: str, after: str, site: FunctionSite) -> bool:
    """True if anything outside the function body changed (used by tests and as a self-check)."""
    b = before.splitlines(keepends=True)
    a = after.splitlines(keepends=True)
    tail = len(b) - site.end_line
    return b[: site.body_start - 1] != a[: site.body_start - 1] or b[len(b) - tail:] != a[len(a) - tail:]

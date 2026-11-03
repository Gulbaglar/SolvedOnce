#!/usr/bin/env python3
"""
Strict, dependency-free unified-diff applier used by the SolvedOnce runner.

Strict means: every context and removed line must match exactly at the
position the hunk header gives. No fuzz, no offset search, no creating or
deleting files, no paths outside the root. If anything does not match, the
whole patch is rejected and nothing is written.

Usage:
    python apply_patch.py ROOT_DIR PATCH_FILE

Exit codes: 0 applied, 1 rejected, 2 usage error.
License: MIT
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


def _strip_prefix(path: str) -> str:
    path = path.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _safe_target(root: Path, rel: str) -> Path:
    if not rel or rel == "/dev/null":
        raise PatchError("creating or deleting files is not allowed")
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise PatchError(f"path escapes the target tree: {rel!r}")
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise PatchError(f"path escapes the target tree: {rel!r}")
    if not target.is_file():
        raise PatchError(f"file to patch does not exist: {rel!r}")
    return target


def parse(patch_text: str) -> list[tuple[str, list[tuple[int, int, list[str]]]]]:
    """Return [(relative_path, [(old_start, old_len, hunk_lines), ...]), ...]."""
    lines = patch_text.splitlines()
    files: list[tuple[str, list[tuple[int, int, list[str]]]]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("'---' header without '+++' header")
            old = _strip_prefix(lines[i][4:])
            new = _strip_prefix(lines[i + 1][4:])
            if old != new:
                raise PatchError(f"renames are not allowed ({old!r} -> {new!r})")
            hunks: list[tuple[int, int, list[str]]] = []
            i += 2
            while i < len(lines) and lines[i].startswith("@@"):
                m = HUNK.match(lines[i])
                if not m:
                    raise PatchError(f"bad hunk header: {lines[i]!r}")
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                i += 1
                body: list[str] = []
                o = n = 0
                while i < len(lines) and (o < old_len or n < new_len):
                    line = lines[i]
                    if line.startswith("\\"):
                        i += 1
                        continue
                    tag = line[:1] if line else " "
                    if tag == " ":
                        o += 1
                        n += 1
                    elif tag == "-":
                        o += 1
                    elif tag == "+":
                        n += 1
                    else:
                        raise PatchError(f"unexpected line in hunk: {line!r}")
                    body.append(line if line else " ")
                    i += 1
                if (o, n) != (old_len, new_len):
                    raise PatchError("hunk body does not match its header line counts")
                hunks.append((old_start, old_len, body))
            if not hunks:
                raise PatchError(f"no hunks for {old!r}")
            files.append((old, hunks))
        else:
            i += 1
    if not files:
        raise PatchError("no file sections found")
    return files


def apply(root: Path, patch_text: str) -> list[str]:
    """Apply strictly. Returns the list of changed files. Writes nothing on failure."""
    root = root.resolve()
    results: list[tuple[Path, str, bool]] = []
    for rel, hunks in parse(patch_text):
        target = _safe_target(root, rel)
        raw = target.read_bytes().decode("utf-8")
        ends_with_newline = raw.endswith("\n")
        crlf = "\r\n" in raw
        src = raw.replace("\r\n", "\n").split("\n")
        if ends_with_newline:
            src = src[:-1]
        out: list[str] = []
        pos = 0  # 0-based index into src
        for old_start, old_len, body in hunks:
            start = old_start - 1 if old_len else old_start
            if start < pos:
                raise PatchError(f"{rel}: overlapping or unordered hunks")
            out.extend(src[pos:start])
            cur = start
            for line in body:
                tag, text = line[0], line[1:]
                if tag in " -":
                    if cur >= len(src) or src[cur] != text:
                        got = src[cur] if cur < len(src) else "<end of file>"
                        raise PatchError(f"{rel}:{cur + 1}: context mismatch: expected {text!r}, found {got!r}")
                    if tag == " ":
                        out.append(text)
                    cur += 1
                else:
                    out.append(text)
            pos = cur
        out.extend(src[pos:])
        nl = "\r\n" if crlf else "\n"
        new = nl.join(out) + (nl if ends_with_newline else "")
        results.append((target, new, True))
    for target, new, _ in results:  # write only after every file applied cleanly
        target.write_bytes(new.encode("utf-8"))
    return [str(t.relative_to(root)) for t, _, _ in results]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        sys.stderr.write("usage: apply_patch.py ROOT_DIR PATCH_FILE\n")
        return 2
    root, patch_file = Path(argv[0]), Path(argv[1])
    try:
        changed = apply(root, patch_file.read_text(encoding="utf-8"))
    except (PatchError, OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"patch rejected: {exc}\n")
        return 1
    for c in changed:
        print(f"patched {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

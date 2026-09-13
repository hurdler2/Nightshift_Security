#!/usr/bin/env python3
"""Pre-commit guard: no XVR credentials or credentialed stream URLs in the tree.

Spec §33/§46 make this a hard rule, and a leaked device password is not something a
code review reliably catches. Test fixtures may use obvious placeholders.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "credentialed rtsp/http URL",
        re.compile(r"(rtsp|http)s?://[A-Za-z0-9._%-]+:[^@/\s\"']+@", re.IGNORECASE),
    ),
    (
        "hard-coded dahua password",
        re.compile(r"(?i)(dahua|xvr)_?password\s*[=:]\s*[\"'][^\"']+[\"']"),
    ),
    (
        "curl --digest with inline password",
        re.compile(r"(?i)--digest\s+-u\s+['\"]?[^\s'\"]+:(?!PASS\b)[^\s'\"]+"),
    ),
]

#: Documented placeholders that are safe by construction.
ALLOWED = re.compile(
    r"(?i)(USER:PASS|<USER>:<PASS>|user:pass@|u:p@|username:password|\*\*\*|"
    r"nightshift:p%40ss|nightshift:sup3r|nightshift:secret-pass|nightshift:hunter2|"
    r"user:pw@|USER:PASSWORD)"
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "probe-reports",
}
SKIP_SUFFIXES = {".bin", ".png", ".jpg", ".jpeg", ".mp4", ".pdf", ".lock"}


def scan(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return problems
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOWED.search(line):
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                problems.append(f"{path}:{lineno}: {label}")
                break
    return problems


def iter_files(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv if Path(a).is_file()]
    return [
        p
        for p in Path().rglob("*")
        if p.is_file() and not SKIP_DIRS & set(p.parts) and p.suffix.lower() not in SKIP_SUFFIXES
    ]


def main(argv: list[str]) -> int:
    problems: list[str] = []
    for path in iter_files(argv):
        if path.name == "check_no_secrets.py" or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        problems.extend(scan(path))

    if problems:
        print("Refusing the commit - possible credentials in the tree:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nUse the encrypted edge secret store, or a documented placeholder (USER:PASS).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

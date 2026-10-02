"""Refuse a commit that would publish organization data.

This repository is public. Exports, Entra lookups and candidate lists hold real
people's names, NetIDs and item IDs, and none of it may ever reach git history.
The check runs as a pre-commit hook (see .githooks/pre-commit) and looks only at
what the commit adds, so existing lines do not block unrelated work.

Run it by hand with:  python tools/leak_check.py            (staged changes)
                      python tools/leak_check.py --all      (every tracked file)

A line that legitimately matches (a format string, a documented example) can be
allowed by ending it with the marker:  leak-check: allow
"""
import re
import subprocess
import sys

ALLOW_MARKER = "leak-check: allow"

# Data files never belong in the repository, except synthetic test fixtures.
DATA_EXTENSIONS = (".csv", ".xlsx", ".xls", ".parquet", ".duckdb", ".db", ".bak", ".mdf", ".ldf", ".json.gz")
FIXTURE_DIRS = ("tests/fixtures/",)

PATTERNS = {
    "email address": re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|edu)\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    "AGOL username": re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+_[A-Za-z0-9]+\b"),
    "item ID": re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])"),
}


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


def added_lines_staged():
    """Yield (path, line) for every line the staged commit adds."""
    path = None
    for raw in git("diff", "--cached", "--unified=0", "--no-color", "--diff-filter=AM").splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("+") and path:
            yield path, raw[1:]


def staged_paths():
    return git("diff", "--cached", "--name-only", "--diff-filter=AM").splitlines()


def all_lines():
    for path in git("ls-files").splitlines():
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                for line in fh:
                    yield path, line.rstrip("\n")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary or missing; extension rule covers data files


def main():
    scan_all = "--all" in sys.argv[1:]
    paths = git("ls-files").splitlines() if scan_all else staged_paths()
    lines = all_lines() if scan_all else added_lines_staged()
    problems = []

    for path in paths:
        if path.lower().endswith(DATA_EXTENSIONS) and not path.startswith(FIXTURE_DIRS):
            problems.append(f"{path}: data file ({path.rsplit('.', 1)[-1]}) outside {FIXTURE_DIRS[0]}")

    for path, line in lines:
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS.items():
            match = pattern.search(line)
            if match:
                problems.append(f"{path}: {label} '{match.group(0)[:6]}...'")
                break

    if problems:
        print("leak_check: REFUSED. This repository is public and these look like organization data:")
        for problem in sorted(set(problems)):
            print("  " + problem)
        print(f"Remove them, or end a line that is genuinely safe with '{ALLOW_MARKER}'.")
        return 1
    print("leak_check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Small Alembic command wrapper for local development."""

from __future__ import annotations

import argparse
import subprocess
import sys


def run_alembic(args: list[str]) -> int:
    return subprocess.call(["alembic", *args])


def main() -> int:
    parser = argparse.ArgumentParser(description="thinkback database migration helper")
    parser.add_argument("command", choices=["upgrade", "downgrade", "current", "history"])
    parser.add_argument("revision", nargs="?", default="head")
    parsed = parser.parse_args()

    if parsed.command == "upgrade":
        return run_alembic(["upgrade", parsed.revision])
    if parsed.command == "downgrade":
        return run_alembic(["downgrade", parsed.revision])
    if parsed.command == "current":
        return run_alembic(["current", "-v"])
    return run_alembic(["history", "--verbose"])


if __name__ == "__main__":
    sys.exit(main())

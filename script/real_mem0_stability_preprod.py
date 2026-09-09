"""Compatibility entry point for memory performance and stability checks."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "script"

from script.real_mem0_p0_preprod_pressure import main

if __name__ == "__main__":
    main()

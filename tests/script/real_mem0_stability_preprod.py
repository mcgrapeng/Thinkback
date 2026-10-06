"""Compatibility entry point for memory performance and stability checks."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    # 以 `python tests/script/real_mem0_stability_preprod.py` 直跑时，
    # 需要把仓库根（而非 tests/）放上 sys.path，`tests.script` 才能被导入。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.script.real_mem0_p0_preprod_pressure import main

if __name__ == "__main__":
    main()

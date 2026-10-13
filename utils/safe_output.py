"""Outputs a person edits by hand (samples to mark, sheets to check) must never be overwritten."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


def dated_path(path: str | Path, stamp: str = "%Y%m%d") -> Path:
    """name.ext -> name_<stamp>.ext. Exits with a message instead of overwriting an existing file."""
    p = Path(path)
    out = p.with_name(f"{p.stem}_{datetime.now().strftime(stamp)}{p.suffix}")
    if out.exists():
        sys.exit(f"{out} already exists and may hold hand edits; not overwriting. "
                 f"Move or rename it first.")
    return out

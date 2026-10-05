#!/usr/bin/env python3
"""Run from a checkout without installing dependencies."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from claude_history_audit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

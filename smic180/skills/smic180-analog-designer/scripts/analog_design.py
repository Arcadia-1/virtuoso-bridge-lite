#!/usr/bin/env python3
"""Entry point for the SMIC180 analog design workflow."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from analog_design.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

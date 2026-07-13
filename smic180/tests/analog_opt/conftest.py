from pathlib import Path
import sys


SIMULATOR_ROOT = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator"
sys.path.insert(0, str(SIMULATOR_ROOT))

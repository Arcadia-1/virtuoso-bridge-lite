from pathlib import Path
import sys


DESIGNER_ROOT = Path(__file__).resolve().parents[2] / "skills" / "smic180-analog-designer"
sys.path.insert(0, str(DESIGNER_ROOT))

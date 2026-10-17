"""Root entrypoint exporting app for Vercel service 'app'."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_standalone import app  # noqa: E402

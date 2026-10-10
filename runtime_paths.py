"""Writable portable paths and read-only packaged resources."""
import sys
from pathlib import Path

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(sys._MEIPASS) / "resources" if getattr(sys, "frozen", False) else APP_ROOT

"""Resolve application files independently of the working directory."""
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = (Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False) else PACKAGE_ROOT.parent)
QML_DIR = PACKAGE_ROOT / "qml"

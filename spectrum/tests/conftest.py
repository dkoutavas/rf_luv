"""Pytest configuration — add spectrum/ to sys.path for scanner imports."""

import sys
from pathlib import Path

# Add spectrum/ directory to sys.path so `from scanner import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

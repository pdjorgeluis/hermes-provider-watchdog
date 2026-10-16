#!/usr/bin/env python3
"""Runner: executes the hermetic unittest suite with plain python3 (no pytest)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

loader = unittest.TestLoader()
suite = loader.discover(str(HERE), pattern="test_*.py")
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
sys.exit(0 if result.wasSuccessful() else 1)

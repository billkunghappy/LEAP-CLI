"""Puts `leap/` and the repo root on sys.path, so tests import modules flatly.

Every test file imports this first. It works both for `python3 tests/x_test.py`
and for `python3 -m unittest discover -s tests -p '*_test.py'`.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "leap"), _ROOT):
  if _p not in sys.path:
    sys.path.insert(0, _p)

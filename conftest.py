"""Make the demo's `app` and `upstream` packages importable in tests."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(ROOT, "demo")

for path in (DEMO, os.path.join(DEMO, "downstream")):
    if path not in sys.path:
        sys.path.insert(0, path)

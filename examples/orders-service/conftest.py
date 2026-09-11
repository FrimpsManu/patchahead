"""Put this repository's own packages on sys.path for the tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

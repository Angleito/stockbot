"""Root test configuration ensuring the project root is in the Python path."""

import os
import sys

# Add project root directory to sys.path so tests can import `app`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

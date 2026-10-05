# Double-click this file to open Core Compressor without a terminal window.
# It must stay in the same folder as core_compressor.py.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core_compressor

core_compressor.run_gui()

#!/usr/bin/env python3
"""A health diagnosis is work; the original failing watch keeps its watermark."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tick_state
from system_health import diagnosis
import result

if __name__ == "__main__":
    root = tick_state._repo_root(__file__)
    text = diagnosis(root, json.loads(sys.argv[1]))
    result.counted(1 if text else 0, text or "No health diagnosis due")

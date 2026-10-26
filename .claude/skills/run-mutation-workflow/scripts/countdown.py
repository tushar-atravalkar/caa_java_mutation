# -*- coding: utf-8 -*-
"""Pause before the LLM round so the human can interrupt. `countdown.py [seconds]` (default 15)."""
import sys
import time

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
print(f"Round 2 (LLM test writing) starts in {seconds}s - interrupt now to cancel.", flush=True)
time.sleep(seconds)
print("Starting round 2.", flush=True)

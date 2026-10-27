# -*- coding: utf-8 -*-
"""Pause so the human can interrupt. `countdown.py [seconds] [--scope]` (default 15 seconds).

Default: the pause before the LLM test-writing round. `--scope`: the pause when the prompt named
no file, PR, commit or branch, before falling back to uncommitted + untracked changes."""
import sys
import time

args = [a for a in sys.argv[1:] if a != "--scope"]
scope = "--scope" in sys.argv[1:]
seconds = int(args[0]) if args else 15
if scope:
    print("No file, PR, commit or branch was named. Tell me which one to mutate (a file path, a PR "
          "number or link, a commit id, a branch, 'untracked files' or 'last commit'). "
          f"Otherwise in {seconds}s I use your uncommitted + untracked changes, else the last commit "
          "- interrupt now to give a scope.", flush=True)
else:
    print(f"Round 2 (LLM test writing) starts in {seconds}s - interrupt now to cancel.", flush=True)
time.sleep(seconds)
print("No scope given - using uncommitted + untracked changes, else the last commit." if scope
      else "Starting round 2.", flush=True)

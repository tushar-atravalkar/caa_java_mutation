# -*- coding: utf-8 -*-
"""
Wait for a background mutation_wizard.py run to finish and print its result.

A sub-agent cannot "wait for the completion notification" of its own
background command: ending its turn hands control back to its caller. Without
this it either polls in a shell loop or reads the output too early, sees no
result yet, and launches the whole engine run a second time.

Pass the output file the Bash tool named when the wizard was launched in the
background. This blocks until that run's `MUTATION_RESULT:` line appears and
prints it -- so one tool call returns the answer -- or, after --timeout
seconds (kept under the 10-minute Bash tool limit), prints STILL_RUNNING so the
same command can simply be run again.

Usage:
    python await_result.py <background-task-output-file> [--timeout 540]
"""

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

_EXITED = re.compile(r"^\[exited with code (\d+)\]", re.MULTILINE)
# The Bash tool creates the output file when it launches the command, so a path still missing after this was never right.
_MISSING_GRACE_S = 20


def _locate(path: Path):
    """The output file, tolerating a retyped path (e.g. `-` vs `_`) by looking the task id up under the Claude temp dir."""
    if path.is_file():
        return path
    roots = {Path(tempfile.gettempdir()) / "claude"}
    roots.update(p for p in path.parents if p.name.lower() == "claude")
    hits = [hit for root in roots if root.is_dir()
            for hit in root.glob(f"*/*/tasks/{path.name}")]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _result_line(text: str):
    for line in reversed(text.splitlines()):
        i = line.find("MUTATION_RESULT:")
        if i != -1:
            return line[i:]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Block until a wizard run prints MUTATION_RESULT")
    parser.add_argument("output", help="Background task output file of the mutation_wizard.py launch")
    parser.add_argument("--timeout", type=int, default=540,
                        help="Seconds to wait before returning STILL_RUNNING (default 540)")
    args = parser.parse_args()

    path = Path(args.output)
    started = time.time()
    deadline = started + args.timeout
    while True:
        found = _locate(path)
        if found is None:
            if time.time() - started >= _MISSING_GRACE_S:
                print(f"NO_OUTPUT_FILE: {path} does not exist, and no task output named "
                      f"{path.name!r} was found either. Re-run this command with the exact "
                      "path the Bash tool printed when it launched the wizard. Do NOT "
                      "relaunch mutation_wizard.py.")
                return 1
            time.sleep(3)
            continue
        text = found.read_text(encoding="utf-8", errors="replace")
        line = _result_line(text)
        if line:
            print(line)
            return 0
        exited = _EXITED.search(text)
        if exited:
            print(f"NO_RESULT: the wizard exited (code {exited.group(1)}) without printing "
                  "MUTATION_RESULT. Report this as an error; do NOT relaunch it. Last lines:")
            print("\n".join(text.splitlines()[-15:]))
            return 1
        if time.time() >= deadline:
            print(f"STILL_RUNNING: no result after {args.timeout}s. Run this same command "
                  "again to keep waiting. Do NOT relaunch mutation_wizard.py.")
            return 0
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())

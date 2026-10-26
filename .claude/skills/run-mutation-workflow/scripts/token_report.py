# -*- coding: utf-8 -*-
"""
Measure the ORCHESTRATION token cost of a mutation testing run.

The pipeline steps are Python and cost nothing -- telemetry.json records that
correctly. But invoking an agent to press the button is not free, and the agent
cannot read its own usage. This script recovers the real number from the Claude
Code session transcript instead of guessing.

Claude Code appends JSONL records to:
    ~/.claude/projects/<slugified-cwd>/<session-id>.jsonl
and writes each sub-agent's records to its own file under
    ~/.claude/projects/<slugified-cwd>/<session-id>/subagents/agent-*.jsonl
Assistant records carry a `usage` object with exact token counts -- repeated
on every record belonging to the same API response, so totals are counted
once per response (see _api_messages), across the session and its sub-agents.

Usage:
    python token_report.py --run-id run-1786536102
    python token_report.py --since 2026-08-12T12:00:00
    python token_report.py --run-id run-1786536102 --write
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def _project_dir(cwd: str) -> Path:
    """Claude Code slugifies the cwd: C:\\a\\b -> -a-b (non-alphanumerics to '-')."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in str(cwd))
    return Path.home() / ".claude" / "projects" / slug


def find_transcripts(cwd: str = None) -> list:
    """Newest first. Falls back to scanning every project when the slug misses."""
    cwd = cwd or os.getcwd()
    candidates = []

    exact = _project_dir(cwd)
    if exact.exists():
        candidates.extend(exact.glob("*.jsonl"))

    if not candidates:
        root = Path.home() / ".claude" / "projects"
        if root.exists():
            candidates.extend(root.rglob("*.jsonl"))

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def _parse_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_USAGE_FIELDS = {
    "input_tokens": "inputTokens",
    "cache_creation_input_tokens": "cacheCreationTokens",
    "cache_read_input_tokens": "cacheReadTokens",
    "output_tokens": "outputTokens",
}

# Recorded in telemetry.json to tell figures from this deduplicating counter apart from the old per-record sum.
COUNTING_METHOD = "unique-api-message+subagents"


def subagent_transcripts(transcript: Path) -> list:
    """Sub-agent transcripts live in <session-id>/subagents/agent-*.jsonl, not the session file; without them delegated work is missed."""
    folder = transcript.parent / transcript.stem / "subagents"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def _api_messages(path: Path, merged: dict, bucket: str) -> None:
    """Fold `path`'s records into `merged`, one entry per API response (message.id).

    One response is written as several records that all repeat its usage, so each field takes
    its max across the group (output_tokens grows while streaming). `merged` is shared across
    the session and sub-agent files because a fork sub-agent copies the parent's last response;
    it counts once, under whichever file was folded in first.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            usage = message.get("usage")
            if not usage:
                continue
            key = (message.get("id") or record.get("requestId")
                   or record.get("uuid") or f"{path.name}:{n}")
            stamp = _parse_ts(record.get("timestamp"))
            entry = merged.get(key)
            if entry is None:
                merged[key] = {
                    "stamp": stamp,
                    "bucket": bucket,
                    "model": message.get("model") or "unknown",
                    "usage": {f: usage.get(f, 0) or 0 for f in _USAGE_FIELDS},
                }
                continue
            for f in _USAGE_FIELDS:
                entry["usage"][f] = max(entry["usage"][f], usage.get(f, 0) or 0)
            if stamp and (entry["stamp"] is None or stamp < entry["stamp"]):
                entry["stamp"] = stamp


def collect(transcript: Path, since: datetime = None, until: datetime = None,
            include_subagents: bool = True) -> dict:
    totals = {
        "messages": 0,
        "mainMessages": 0,
        "subagentMessages": 0,
        "inputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
        "outputTokens": 0,
        "models": {},
    }

    sources = [(transcript, "mainMessages")]
    if include_subagents:
        sources += [(p, "subagentMessages") for p in subagent_transcripts(transcript)]

    merged = {}
    for path, bucket in sources:
        _api_messages(path, merged, bucket)

    for msg in merged.values():
        stamp = msg["stamp"]
        if since and stamp and stamp < since:
            continue
        if until and stamp and stamp > until:
            continue
        totals["messages"] += 1
        totals[msg["bucket"]] += 1
        for field, key in _USAGE_FIELDS.items():
            totals[key] += msg["usage"][field]
        totals["models"][msg["model"]] = totals["models"].get(msg["model"], 0) + 1

    totals["totalInputTokens"] = (
        totals["inputTokens"] + totals["cacheCreationTokens"] + totals["cacheReadTokens"]
    )
    totals["totalTokens"] = totals["totalInputTokens"] + totals["outputTokens"]
    totals["countingMethod"] = COUNTING_METHOD
    return totals


def _window_from_run(project_root: Path, run_id: str):
    """Derive the run's time window from its telemetry.json."""
    telemetry_path = project_root / "mutation-reports" / run_id / "telemetry.json"
    if not telemetry_path.exists():
        return None, None, None
    data = json.loads(telemetry_path.read_text(encoding="utf-8"))
    start_ms = data.get("runStartMs")
    end_ms = data.get("runEndMs")
    since = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) if start_ms else None
    until = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) if end_ms else None
    return since, until, telemetry_path


def _round1_boundary(telemetry: dict):
    """Timestamp where round 1 ends and round 2 begins, or None when the run never reached round 2."""
    steps = telemetry.get("steps") or {}
    start_ms = telemetry.get("runStartMs")
    handoff = steps.get("step5_handoff")
    # A skipped hand-off (FAIL without --remediate) means no round 2 follows: nothing to split.
    if not handoff or handoff.get("skipped") or not handoff.get("promptPath") or start_ms is None:
        return None
    cumulative_ms = sum(
        (steps.get(k) or {}).get("wallClockMs", 0)
        for k in ("step1", "step2", "step3", "step4", "step5_handoff")
    )
    return datetime.fromtimestamp((start_ms + cumulative_ms) / 1000, tz=timezone.utc)


# --- per-invocation attribution ---
# The time window misses agent turns just before and after the script runs, so when sub-agents drove the
# run, tokens are attributed by which invocation worked on this run id instead.

_PIPELINE = re.compile(r"(mutation_wizard|(?<![\w-])run)\.py\b")
_PIPELINE_VERIFY = re.compile(r"(mutation_wizard|(?<![\w-])run)\.py\b.*\s--verify\b", re.DOTALL)
# Pre-run wizard work (scope resolution, test counting) sits between agent launch and run.py's start.
_LAUNCH_LEAD_S = 300


def _agent_type(path: Path) -> str:
    try:
        meta = path.with_suffix(".meta.json")
        return json.loads(meta.read_text(encoding="utf-8")).get("agentType") or ""
    except (OSError, ValueError):
        return ""


def _responses(path: Path, run_id: str) -> dict:
    """{response key: entry} for one transcript, merged per API response, plus whether it launched `--verify` for this run (a tool call, not quoted text)."""
    out = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            usage = message.get("usage")
            if not usage:
                continue
            key = (message.get("id") or record.get("requestId")
                   or record.get("uuid") or f"{path.name}:{n}")
            stamp = _parse_ts(record.get("timestamp"))
            entry = out.setdefault(key, {
                "stamp": stamp, "model": message.get("model") or "unknown",
                "usage": dict.fromkeys(_USAGE_FIELDS, 0), "verify": False, "launch": False,
            })
            for f in _USAGE_FIELDS:
                entry["usage"][f] = max(entry["usage"][f], usage.get(f, 0) or 0)
            if stamp and (entry["stamp"] is None or stamp < entry["stamp"]):
                entry["stamp"] = stamp
            content = message.get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    cmd = str((b.get("input") or {}).get("command") or "")
                    # The pipeline's own round-2 launch.
                    if run_id in cmd and _PIPELINE_VERIFY.search(cmd):
                        entry["verify"] = True
                    elif (_PIPELINE.search(cmd) and "--verify" not in cmd
                          and "--regen-prompt" not in cmd):
                        entry["launch"] = True   # a fresh round-1 pipeline launch
    return out


def _task_prompt(path: Path) -> str:
    """The instructions this sub-agent was invoked with (its first user turn)."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "user":
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str):
                return content
            return " ".join(b.get("text", "") for b in (content or [])
                            if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _totals(entries: list) -> dict:
    t = {"messages": 0, "mainMessages": 0, "subagentMessages": 0, "inputTokens": 0,
         "cacheCreationTokens": 0, "cacheReadTokens": 0, "outputTokens": 0, "models": {}}
    for e in entries:
        t["messages"] += 1
        t["mainMessages" if e["origin"] == "main" else "subagentMessages"] += 1
        for field, key in _USAGE_FIELDS.items():
            t[key] += e["usage"][field]
        t["models"][e["model"]] = t["models"].get(e["model"], 0) + 1
    t["totalInputTokens"] = t["inputTokens"] + t["cacheCreationTokens"] + t["cacheReadTokens"]
    t["totalTokens"] = t["totalInputTokens"] + t["outputTokens"]
    t["countingMethod"] = COUNTING_METHOD
    return t


def collect_run(transcript: Path, run_id: str, run_start: datetime):
    """Attribute a sub-agent-driven run's tokens to round 1 / round 2. Returns {"total", "round1", "round2"} or None.

    None means no sub-agent can be tied to this run (caller falls back to the time window).
    Membership is decided by what an invocation did, never by mentioning the run id:
    - Round 1: the invocation whose fresh launch was the latest shortly before the run's start.
    - Round 2: any invocation that launched `--verify` for this run, plus the test-writer for it.
    - Main session: only turns between the run's first and last sub-agent responses.
    """
    if run_start is None:
        return None
    lead = run_start.timestamp() - _LAUNCH_LEAD_S
    orchestrators, writers, launches = [], [], []
    for p in subagent_transcripts(transcript):
        if _agent_type(p) == "mutation-test-writer":
            if run_id in _task_prompt(p):
                writers.append(p)
            continue
        resp = _responses(p, run_id)
        if any(e["verify"] for e in resp.values()):
            orchestrators.append(p)
        for e in resp.values():
            if e["launch"] and e["stamp"] and lead <= e["stamp"].timestamp() <= run_start.timestamp():
                launches.append((e["stamp"], p))
    if launches:
        round1_owner = max(launches, key=lambda x: x[0])[1]
        if round1_owner not in orchestrators:
            orchestrators.append(round1_owner)
    if not launches and not orchestrators:
        return None

    merged = {}
    for path, origin in ([(transcript, "main")] + [(p, "orch") for p in orchestrators]
                         + [(p, "writer") for p in writers]):
        for key, e in _responses(path, run_id).items():
            if key in merged:      # a fork's copy of a response counts once
                continue
            e["origin"] = origin
            merged[key] = e
    stamped = [e for e in merged.values() if e["stamp"]]
    subs = sorted((e for e in stamped if e["origin"] != "main"), key=lambda e: e["stamp"])
    mains = sorted((e for e in stamped if e["origin"] == "main"), key=lambda e: e["stamp"])
    if not subs:
        return None

    verify_starts = [e["stamp"] for e in subs if e["origin"] == "orch" and e["verify"]]
    writer_starts = [e["stamp"] for e in subs if e["origin"] == "writer"]
    round2_start = min(verify_starts + writer_starts) if (verify_starts or writer_starts) else None

    def _launcher(at):
        """The main-session turn that launched the agent whose first response is `at`."""
        before = [m["stamp"] for m in mains if m["stamp"] <= at]
        return before[-1] if before else at

    main_from, main_to = _launcher(subs[0]["stamp"]), subs[-1]["stamp"]
    main_round2 = _launcher(round2_start) if round2_start else None
    if main_round2 is not None and main_round2 <= main_from:
        # No separate turn launched round 2 (orchestrator resumed): split at round 2's start instead.
        main_round2 = round2_start

    r1, r2 = [], []
    for e in subs:
        is_r2 = e["origin"] == "writer" or (round2_start is not None and e["stamp"] >= round2_start)
        (r2 if is_r2 else r1).append(e)
    for m in mains:
        if main_from <= m["stamp"] <= main_to:
            (r2 if main_round2 is not None and m["stamp"] >= main_round2 else r1).append(m)
    return {"total": _totals(r1 + r2), "round1": _totals(r1), "round2": _totals(r2)}


def _session_for_run(transcripts: list, run_id: str):
    """The session whose sub-agents handled this run, so an older run is measured against its own session."""
    for t in transcripts[:30]:
        for p in subagent_transcripts(t):
            try:
                if run_id in p.read_text(encoding="utf-8", errors="replace"):
                    return t
            except OSError:
                continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Report real agent token usage for a run")
    parser.add_argument("--run-id", help="Scope to this run's time window")
    parser.add_argument("--since", help="ISO timestamp lower bound")
    parser.add_argument("--until", help="ISO timestamp upper bound")
    parser.add_argument("--cwd", help="Project directory (defaults to current)")
    parser.add_argument("--transcript", help="Explicit .jsonl path")
    parser.add_argument("--write", action="store_true",
                        help="Merge the result into the run's telemetry.json")
    parser.add_argument("--project-root", default=None,
                        help="Override the auto-detected project root. Normally derived "
                             "from this file's own install depth (four levels above "
                             "scripts/token_report.py), which is wrong when run.py "
                             "invokes this script automatically from a different install "
                             "layout (its own source tree, a test's synthetic repo) -- "
                             "run.py always passes its own, correctly git-toplevel-"
                             "anchored PROJECT_ROOT here instead of relying on this "
                             "script's guess.")
    args = parser.parse_args()

    project_root = Path(args.project_root) if args.project_root else Path(__file__).resolve().parents[4]

    since = _parse_ts(args.since)
    until = _parse_ts(args.until)
    telemetry_path = None
    telemetry_data = None
    boundary = None
    if args.run_id:
        since, until, telemetry_path = _window_from_run(project_root, args.run_id)
        if since is None:
            print(f"No telemetry.json for {args.run_id}; reporting whole transcript.")
        elif telemetry_path.exists():
            telemetry_data = json.loads(telemetry_path.read_text(encoding="utf-8"))
            boundary = _round1_boundary(telemetry_data)

    if args.transcript:
        transcripts = [Path(args.transcript)]
    else:
        transcripts = find_transcripts(args.cwd or str(project_root))

    if not transcripts:
        print("No Claude Code transcript found under ~/.claude/projects/.")
        print("If this run was not driven by an agent, orchestration cost is genuinely 0.")
        return 1

    transcript = transcripts[0]
    attributed = None
    if args.run_id and since is not None and not args.transcript:
        transcript = _session_for_run(transcripts, args.run_id) or transcript
    if args.run_id and since is not None:
        attributed = collect_run(transcript, args.run_id, since)

    rounds = None
    if attributed:
        attribution = "per-invocation"
        totals = attributed["total"]
        if boundary is not None:
            rounds = {"round1": attributed["round1"], "round2": attributed["round2"]}
    else:
        attribution = "run-window"
        # Strictly the run's own window; never widen an empty one, that would borrow later work.
        totals = collect(transcript, since, until)
        # If the run reached round 2, split the same window at the hand-off boundary.
        if boundary is not None and since is not None:
            rounds = {"round1": collect(transcript, since, boundary),
                      "round2": collect(transcript, boundary, until)}
    totals["attribution"] = attribution

    print(f"Transcript : {transcript}")
    if attributed:
        print("Attribution: per agent invocation (sub-agents that handled this run, "
              "plus the session's turns that launched them)")
    elif since:
        print(f"Window     : {since.isoformat()} -> {until.isoformat() if until else 'now'}")
    print("")
    print("ORCHESTRATION TOKENS (the agent, not the scripts)")
    print(f"  API responses        : {totals['messages']}  "
          f"(session {totals['mainMessages']}, sub-agents {totals['subagentMessages']})")
    print(f"  input (uncached)     : {totals['inputTokens']:,}")
    print(f"  cache creation       : {totals['cacheCreationTokens']:,}")
    print(f"  cache read           : {totals['cacheReadTokens']:,}")
    print(f"  output               : {totals['outputTokens']:,}")
    print(f"  TOTAL                : {totals['totalTokens']:,}")
    if totals["models"]:
        print(f"  models               : "
              + ", ".join(f"{m} x{c}" for m, c in totals["models"].items()))
    if rounds:
        print("")
        print(f"  round 1 (scoring + hand-off)      : {rounds['round1']['totalTokens']:,} tokens "
              f"over {rounds['round1']['messages']} message(s)")
        print(f"  round 2 (test writing + verify)   : {rounds['round2']['totalTokens']:,} tokens "
              f"over {rounds['round2']['messages']} message(s)")
    print("")
    print("SCRIPT TOKENS          : 0  (steps 1-4, 4b, 6 are Python)")

    if args.write and telemetry_path and telemetry_path.exists():
        data = telemetry_data if telemetry_data is not None else json.loads(
            telemetry_path.read_text(encoding="utf-8"))
        data["orchestration"] = totals
        data["orchestration"]["transcript"] = str(transcript)
        if rounds:
            data["orchestration"]["rounds"] = rounds
        telemetry_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nMerged into {telemetry_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
Render the ONE block the gate-runner agent relays after a run.

Everything the agent used to derive by reading prose (exit-code branching, the
hand-off block, the exit-3 flag text, the timing table, the token meter) is
computed here, deterministically, so the agent's whole post-run job is: run
this, relay stdout, stop.

    python final_report.py --latest --tokens
    python final_report.py --run-id run-123 [--tests-added N --test-file P ...]

The last lines are always `NEXT: ...`: `STOP` or `HANDOFF` (the caller then
invokes mutation-test-writer with the printed MUTATION_HANDOFF line).
"""

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import changed_files_scanner  # noqa: E402
import token_report  # noqa: E402

# The repo under test, resolved from the invocation context - never from where
# the plugin happens to be installed.
PROJECT_ROOT = Path(changed_files_scanner.resolve_project_root())
REPORTS = PROJECT_ROOT / "mutation-reports"

_STEP_LABELS = (
    ("step1", "languages"), ("step2", "scope"), ("step3", "arid filter"),
    ("step4", "engines"), ("step4b", "engines (verify)"),
    ("step5_handoff", "handoff"),
)


def latest_run_id() -> str:
    pointer = REPORTS / "LATEST"
    if pointer.exists():
        return pointer.read_text(encoding="utf-8").strip()
    gates = sorted(REPORTS.glob("*/gate.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return gates[0].parent.name if gates else ""


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _live_holder(run_id: str) -> dict:
    """The wizard lock's holder when a live wizard is still running THIS run, else {}.

    Reached when a duplicate launch was blocked (wizard exit 4): `LATEST` still
    points at the run in progress, which has no result yet."""
    try:
        import mutation_wizard as wizard
        holder = wizard._read_json(REPORTS / ".wizard.lock")
        alive = (holder.get("runId") == run_id
                 and wizard._pid_alive(int(holder.get("pid") or 0))
                 and time.time() - float(holder.get("startedAt") or 0) < wizard._LOCK_MAX_AGE_S)
    except Exception:  # best-effort: never let this probe break the report
        return {}
    return holder if alive else {}


def _sonar_lines(gate: dict) -> list:
    """Step 7 outcome. A failed upload never changes the verdict, but it makes the run incomplete."""
    sonar = gate.get("sonarUpload") or {}
    if sonar.get("ok"):
        return [f"sonar: uploaded (quality gate {sonar.get('qualityGate') or 'n/a'}) - "
                "commit .sonar/mutation-report.json to the PR, CI reads it from the checkout"]
    if sonar.get("attempted"):
        lines = [f"sonar: UPLOAD FAILED [{sonar.get('stage', '?')}] - run INCOMPLETE, verdict unchanged"]
        if sonar.get("message"):
            lines.append("  " + str(sonar["message"]).strip().splitlines()[0][:300])
        return lines
    return []


def _timing_lines(telemetry: dict) -> list:
    steps = telemetry.get("steps") or {}
    lines = []
    for key, label in _STEP_LABELS:
        s = steps.get(key)
        if not s:
            continue
        lines.append(f"  {label:<18}{s.get('wallClockMs', 0) / 1000:>9.2f}s")
        if key in ("step4", "step4b"):
            for lang, p in sorted((s.get("perLanguage") or {}).items()):
                if isinstance(p, dict) and "wallClockMs" in p:
                    lines.append(f"    {lang:<16}{p['wallClockMs'] / 1000:>9.2f}s")
    total = telemetry.get("totalWallClockMs")
    if total is not None:
        lines.append(f"  {'TOTAL':<18}{total / 1000:>9.2f}s   (script tokens: 0)")
    return lines


def _orchestration_line(telemetry: dict) -> str:
    o = telemetry.get("orchestration")
    if not o:
        return "orchestration tokens: unavailable (no transcript found)"
    return (f"orchestration tokens: {o['totalTokens']:,} "
            f"(in {o['totalInputTokens']:,}, out {o['outputTokens']:,}, "
            f"{o['messages']} messages)")


def _measure_tokens(run_id: str) -> None:
    """Merge the agent's real usage into telemetry.json, if a transcript exists."""
    since, until, tpath = token_report._window_from_run(PROJECT_ROOT, run_id)
    transcripts = token_report.find_transcripts(str(PROJECT_ROOT))
    if not (tpath and transcripts):
        return
    totals = token_report.collect(transcripts[0], since, until)
    if totals["messages"] == 0:
        totals = token_report.collect(transcripts[0], since, None)
    if not totals["messages"]:
        return
    data = _load(tpath)
    data["orchestration"] = {**totals, "transcript": str(transcripts[0])}
    tpath.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _mutant_lines(mutants: list, cap: int = 25) -> list:
    lines = [f"  [{m.get('language', '?')}] {m.get('file') or m.get('source_file')}"
             f":{m.get('line')} | {m.get('operator')}" for m in mutants[:cap]]
    if len(mutants) > cap:
        lines.append(f"  ... +{len(mutants) - cap} more (results.json)")
    return lines


def _write_bug_report(adir: Path, run_id: str, gate: dict, remaining: list,
                      tests_added, test_files) -> str:
    """Paste-ready issue text for a run that failed the gate after its one LLM round."""
    langs = gate.get("perLanguage") or {}
    body = [
        f"# Mutation gate failed after the LLM round (run {run_id})", "",
        f"- Score: {gate.get('scoreBefore')}% -> {gate.get('score')}% "
        f"(threshold {gate.get('threshold')}%)",
        f"- Languages/engines: {', '.join(gate.get('languages') or [])} / "
        f"{', '.join(gate.get('tools') or [])}",
        f"- Tests added by the writer: {tests_added if tests_added is not None else 'unknown'}",
        *[f"  - {f}" for f in (test_files or [])], "",
        "## Per language",
        *[f"- {k}: {v.get('mutation_score')}% ({v.get('killed')}/{v.get('total')})"
          for k, v in sorted(langs.items())], "",
        f"## Mutants still alive ({len(remaining)})",
        *_mutant_lines(remaining, cap=200), "",
        "## To decide", "Either these are equivalent mutants (accept or exclude them) or the "
        "pipeline/test writer has a defect. The LLM is not re-run automatically.",
        f"", f"Artifacts: mutation-reports/{run_id}/",
    ]
    path = adir / "bug-report.md"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return f"mutation-reports/{run_id}/bug-report.md"


def render(run_id: str, tests_added=None, test_files=None, tokens: bool = False) -> str:
    adir = REPORTS / run_id
    if _live_holder(run_id):
        return (f"verdict: ALREADY_RUNNING   run: {run_id}\n"
                "a mutation run is still in progress in this project; this launch did not "
                "start a second one. Wait for that run's own report.\nNEXT: STOP")
    gate = _load(adir / "gate.json")
    if not gate:
        return (f"verdict: CRASHED\nrun: {run_id}\n"
                f"no gate.json - see {adir / 'mutation-wizard.log'}\nNEXT: STOP")
    if tokens:
        _measure_tokens(run_id)
    telemetry = _load(adir / "telemetry.json")

    verdict = gate.get("verdict", "UNKNOWN")
    out = [f"verdict: {verdict}   run: {run_id}"]
    if gate.get("reason"):
        out.append(f"reason: {gate['reason']}")
    if gate.get("scopeMessage"):  # --pr/--commit named code this checkout does not hold: options, user decides
        out.append(gate["scopeMessage"])
        out.append("Ask the user which option they want; never choose for them. Nothing was mutated.")
    if gate.get("error"):
        # Engine failures carry whole tracebacks; the tail names the cause and
        # the full transcript is in mutation-wizard.log.
        err = gate["error"].strip().splitlines()
        shown = err if len(err) <= 8 else [err[0], "  ...", *err[-6:]]
        out.append("error: " + "\n".join(shown)[:1200])
        out.append(f"full log: mutation-reports/{run_id}/mutation-wizard.log")

    score = gate.get("score")
    if score is not None:
        before = gate.get("scoreBefore")
        arrow = f"{before}% -> " if before is not None else ""
        out.append(f"mutation score: {arrow}{score}% (threshold {gate.get('threshold')}%)")
    langs = gate.get("perLanguage") or {}
    for lang, s in sorted(langs.items()):
        tool = f" [{s['tool']}]" if s.get("tool") else ""
        out.append(f"  {lang:<11}{s.get('mutation_score', 0)}% "
                   f"({s.get('killed', 0)}/{s.get('total', 0) - s.get('non_viable', 0)}){tool}"
                   + (f"  +{s['non_viable']} non-viable (not scored)" if s.get("non_viable") else ""))
    if "survived" in gate and not isinstance(gate["survived"], list):
        out.append(f"survived: {gate['survived']}   no-coverage: {gate.get('noCoverage')}")
    if tests_added is not None:
        out.append(f"tests added (by mutation-test-writer): {tests_added}")
    for f in test_files or []:
        out.append(f"  {f}")

    present = [n for n in ("report.txt", "gate.json") if (adir / n).exists()]
    out.append("artifacts: " + ", ".join(f"mutation-reports/{run_id}/{n}" for n in present))
    out.extend(_sonar_lines(gate))
    timing = _timing_lines(telemetry)
    if timing:
        out.append("timing:")
        out.extend(timing)
    if tokens:
        out.append(_orchestration_line(telemetry))

    if verdict == "FAIL":
        todo = (gate.get("survivorsForLlm") or []) + (gate.get("noCoverageMutants") or [])
        out.append(f"threshold not met - {len(todo)} mutant(s) need tests:")
        out.extend(_mutant_lines(todo))
    if verdict == "MANUAL_REVIEW":
        remaining = gate.get("survivorsRemaining") or []
        out.append("ROUND 2 FAILED: threshold still not met after the LLM fixes. "
                   "This is a BUG to report, not a reason for another round - no further LLM run.")
        out.extend(_mutant_lines(remaining))
        bug = _write_bug_report(adir, run_id, gate, remaining, tests_added, test_files)
        out.append(f"bug report draft: {bug}")

    if gate.get("llmRequired") and gate.get("phase") != "verify":
        handoff = {"runId": run_id, "artifactsDir": f"mutation-reports/{run_id}",
                   "promptPath": f"mutation-reports/{run_id}/prompt.txt"}
        if gate.get("newFileNeeded"):
            handoff["newFileNeeded"] = gate["newFileNeeded"]
        out.append("MUTATION_HANDOFF: " + json.dumps(handoff))
        out.append("NEXT: HANDOFF")
    else:
        out.append("NEXT: STOP")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--latest", action="store_true", help="use the newest run in mutation-reports/")
    ap.add_argument("--tokens", action="store_true", help="measure and merge orchestration tokens")
    ap.add_argument("--tests-added", type=int)
    ap.add_argument("--test-file", action="append", dest="test_files")
    args = ap.parse_args()
    run_id = args.run_id or (latest_run_id() if args.latest else "")
    if not run_id:
        print("verdict: CRASHED\nno run found under mutation-reports/\nNEXT: STOP")
        return 1
    print(render(run_id, args.tests_added, args.test_files, args.tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main())

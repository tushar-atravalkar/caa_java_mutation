# -*- coding: utf-8 -*-
"""
Step 6: Generate plain-text and JSON reports from the merged engine
results. No LLM involved -- pure data formatting.

Polyglot: a merged score is the right gate but hides where the weakness is --
95% over 200 TypeScript mutants and 20% over 10 Python ones average to a
passing number. Every report therefore carries a PER-LANGUAGE breakdown next to
the overall figure, and names the engine that produced each part.

Reports mutation score and test strength separately, and breaks actionable
mutants into SURVIVED (weak assertions -- needs judgement) vs NO_COVERAGE (no
test reaches the line -- ordinary coverage work).
"""

import json
from datetime import datetime
from pathlib import Path


DEFAULT_THRESHOLD = 80
# The gate is mutation score. test_strength is reported for information only and
# must never be substituted when the mutation score looks unfavourable.
GATE_METRIC = "mutation_score"


def _score(summary: dict, key: str = GATE_METRIC) -> float:
    """Tolerate both the current summary shape and the legacy kill_ratio key."""
    if key in summary:
        return summary[key]
    return summary.get("kill_ratio", 0)


def generate(
    project_root: str,
    file_scoped: str,
    survivors_before: list,
    survivors_after: list,
    new_tests: list,
    summary_before: dict,
    summary_after: dict,
    run_id: str,
    elapsed_seconds: float,
    threshold: int = DEFAULT_THRESHOLD,
    gate_metric: str = GATE_METRIC,
    target_files: list = None,
    per_language: dict = None,
    tools: list = None,
    tests_added: int = 0,
) -> str:
    # Never gate on anything but the mutation score, whatever a caller passes.
    gate_metric = GATE_METRIC
    before_score = _score(summary_before, gate_metric)
    after_score = _score(summary_after, gate_metric)
    gate = "PASS" if after_score >= threshold else "FAIL"

    per_language = per_language or {}
    tools = tools or []
    target_files = target_files or [file_scoped]
    surv_after = [s for s in survivors_after if s.get("category") == "survived"]
    nocov_after = [s for s in survivors_after if s.get("category") == "no-coverage"]
    closed = len(survivors_before) - len(survivors_after)
    added = tests_added or len(new_tests)

    lines = [
        "=" * 70,
        "MUTATION TESTING RESULTS -- polyglot lightweight runner",
        "-" * 70,
        f"Run           : {run_id}",
        f"Date          : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Languages     : {', '.join(sorted(per_language)) or 'n/a'}",
        f"Engines       : {', '.join(sorted(set(tools))) or 'n/a'}",
        f"Files scoped  : {len(target_files)}",
    ]
    lines += [f"                  {f}" for f in target_files]
    lines += [
        "-" * 70,
        f"GATE: {gate}   (mutation score >= {threshold}%)",
        "",
        f"  Mutation score : {before_score}%  ->  {after_score}%",
        f"  Test strength  : {summary_before.get('test_strength', 'n/a')}%"
        f"  ->  {summary_after.get('test_strength', 'n/a')}%   (informational only)",
        f"  Mutants closed : {closed}",
    ]

    if len(per_language) > 1:
        lines += ["-" * 70, "PER LANGUAGE (a merged score can hide one weak language)"]
        lines.append(f"  {'LANGUAGE':<12} {'SCORE':>7} {'KILLED':>7} {'TOTAL':>6} "
                     f"{'SURV':>5} {'NOCOV':>6}")
        for lang, s in sorted(per_language.items()):
            lines.append(
                f"  {lang:<12} {str(s.get('mutation_score', 0)) + '%':>7} "
                f"{s.get('killed', 0):>7} {s.get('total', 0):>6} "
                f"{s.get('survived', 0):>5} {s.get('no_coverage', 0):>6}"
            )

    lines += [
        "-" * 70,
        "MUTANT BREAKDOWN (after)",
        f"  Generated   : {summary_after.get('total', 0)}",
        f"  Scored      : {summary_after.get('total', 0) - summary_after.get('non_viable', 0)}"
        f"  (generated minus non-viable)",
        f"  Killed      : {summary_after.get('killed', 0)}",
        f"  Survived    : {summary_after.get('survived', 0)}",
        f"  No coverage : {summary_after.get('no_coverage', 0)}",
    ]
    if summary_after.get("errors"):
        lines.append(f"  Errors      : {summary_after['errors']}  "
                     f"(timeout / memory / run error)")
    if summary_after.get("non_viable"):
        lines.append(f"  Non-viable  : {summary_after['non_viable']}  "
                     f"(did not compile -- excluded from the score)")
    if summary_after.get("suppressed"):
        lines.append(f"  Suppressed  : {summary_after['suppressed']}  "
                     f"(arid lines and equivalent mutants -- excluded from BOTH "
                     f"numerator and denominator)")

    lines += [
        "-" * 70,
        f"TESTS ADDED : {added}",
        "-" * 70,
        "REMAINING WORK",
        f"  Needs judgement (SURVIVED)   : {len(surv_after)}",
        f"  Coverage backlog (NO_COVER)  : {len(nocov_after)}",
    ]

    if surv_after:
        lines += ["", "  Surviving mutants:"]
        for s in surv_after:
            lines.append(
                f"    [{s.get('language', '?')}] "
                f"{s.get('file') or s.get('source_file', '?')}:{s.get('line', '?')} | "
                f"{s.get('operator', '?')} | {s.get('description', '')}"
            )

    lines += [
        "-" * 70,
        f"Wall-clock time : {elapsed_seconds:.1f}s",
        "Script tokens   : 0 (steps 1-4 and 6 are deterministic Python). This is "
        "NOT the run's total cost -- the orchestrating agent's own usage is "
        "measured separately by token_report.py.",
        "=" * 70,
    ]

    report_text = "\n".join(lines)
    print(report_text)

    out_dir = Path(project_root) / "mutation-reports" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text(report_text, encoding="utf-8")

    payload = {
        "run_id": run_id,
        "file": file_scoped,
        "target_files": target_files,
        "languages": sorted(per_language),
        "tools": sorted(set(tools)),
        "gate": gate,
        "gate_metric": gate_metric,
        "threshold": threshold,
        "summary_before": summary_before,
        "summary_after": summary_after,
        "per_language": per_language,
        "mutation_score_before": before_score,
        "mutation_score_after": after_score,
        # legacy key names, kept so older consumers keep parsing
        "kill_ratio_before": before_score,
        "kill_ratio_after": after_score,
        "survivors_before": survivors_before,
        "survivors_after": survivors_after,
        "new_tests_count": added,
        "elapsed_seconds": elapsed_seconds,
    }
    # Merge into the existing results.json: round 1 state (diff_map, remediate flag, ...) must survive verify.
    # Everything this call computes wins; keys it doesn't mention are kept.
    results_path = out_dir / "results.json"
    try:
        existing = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    for key, value in existing.items():
        payload.setdefault(key, value)
    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    audit_line = (
        f"{datetime.now().isoformat()} | {run_id} | "
        f"{','.join(sorted(per_language)) or 'n/a'} | "
        f"{len(target_files)} file(s) | {after_score}% | {gate}\n"
    )
    with open(Path(project_root) / "MUTATION_AUDIT.log", "a", encoding="utf-8") as fh:
        fh.write(audit_line)

    print(f"\nReport saved to: {out_dir}")
    print(f"  report.txt   {out_dir / 'report.txt'}")
    print(f"  results.json {out_dir / 'results.json'}")
    return str(out_dir)

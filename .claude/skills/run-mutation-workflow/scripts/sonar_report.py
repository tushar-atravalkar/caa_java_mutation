# -*- coding: utf-8 -*-
"""
Step 7a: write the Sonar mutation report (JSON, schemaVersion 1).

Deterministic and token-free. The report is the whole contract with the
`sonar-mutation-report-plugin` SonarQube plugin: the plugin reads it during the
normal Sonar scan and publishes the measures and the survivor issues. This
module only builds and writes the file; sonar_upload.py runs the scan.

Two scopes, both from the SAME mutants (suppression applied identically):

  overall  every mutant of the changed files (whole-file score, NOT repo-wide)
  new      only mutants on lines the diff changed (the agent's gate score)

Schema: schema/mutation-report.schema.json (vendored copy of the plugin's
contract; selftest validates the builder's output against it).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

try:  # package-relative imports -- resolve at any install location
    from .arid_filter import is_suppressed
    from .pit_parser import partition_suppressed
except ImportError:  # direct invocation: py .../sonar_report.py
    sys.path.insert(0, str(Path(__file__).parent))
    from arid_filter import is_suppressed
    from pit_parser import partition_suppressed

SCHEMA_VERSION = 1
REPORT_RELPATH = ".sonar/mutation-report.json"
PROPERTY = "sonar.mutation.reportPath"

_TIMED_OUT = "TIMED_OUT"
_MEMORY_ERROR = "MEMORY_ERROR"
_ACTIONABLE = {"SURVIVED": "SURVIVED", "NO_COVERAGE": "NO_COVERAGE"}


def _counts(mutants: list, suppressed: int = 0) -> dict:
    """Counts for one scope. Every mutant lands in exactly one bucket, so
    total >= the sum of the parts (the plugin rejects anything else).

    NON_VIABLE mutants (compile errors, PIT NOT_VIABLE) never ran a test; the gate leaves
    them out of the score, so they are left out here too. Counting them would make Sonar
    compute a lower score than the agent's own gate reported."""
    mutants = [m for m in mutants if m.get("status") != "NON_VIABLE"]
    c = {"total": len(mutants), "killed": 0, "timedOut": 0, "memoryError": 0,
         "survived": 0, "noCoverage": 0, "unknown": 0, "suppressed": suppressed}
    for m in mutants:
        status = m.get("status")
        if status == "KILLED":
            c["killed"] += 1
        elif status == _TIMED_OUT:
            c["timedOut"] += 1
        elif status == _MEMORY_ERROR:
            c["memoryError"] += 1
        elif status == "SURVIVED":
            c["survived"] += 1
        elif status == "NO_COVERAGE":
            c["noCoverage"] += 1
        else:
            c["unknown"] += 1
    ran = [m["tests_run"] for m in mutants if m.get("tests_run") is not None]
    if ran:  # absent (not zero) when the engine reports no per-test data
        c["testsExecuted"] = sum(ran)
        c["testKills"] = sum(1 for m in mutants
                             if m.get("status") == "KILLED" and m.get("killing_test"))
    return c


def _resolve_files(target_files: list) -> dict:
    """basename -> repo-relative path, only where unambiguous (PIT reports bare
    basenames; two files with the same name cannot be told apart)."""
    seen = {}
    for rel in target_files:
        seen.setdefault(Path(rel).name, set()).add(rel)
    return {name: next(iter(rels)) for name, rels in seen.items() if len(rels) == 1}


def _file_of(m: dict, by_basename: dict) -> str:
    return m.get("file") or by_basename.get(m.get("source_file") or "") or ""


def _git_head(project_root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(project_root),
                             capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _valid_score(value):
    """A percentage rounded to one decimal, or None when absent or not a 0-100 number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 100:  # also rejects NaN
        return None
    return round(float(value), 1)


def _valid_seconds(value):
    """Whole non-negative seconds, or None when absent or not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value < float("inf"):  # also rejects NaN
        return None
    return int(round(value))


def build_report(*, run_id: str, threshold: int, round_no: int, verdict: str,
                 tools: list, agent_version: str, project_root: Path,
                 all_mutations: list, scope_filter, diff_map: dict,
                 arid_map: dict, equivalent_map: dict,
                 target_files: list, productive: dict,
                 score_before=None, duration_sec=None) -> dict:
    """Build the report dict.

    `all_mutations`  every mutant the engines produced, BEFORE diff scoping.
    `scope_filter`   callable(mutations, diff_map) -> (kept, dropped); the
                     agent's own diff scoping, passed in so both scopes agree.
    `productive`     {file -> list of analyzable lines} from the arid filter.
    `score_before`   new-code mutation score of round 1, before the agent wrote any
                     test; the dashboard shows it next to the final score.
    `duration_sec`   wall time of the whole run, round 1 start to this report.
    Both are optional: a value that is missing or out of range is left out.
    """
    def partition(muts):
        return partition_suppressed(muts, arid_map, equivalent_map,
                                    is_suppressed=is_suppressed)

    counted_all, suppressed_all = partition(all_mutations)
    new_scope, _ = scope_filter(all_mutations, diff_map)
    counted_new, suppressed_new = partition(new_scope)
    by_basename = _resolve_files(target_files)

    def group(muts, suppressed, key):
        buckets = {}
        for m in muts:
            buckets.setdefault(key(m), []).append(m)
        sup = {}
        for m in suppressed:
            sup[key(m)] = sup.get(key(m), 0) + 1
        return buckets, sup

    def scoped(all_by, all_sup, new_by, new_sup, k):
        entry = {"overall": _counts(all_by.get(k, []), all_sup.get(k, 0))}
        entry["new"] = _counts(new_by.get(k, []), new_sup.get(k, 0))
        return entry

    lang = lambda m: (m.get("language") or "other").lower()
    file_key = lambda m: _file_of(m, by_basename)

    la, las = group(counted_all, suppressed_all, lang)
    ln, lns = group(counted_new, suppressed_new, lang)
    languages = {k: scoped(la, las, ln, lns, k) for k in sorted(set(la) | set(las))}

    fa, fas = group(counted_all, suppressed_all, file_key)
    fn, fns = group(counted_new, suppressed_new, file_key)
    files = {}
    for rel in sorted(k for k in set(fa) | set(fas) if k):
        sample = (fa.get(rel) or [{}])[0]
        lines = len(productive.get(rel) or [])
        changed = diff_map.get(rel) if diff_map else None
        entry = {"language": lang(sample) if sample else "other",
                 "changedLines": len(changed) if changed is not None else lines}
        if lines:
            entry["lines"] = lines
        entry.update(scoped(fa, fas, fn, fns, rel))
        files[rel] = entry

    mutants = []
    for m in counted_new:  # diff-scoped: issues only where the PR changed code
        status = _ACTIONABLE.get(m.get("status"))
        f = file_key(m)
        if status and f and int(m.get("line") or 0) > 0:
            mutants.append({"file": f, "line": int(m["line"]),
                            "operator": m.get("operator") or "unknown", "status": status})
    mutants.sort(key=lambda x: (x["file"], x["line"], x["operator"]))

    run = {"runId": run_id, "threshold": threshold, "round": round_no,
           "verdict": verdict, "tools": sorted(set(tools)),
           "agentVersion": agent_version, "commit": _git_head(Path(project_root))}
    before = _valid_score(score_before)
    if before is not None:
        run["scoreBefore"] = before
    seconds = _valid_seconds(duration_sec)
    if seconds is not None:
        run["durationSec"] = seconds

    return {
        "schemaVersion": SCHEMA_VERSION,
        "run": run,
        "summary": {"overall": _counts(counted_all, len(suppressed_all)),
                    "new": _counts(counted_new, len(suppressed_new))},
        "languages": languages,
        "files": files,
        "mutants": mutants,
    }


def write_report(project_root: Path, artifacts_dir: Path, report: dict) -> Path:
    """Write to the STABLE path (overwritten every run, committed to the PR)
    plus a per-run archive copy. Returns the stable path."""
    project_root = Path(project_root)
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    stable = project_root / REPORT_RELPATH
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_text(text, encoding="utf-8")
    archive = Path(artifacts_dir) / "sonar" / "mutation-report.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(text, encoding="utf-8")
    return stable


def find_properties_dir(project_root: Path):
    """Directory holding sonar-project.properties: the project root, else the nearest parent
    up to the git toplevel (a monorepo keeps one file at the repo root while the package
    lives in a subdirectory). None when there is none."""
    d = Path(project_root).resolve()
    while True:
        if (d / "sonar-project.properties").exists():
            return d
        if (d / ".git").exists() or d.parent == d:
            return None
        d = d.parent


def report_path_from(props_dir: Path, project_root: Path) -> str:
    """The stable report path as sonar-project.properties must spell it: relative to the
    directory the scanner runs from (where the properties file lives)."""
    stable = Path(project_root).resolve() / REPORT_RELPATH
    return Path(os.path.relpath(stable, Path(props_dir).resolve())).as_posix()


def wire_properties(project_root: Path, props_dir: Path = None) -> str:
    """Make `sonar.mutation.reportPath` point at the stable report in
    sonar-project.properties, so CI's next scan keeps the measures instead of
    wiping them. Returns 'added', 'updated', 'present' or 'no-properties-file'."""
    props_dir = Path(props_dir) if props_dir else (find_properties_dir(project_root) or Path(project_root))
    props = props_dir / "sonar-project.properties"
    if not props.exists():
        return "no-properties-file"
    lines = props.read_text(encoding="utf-8").splitlines()
    want = f"{PROPERTY}={report_path_from(props_dir, project_root)}"
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == PROPERTY:
            if line.strip() == want:
                return "present"
            lines[i] = want
            props.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "updated"
    props.write_text("\n".join(lines) + ("\n" if lines else "") + want + "\n", encoding="utf-8")
    return "added"

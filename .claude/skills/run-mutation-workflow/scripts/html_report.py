# -*- coding: utf-8 -*-
"""
Step 6b (optional, always run after token_report.py): render a single,
self-contained HTML report for one run, next to its other artifacts.

No LLM involved -- pure data formatting, same spirit as report.py. Reads only
files that Steps 1-6 already wrote for this run:

    gate.json           verdict, round/phase, per-language scores, test files
    results.json         summary_before/after, survivors, tests-added count
    telemetry.json        per-step wall clock + measured orchestration tokens
    report.txt             (parsed only as a fallback if the JSON is missing)
    mutation-wizard.log    jira-updater adoption line, "Ran N tests" line
    pit*/**/*.log           pitest's own per-mutator KILLED/SURVIVED breakdown

Round-aware: when only round 1 ran, the report shows a single mutation score
(no before -> after arrow), "New Tests Added" is 0, and the "Tests Added
Summary" section is omitted entirely. Round 2 (--verify) adds the before/after
arrow, the round-by-round table's second row, and the tests-added table.

Usage:
    python html_report.py --run-id run-1790310132
    python html_report.py --run-id run-1790310132 --out custom/path.html
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

_HERE = Path(__file__).parent
_SKILLS = _HERE.parent.parent
PROJECT_ROOT = Path(_SKILLS.parent.parent)


# --- small IO helpers ---------------------------------------------------------

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _git(*args) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(PROJECT_ROOT), capture_output=True,
            text=True, timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _fmt_duration(ms) -> str:
    if ms is None:
        return "n/a"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.0f} sec"
    minutes = int(seconds // 60)
    rem = int(round(seconds - minutes * 60))
    return f"{minutes} min {rem} sec"


def _fmt_epoch_ms(ms) -> str:
    if not ms:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value}%"


# --- per-mutator breakdown, parsed from pitest's own log ----------------------

_PIT_MUTATOR_BLOCK = re.compile(
    r"^>\s+(?:org\.pitest[.\w]*\.)?([\w]+?)Mutator(?:_(\w+))?\s*$\n"
    r">>\s+Generated\s+(\d+)\s+Killed\s+(\d+)\s+\((\d+)%\)",
    re.MULTILINE,
)


def _mutator_label(base: str, variant: str) -> str:
    label = base
    if variant:
        label = f"{base} ({variant})"
    return label


def parse_pitest_mutator_stats(log_text: str) -> list:
    """Returns [{mutator, generated, killed, survived, kill_rate}], most-generated first."""
    rows = []
    for m in _PIT_MUTATOR_BLOCK.finditer(log_text):
        base, variant, generated, killed, rate = m.groups()
        generated, killed = int(generated), int(killed)
        rows.append({
            "mutator": _mutator_label(base, variant),
            "generated": generated,
            "killed": killed,
            "survived": generated - killed,
            "kill_rate": float(rate),
        })
    rows.sort(key=lambda r: (-r["generated"], r["mutator"]))
    return rows


def find_mutator_stats(artifacts_dir: Path) -> list:
    """Prefers the round-2 (verify) engine logs when present, else round 1's."""
    for sub in ("pit-verify", "pit"):
        base_dir = artifacts_dir / sub
        if not base_dir.is_dir():
            continue
        rows = []
        for log_path in sorted(base_dir.rglob("*.log")):
            rows.extend(parse_pitest_mutator_stats(_read_text(log_path)))
        if rows:
            return rows
    return []


def mutator_stats_from_results(results: dict) -> list:
    """results.json's "mutator_stats" (grouped by run.py's summarize()) in this report's row shape.

    Preferred over find_mutator_stats(), which parses an engine log that may classify statuses
    differently; this one reconciles with the Executive Summary by construction.
    """
    raw = results.get("mutator_stats") or {}
    rows = []
    for operator, s in raw.items():
        generated = s.get("total", 0)
        killed = s.get("killed", 0)
        rows.append({
            "mutator": operator,
            "generated": generated,
            "killed": killed,
            "survived": s.get("survived", generated - killed),
            "kill_rate": s.get("mutation_score", 0.0),
        })
    rows.sort(key=lambda r: (-r["generated"], r["mutator"]))
    return rows


# --- "Ran N tests" / "Sending N test classes" / jira adoption line -----------

_RAN_TESTS = re.compile(r"Ran\s+(\d+)\s+tests?\b")
_TEST_CLASSES = re.compile(r"Sending\s+(\d+)\s+test classes?\s+to minion")
_JIRA_LINE = re.compile(r"\[jira-updater\]\s+applied label '([\w-]+)' to (\S+)")


def _engine_log_text(artifacts_dir: Path) -> str:
    """Concatenates every engine log, preferring round 2's when present."""
    for sub in ("pit-verify", "pit"):
        base_dir = artifacts_dir / sub
        if not base_dir.is_dir():
            continue
        text = "\n".join(_read_text(p) for p in sorted(base_dir.rglob("*.log")))
        if text:
            return text
    return ""


def parse_total_tests_run(engine_log: str) -> int | None:
    matches = _RAN_TESTS.findall(engine_log)
    return int(matches[-1]) if matches else None


def parse_tests_examined(engine_log: str) -> int | None:
    matches = _TEST_CLASSES.findall(engine_log)
    return int(matches[-1]) if matches else None


def parse_jira(wizard_log: str) -> str | None:
    m = _JIRA_LINE.search(wizard_log)
    return m.group(2) if m else None


# --- test-count-by-file, for the "Tests Added Summary" section ---------------

_TEST_LINE_PATTERNS = {
    "typescript": re.compile(r"^\s*it\s*[\(\`]|^\s*it\.each\s*[\(\`]|^\s*test\s*[\(\`]|^\s*test\.each\s*[\(\`]"),
    "javascript": re.compile(r"^\s*it\s*[\(\`]|^\s*it\.each\s*[\(\`]|^\s*test\s*[\(\`]|^\s*test\.each\s*[\(\`]"),
    "java":       re.compile(r"^\s*@Test\b|^\s*@ParameterizedTest\b"),
    "kotlin":     re.compile(r"^\s*@Test\b|^\s*@ParameterizedTest\b|^\s*fun\s+`"),
    "python":     re.compile(r"^\s*def\s+test_"),
    "csharp":     re.compile(r"^\s*\[Fact\]|\s*\[Theory\]|\s*\[Test\]|\s*\[TestMethod\]"),
    "go":         re.compile(r"^\s*func\s+Test[A-Z]"),
}

_EXT_LANG = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".java": "java", ".kt": "kotlin",
    ".py": "python", ".cs": "csharp", ".go": "go",
}


def _lang_for_test_file(test_file: str) -> str | None:
    return _EXT_LANG.get(Path(test_file).suffix.lower())


def _count_tests_in_text(text: str, lang: str) -> int:
    pat = _TEST_LINE_PATTERNS.get(lang)
    if pat is None or not text:
        return 0
    return sum(1 for line in text.splitlines() if pat.match(line))


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha1("\n".join(text.splitlines()).encode("utf-8")).hexdigest()


def count_tests_before_after(test_files: list, base_ref: str = "HEAD",
                             snapshot: dict = None) -> list:
    """Compare `base_ref`'s version of each test file with the working tree.

    `base_ref` should be results.json["head_commit"], not "HEAD": a later remediation commit would
    make HEAD already contain the tests being measured. A file absent at `base_ref` counts as 0.
    Never raises. `modified` flags content changes with an unchanged test count (e.g. a
    strengthened assertion); line endings alone never trip it.
    """
    rows = []
    for rel_path in test_files:
        lang = _lang_for_test_file(rel_path)
        if lang is None:
            continue
        abs_path = PROJECT_ROOT / rel_path
        after_text = _read_text(abs_path)
        after_count = _count_tests_in_text(after_text, lang)

        snap = (snapshot or {}).get(rel_path)
        if snap:
            # Round 1's own working-tree snapshot: exact even when the file had
            # uncommitted edits before the run, which base_ref cannot see.
            before_count = snap.get("tests", 0)
            added = after_count - before_count
            modified = added == 0 and _digest(after_text) != snap.get("sha1")
        else:  # runs recorded before snapshots existed
            before_text = _git("show", f"{base_ref}:./{Path(rel_path).as_posix()}")
            before_count = _count_tests_in_text(before_text, lang) if before_text else 0
            added = after_count - before_count
            modified = added == 0 and before_text.splitlines() != after_text.splitlines()

        rows.append({
            "file": rel_path,
            "before": before_count,
            "after": after_count,
            "added": added,
            "modified": modified,
        })
    return rows


# --- data assembly -------------------------------------------------------------

def load_run(run_id: str, artifacts_dir: Path) -> dict:
    gate = _read_json(artifacts_dir / "gate.json")
    results = _read_json(artifacts_dir / "results.json")
    telemetry = _read_json(artifacts_dir / "telemetry.json")
    wizard_log = _read_text(artifacts_dir / "mutation-wizard.log")

    is_round2 = bool(gate.get("phase") == "verify" or (artifacts_dir / "pit-verify").is_dir())

    summary_before = results.get("summary_before") or {}
    summary_after = results.get("summary_after") or summary_before
    per_language = results.get("per_language") or gate.get("perLanguage") or {}
    languages = results.get("languages") or gate.get("languages") or []
    tools = results.get("tools") or gate.get("tools") or []
    target_files = results.get("target_files") or gate.get("targetFiles") or []

    steps = telemetry.get("steps") or {}
    orchestration = telemetry.get("orchestration") or {}

    test_files = sorted(set((gate.get("testFiles") or {}).values()))
    tests_added_rows = (count_tests_before_after(test_files, results.get("head_commit") or "HEAD",
                                                 results.get("test_snapshot"))
                        if is_round2 else [])
    # Authoritative "tests added": a git-diff count, not the agent's self-reported --tests-added.
    tests_added_actual = sum(r["added"] for r in tests_added_rows)

    survivors_final = results.get("survivors_after") if is_round2 else results.get("survivors_before")
    survivors_final = survivors_final or []

    engine_log = _engine_log_text(artifacts_dir)
    tests_examined = len(test_files) if test_files else parse_tests_examined(engine_log)

    return {
        "run_id": run_id,
        "artifacts_dir": artifacts_dir,
        "gate": gate,
        "results": results,
        "telemetry": telemetry,
        "steps": steps,
        "orchestration": orchestration,
        "wizard_log": wizard_log,
        "is_round2": is_round2,
        "summary_before": summary_before,
        "summary_after": summary_after,
        "per_language": per_language,
        "languages": languages,
        "tools": tools,
        "target_files": target_files,
        "test_files": test_files,
        "tests_added_rows": tests_added_rows,
        "tests_added_actual": tests_added_actual,
        "survivors_final": survivors_final,
        "mutator_stats": (mutator_stats_from_results(results)
                          or find_mutator_stats(artifacts_dir)),
        "total_tests_run": parse_total_tests_run(engine_log),
        "tests_examined": tests_examined,
        "jira_id": parse_jira(wizard_log),
        "threshold": gate.get("threshold", results.get("threshold", 80)),
        "verdict": gate.get("verdict", results.get("gate", "UNKNOWN")),
    }


# --- HTML rendering -------------------------------------------------------------

# Emitted as an HTML comment in every genuine report and checked by `--verify`, to tell a
# generated report from a hand-written one.
STANDARD_FORMAT_MARKER = "MUTATION_REPORT_STANDARD_FORMAT_v1"

CSS = """
:root{
  --bg:#f5f6f8; --surface:#ffffff; --surface-2:#eef0f3; --text:#1b2430;
  --muted:#606a78; --border:#dde1e7; --accent:#1f7a6c; --accent-soft:#e2f1ee;
  --num:#8a4b00; --kill:#1f9d55; --kill-soft:#e5f6ea; --survive:#c23b34;
  --survive-soft:#fbe9e8; --nocov:#b4790a; --nocov-soft:#fbf1de; --neutral:#8a93a3;
  --shadow: 0 1px 2px rgba(20,26,34,.04), 0 6px 20px -8px rgba(20,26,34,.12);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#12161c; --surface:#1a2027; --surface-2:#212832; --text:#e7ebf1;
    --muted:#93a0b3; --border:#2a323d; --accent:#4fcdb8; --accent-soft:#173029;
    --num:#e0a35a; --kill:#4cd07c; --kill-soft:#173023; --survive:#f0847d;
    --survive-soft:#33201f; --nocov:#e0ab4c; --nocov-soft:#2f2717; --neutral:#7d879a;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 24px -8px rgba(0,0,0,.5);
  }
}
:root[data-theme="dark"]{
  --bg:#12161c; --surface:#1a2027; --surface-2:#212832; --text:#e7ebf1;
  --muted:#93a0b3; --border:#2a323d; --accent:#4fcdb8; --accent-soft:#173029;
  --num:#e0a35a; --kill:#4cd07c; --kill-soft:#173023; --survive:#f0847d;
  --survive-soft:#33201f; --nocov:#e0ab4c; --nocov-soft:#2f2717; --neutral:#7d879a;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 24px -8px rgba(0,0,0,.5);
}
body{ margin:0; padding:0; background:var(--bg); color:var(--text);
  font-family:'Karla','Segoe UI',system-ui,sans-serif; padding-inline:16px; padding-block:28px; }
h1,h2,h3{ font-family:'Manrope',system-ui,sans-serif; text-wrap:balance; margin:0; }
.mono{ font-family:'IBM Plex Mono',ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.wrap{ max-width:1040px; margin:0 auto; }
header{ display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between;
  gap:16px; padding-bottom:20px; margin-bottom:24px; border-bottom:1px solid var(--border); }
.eyebrow{ font-size:.72rem; letter-spacing:.09em; text-transform:uppercase; color:var(--muted);
  font-weight:600; margin-bottom:6px; }
h1{ font-size:1.55rem; font-weight:700; }
.run-meta{ display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:8px; font-size:.85rem; color:var(--muted); }
.run-meta strong{ color:var(--text); font-weight:600; }
.header-right{ display:flex; flex-direction:column; align-items:flex-end; gap:8px; }
.badge{ display:inline-flex; align-items:center; gap:6px; font-size:.78rem; font-weight:700;
  padding:5px 12px; border-radius:100px; letter-spacing:.02em; }
.badge.pass{ background:var(--kill-soft); color:var(--kill); }
.badge.fail{ background:var(--survive-soft); color:var(--survive); }
.badge.review{ background:var(--nocov-soft); color:var(--nocov); }
.badge.sample{ background:var(--surface-2); color:var(--muted); font-weight:600; border:1px dashed var(--border); }
.badge .dot{ width:6px; height:6px; border-radius:50%; background:currentColor; }
section{ margin-bottom:36px; }
.section-head{ display:flex; align-items:baseline; gap:10px; margin-bottom:14px; }
.section-num{ font-family:'IBM Plex Mono',monospace; color:var(--accent); font-size:.8rem; font-weight:600; }
h2{ font-size:1.05rem; font-weight:700; }
.section-sub{ color:var(--muted); font-size:.83rem; margin-left:auto; }
.stats{ display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:1px;
  background:var(--border); border:1px solid var(--border); border-radius:12px; overflow:hidden;
  box-shadow:var(--shadow); }
.stat{ background:var(--surface); padding:16px 16px 14px; display:flex; flex-direction:column;
  gap:6px; min-width:0; }
.stat-label{ font-size:.72rem; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
.stat-value{ font-size:1.3rem; font-weight:700; font-family:'IBM Plex Mono',monospace;
  font-variant-numeric:tabular-nums; line-height:1.15; word-break:break-word; }
.stat-value.accent{ color:var(--accent); }
.stat-value small{ font-size:.7rem; font-weight:600; color:var(--muted); margin-left:2px; }
.table-card{ background:var(--surface); border:1px solid var(--border); border-radius:12px;
  overflow:hidden; box-shadow:var(--shadow); }
.table-scroll{ overflow-x:auto; }
table{ width:100%; border-collapse:collapse; font-size:.86rem; min-width:520px; }
thead th{ text-align:left; font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; font-weight:700;
  color:var(--muted); padding:11px 16px; background:var(--surface-2); border-bottom:1px solid var(--border);
  white-space:nowrap; }
tbody td{ padding:11px 16px; border-bottom:1px solid var(--border); vertical-align:middle; }
tbody tr:last-child td{ border-bottom:none; }
tbody tr.total td{ font-weight:700; background:var(--surface-2); }
td.num, th.num{ text-align:right; font-family:'IBM Plex Mono',monospace; font-variant-numeric:tabular-nums; }
td.file{ font-family:'IBM Plex Mono',monospace; font-size:.82rem; }
.pill{ display:inline-flex; padding:2px 9px; border-radius:100px; font-size:.75rem; font-weight:700; }
.pill.add{ background:var(--kill-soft); color:var(--kill); }
.pill.flat{ background:var(--surface-2); color:var(--muted); }
.steps{ display:flex; flex-direction:column; gap:1px; background:var(--border);
  border:1px solid var(--border); border-radius:12px; overflow:hidden; box-shadow:var(--shadow); }
.step{ background:var(--surface); display:flex; gap:14px; padding:14px 16px; align-items:flex-start; }
.step-flag{ flex:none; width:22px; height:22px; border-radius:6px; display:flex; align-items:center;
  justify-content:center; font-size:.7rem; font-weight:700; margin-top:1px; }
.step-flag.open{ background:var(--survive-soft); color:var(--survive); }
.step-flag.watch{ background:var(--nocov-soft); color:var(--nocov); }
.step-flag.done{ background:var(--kill-soft); color:var(--kill); }
.step-body{ display:flex; flex-direction:column; gap:2px; }
.step-title{ font-weight:600; font-size:.88rem; }
.step-detail{ color:var(--muted); font-size:.82rem; }
.empty{ padding:16px; color:var(--muted); font-size:.85rem; }
footer{ margin-top:8px; padding-top:16px; border-top:1px solid var(--border); color:var(--muted); font-size:.78rem; }
@media (max-width:640px){ header{ align-items:flex-start; flex-direction:column; } .header-right{ align-items:flex-start; } h1{ font-size:1.25rem; } }
"""


def _stat(label: str, value: str, accent: bool = False, small_suffix: str = "") -> str:
    cls = "stat-value accent" if accent else "stat-value"
    suffix = f"<small>{escape(small_suffix)}</small>" if small_suffix else ""
    return (f'<div class="stat"><span class="stat-label">{escape(label)}</span>'
            f'<span class="{cls}">{value}{suffix}</span></div>')


def _section(num: str, title: str, sub: str, body: str) -> str:
    sub_html = f'<span class="section-sub">{escape(sub)}</span>' if sub else ""
    return (f'<section><div class="section-head"><span class="section-num">{num}</span>'
            f'<h2>{escape(title)}</h2>{sub_html}</div>{body}</section>')


def _table(headers: list, rows: list, total_row: list = None) -> str:
    thead = "".join(
        f'<th class="num">{escape(h)}</th>' if i > 0 else f'<th>{escape(h)}</th>'
        for i, h in enumerate(headers)
    )
    body_rows = "".join(
        "<tr>" + "".join(
            f'<td class="num">{c}</td>' if i > 0 else f'<td class="file">{c}</td>'
            for i, c in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    if not rows:
        body_rows = f'<tr><td colspan="{len(headers)}" class="empty">No data available for this run.</td></tr>'
    if total_row:
        body_rows += "<tr class=\"total\">" + "".join(
            f'<td class="num">{c}</td>' if i > 0 else f'<td>{c}</td>'
            for i, c in enumerate(total_row)
        ) + "</tr>"
    return (f'<div class="table-card"><div class="table-scroll"><table>'
            f'<thead><tr>{thead}</tr></thead><tbody>{body_rows}</tbody></table></div></div>')


def build_executive_summary(run: dict) -> str:
    gate = run["gate"]
    summary_before, summary_after = run["summary_before"], run["summary_after"]
    telemetry = run["telemetry"]

    if run["is_round2"]:
        score_html = (f'{summary_before.get("mutation_score", "n/a")}<small>%</small> '
                      f'&rarr; {summary_after.get("mutation_score", "n/a")}<small>%</small>')
    else:
        score_html = f'{summary_before.get("mutation_score", "n/a")}<small>%</small>'

    tiles = [
        _stat("Language", ", ".join(sorted(run["languages"])).title() or "n/a"),
        _stat("Engine", ", ".join(sorted(run["tools"])) or "n/a"),
        _stat("Run Started (UTC)", _fmt_epoch_ms(telemetry.get("runStartMs"))),
        _stat("Run Completed (UTC)", _fmt_epoch_ms(telemetry.get("runEndMs"))),
    ]
    tiles.append(
        f'<div class="stat"><span class="stat-label">Mutation Score</span>'
        f'<span class="stat-value accent" style="font-size:1.1rem">{score_html}</span></div>'
    )
    tiles += [
        _stat("Mutations Generated", str(summary_after.get("total", "n/a"))),
        _stat("Mutations Killed", str(summary_after.get("killed", "n/a"))),
        _stat("Mutations Survived", str(summary_after.get("survived", "n/a"))),
        _stat("Tests Examined", str(run["tests_examined"]) if run["tests_examined"] is not None else "n/a"),
        _stat("Total Tests Run", str(run["total_tests_run"]) if run["total_tests_run"] is not None else "n/a"),
        _stat("New Tests Added", f'+{run["tests_added_actual"]}'
              if run["is_round2"] else "0", accent=run["is_round2"]),
    ]
    if run["jira_id"]:
        tiles.append(_stat("JIRA Story ID", run["jira_id"]))
    return f'<div class="stats">{"".join(tiles)}</div>'


def build_remediation_rounds(run: dict) -> str:
    steps = run["steps"]
    summary_before, summary_after = run["summary_before"], run["summary_after"]
    rows = [[
        "Round 1",
        _pct(summary_before.get("mutation_score")),
        "&ndash;" if not run["is_round2"] else "&ndash;",
        str(summary_before.get("killed", "n/a")),
        str(summary_before.get("survived", "n/a")),
        "&ndash;",
        _fmt_duration((steps.get("step4") or {}).get("wallClockMs")),
    ]]
    if run["is_round2"]:
        added = run["tests_added_actual"]
        rows.append([
            "Round 2",
            _pct(summary_before.get("mutation_score")),
            _pct(summary_after.get("mutation_score")),
            str(summary_after.get("killed", "n/a")),
            str(summary_after.get("survived", "n/a")),
            f"+{added}" if added else "+0",
            _fmt_duration((steps.get("step4b") or {}).get("wallClockMs")),
        ])
    sub = "Round 1 only" if not run["is_round2"] else "Round 1 + Round 2 (combined)"
    return _section("02", "Remediation Rounds", f"Round-by-round breakdown · {sub}",
                    _table(["Round", "Score before", "Score after", "Killed",
                            "Survived", "Tests added", "Duration"], rows))


def build_tests_added_summary(run: dict) -> str:
    rows_data = run["tests_added_rows"]
    rows = []
    total_before = total_after = total_modified = 0
    for r in rows_data:
        total_before += r["before"]
        total_after += r["after"]
        total_modified += 1 if r["modified"] else 0
        pill = (f'<span class="pill add">+{r["added"]}</span>' if r["added"] > 0
                else f'<span class="pill flat">+{r["added"]}</span>')
        rows.append([escape(r["file"]), str(r["before"]), str(r["after"]), pill,
                     "Yes" if r["modified"] else "No"])
    total_row = ["Total", str(total_before), str(total_after),
                 str(total_after - total_before), str(total_modified)]
    return _section("03", "Tests Added Summary", "Remediation round · per test file",
                    _table(["Test file", "Tests before", "Tests after", "Added", "Modified"],
                           rows, total_row=total_row if rows else None))


def build_mutator_performance(run: dict, section_num: str) -> str:
    stats = run["mutator_stats"]
    rows = [[escape(s["mutator"]), str(s["generated"]), str(s["killed"]),
             str(s["survived"]), f'{s["kill_rate"]:.1f}%'] for s in stats]
    total_row = None
    if stats:
        gen = sum(s["generated"] for s in stats)
        killed = sum(s["killed"] for s in stats)
        surv = sum(s["survived"] for s in stats)
        rate = round(100 * killed / gen, 1) if gen else 0
        total_row = ["Total", str(gen), str(killed), str(surv), f"{rate}%"]
    return _section(section_num, "Mutator Performance Analysis", "Kill rate by mutator type",
                    _table(["Mutator", "Generated", "Killed", "Survived", "Kill rate"],
                           rows, total_row=total_row))


_PHASE_LABELS = [
    ("step2", "Scope resolution (target files)"),
    ("step1", "Language & engine detection"),
    ("step3", "Arid filter"),
    ("step4", "Mutation run (round 1)"),
    ("step5_handoff", "Remediation hand-off (prompt generation)"),
    ("step4b", "Mutation run (round 2 / verify)"),
]


def build_timing_analysis(run: dict, section_num: str) -> str:
    steps = run["steps"]
    rows, total_ms = [], 0
    for key, label in _PHASE_LABELS:
        entry = steps.get(key)
        if not entry:
            continue
        ms = entry.get("wallClockMs", 0)
        total_ms += ms
        rows.append([label, _fmt_duration(ms)])
    total_row = ["Total", _fmt_duration(total_ms)] if rows else None
    return _section(section_num, "Timing Analysis", "Wall-clock duration per phase",
                    _table(["Phase", "Duration"], rows, total_row=total_row))


def _token_row(label: str, t: dict) -> list:
    return [
        label,
        f'{t.get("inputTokens", 0):,}',
        f'{t.get("outputTokens", 0):,}',
        f'{t.get("inputTokens", 0) + t.get("outputTokens", 0):,}',
    ]


def _measured_zero(orch: dict) -> bool:
    """token_report.py --write ran but found nothing in the run's window (vs. never having run)."""
    return bool(orch) and "messages" in orch and not orch["messages"]


def build_token_consumption(run: dict, section_num: str) -> str:
    """Total = inputTokens + outputTokens, the two columns shown; cache tokens are not part of this report.

    When token_report.py captured a round split, round 1 and round 2 are separate rows.
    """
    orch = run["orchestration"]
    measured = bool(orch and orch.get("messages"))
    rounds = orch.get("rounds") if measured else None
    rows, total_row = [], None
    if measured:
        if rounds and rounds.get("round1") and rounds.get("round2"):
            rows.append(_token_row("Orchestration (round 1: scoring + hand-off)",
                                    rounds["round1"]))
            rows.append(_token_row("Orchestration (round 2: test writing + verify)",
                                    rounds["round2"]))
        else:
            rows.append(_token_row("Orchestration (agent)", orch))
        rows.append(["Script (steps 1-4, 6)", "0", "0", "0"])
        total_row = _token_row("Total", orch)
    if measured:
        sub = "LLM usage · orchestration"
    elif _measured_zero(orch):
        sub = "Measured -- no agent activity recorded during this run's window (0 tokens)"
    else:
        sub = "Not yet measured -- run token_report.py --write before generating this report"
    return _section(section_num, "Token Consumption", sub,
                    _table(["Phase", "inputTokens", "outputTokens", "totalTokens"],
                           rows, total_row=total_row))


def build_next_steps(run: dict, section_num: str) -> str:
    steps_html = []
    n = 0
    survivors = [s for s in run["survivors_final"] if s.get("category") == "survived"]
    no_cov = [s for s in run["survivors_final"] if s.get("category") == "no-coverage"]

    if survivors or no_cov:
        n += 1
        ops = sorted({s.get("operator", "?") for s in survivors + no_cov})
        detail = (f'{len(survivors)} SURVIVED and {len(no_cov)} NO_COVERAGE mutant(s) remain '
                  f'-- operators involved: {", ".join(ops[:6])}'
                  f'{"..." if len(ops) > 6 else ""}.')
        steps_html.append(("open", f"Review {len(survivors) + len(no_cov)} remaining mutant(s)", detail))

    flat_files = [r["file"] for r in run["tests_added_rows"] if r["added"] == 0]
    if flat_files:
        n += 1
        steps_html.append(("watch", f"Get {len(flat_files)} test file(s) covering new branches",
                            "0 tests were added this round in: " + ", ".join(flat_files[:4])
                            + (", ..." if len(flat_files) > 4 else "") + " -- confirm scope or extend next pass."))

    if run["verdict"] == "PASS":
        n += 1
        jira_note = f"Attach this report to {run['jira_id']}" if run["jira_id"] else "Attach this report to the tracking issue"
        steps_html.append(("done", jira_note,
                            f'Gate passed at {run["summary_after"].get("mutation_score", "n/a")}% '
                            f'against a {run["threshold"]}% threshold.'))

    # `added` is the git-diff-verified count, so this step only appears when tests were actually added.
    added = run["tests_added_actual"]
    if run["is_round2"] and added:
        n += 1
        files = [r["file"] for r in run["tests_added_rows"] if r["added"] > 0]
        file_list = ", ".join(files[:4]) + (", ..." if len(files) > 4 else "")
        steps_html.append(("done", f"Merge the {added} auto-generated test(s)",
                            f"Have a reviewer confirm the new assertions in: {file_list} before merge."))

    if run["verdict"] == "MANUAL_REVIEW":
        n += 1
        steps_html.append(("open", "Manual review required",
                            f'Score is still below {run["threshold"]}% after 2 rounds -- remaining '
                            "mutants are likely equivalent. A human must accept, exclude, or refactor."))

    if not steps_html:
        body = '<div class="steps"><div class="empty">No follow-up actions identified for this run.</div></div>'
    else:
        items = "".join(
            f'<div class="step"><span class="step-flag {flag}">{i+1}</span>'
            f'<div class="step-body"><span class="step-title">{escape(title)}</span>'
            f'<span class="step-detail">{escape(detail)}</span></div></div>'
            for i, (flag, title, detail) in enumerate(steps_html)
        )
        body = f'<div class="steps">{items}</div>'
    return _section(section_num, "Next Steps", "Follow-up actions from this run", body)


def render(run: dict, *, sample: bool = False,
           repo_override: str = None, branch_override: str = None,
           commit_override: str = None) -> str:
    gate, threshold, verdict = run["gate"], run["threshold"], run["verdict"]
    badge_cls = {"PASS": "pass", "FAIL": "fail", "MANUAL_REVIEW": "review"}.get(verdict, "review")
    repo = repo_override if repo_override is not None else Path(PROJECT_ROOT).name
    branch = branch_override if branch_override is not None else (_git("rev-parse", "--abbrev-ref", "HEAD") or "n/a")
    commit = commit_override if commit_override is not None else (_git("rev-parse", "--short", "HEAD") or "n/a")
    scope_label = "Round 2" if run["is_round2"] else "Round 1"
    scope_detail = "(Round 1 + Round 2 combined)" if run["is_round2"] else "(Round 1 only)"
    sample_badge = ('<span class="badge sample"><span class="dot"></span>'
                     'Sample data &mdash; illustrative</span>') if sample else ""

    header = f"""
  <header>
    <div>
      <div class="eyebrow">Mutation Testing &middot; Agent Run Report</div>
      <h1>Mutation Run Report</h1>
      <div class="run-meta">
        <span>Repository <strong class="mono">{escape(repo)}</strong></span>
        <span>&middot;</span>
        <span>Branch <strong class="mono">{escape(branch)}</strong></span>
        <span>&middot;</span>
        <span>Commit <strong class="mono">{escape(commit)}</strong></span>
        <span>&middot;</span>
        <span>Run ID <strong class="mono">{escape(run["run_id"])}</strong></span>
      </div>
      <div class="run-meta">
        <span>Report scope <strong>{scope_label}</strong> {scope_detail}</span>
      </div>
    </div>
    <div class="header-right">
      {sample_badge}
      <span class="badge {badge_cls}"><span class="dot"></span>Gate: {escape(str(verdict))} (threshold {threshold}%)</span>
    </div>
  </header>
"""

    sections = [build_executive_summary(run)]
    sections[0] = _section("01", "Executive Summary", "", sections[0])
    sections.append(build_remediation_rounds(run))

    next_num = 3
    if run["is_round2"]:
        sections.append(build_tests_added_summary(run))
        next_num += 1
    sections.append(build_mutator_performance(run, f"{next_num:02d}")); next_num += 1
    sections.append(build_timing_analysis(run, f"{next_num:02d}")); next_num += 1
    sections.append(build_token_consumption(run, f"{next_num:02d}")); next_num += 1
    sections.append(build_next_steps(run, f"{next_num:02d}")); next_num += 1

    footer = (
        '<footer>Generated by html_report.py from this run\'s own '
        '<span class="mono">gate.json</span> / <span class="mono">results.json</span> / '
        '<span class="mono">telemetry.json</span> / <span class="mono">mutation-wizard.log</span>. '
        'No LLM involved in producing this page.</footer>'
    )

    return f"""<!doctype html><!-- {STANDARD_FORMAT_MARKER}: run {escape(run["run_id"])} -->
<html><head><meta charset=utf8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Mutation Run Report &mdash; {escape(run["run_id"])}</title>
<meta name="description" content="Mutation testing run report: executive summary, remediation rounds, tests-added summary, mutator performance, timing, and token usage.">
<style>{CSS}</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@600;700;800&family=Karla:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600;700&display=swap">
</head><body>
<div class="wrap">
{header}
{"".join(sections)}
{footer}
</div>
</body></html>"""


# --- sample/design-reference data -----------------------------------------------

def build_sample_run() -> dict:
    """Fixed, clearly-fabricated data run through the same render()/build_*() code as real reports.

    For maintainers previewing the layout via `--sample --out <path>`; never shipped with the plugin.
    """
    def _ms(y, mo, d, h, mi, s):
        return round(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)

    summary_before = {"total": 46, "killed": 33, "survived": 13, "no_coverage": 0,
                       "errors": 0, "mutation_score": 58.7, "test_strength": 71.7, "kill_ratio": 58.7}
    summary_after = {"total": 46, "killed": 41, "survived": 5, "no_coverage": 0,
                      "errors": 0, "mutation_score": 87.0, "test_strength": 89.1, "kill_ratio": 87.0}
    mutator_stats = {
        "ArithmeticOperator":   {"total": 14, "killed": 12, "survived": 2, "no_coverage": 0, "errors": 0, "mutation_score": 85.7},
        "ConditionalExpression": {"total": 10, "killed": 9, "survived": 1, "no_coverage": 0, "errors": 0, "mutation_score": 90.0},
        "EqualityOperator":     {"total": 8, "killed": 8, "survived": 0, "no_coverage": 0, "errors": 0, "mutation_score": 100.0},
        "BooleanLiteral":       {"total": 6, "killed": 5, "survived": 1, "no_coverage": 0, "errors": 0, "mutation_score": 83.3},
        "StringLiteral":        {"total": 5, "killed": 4, "survived": 1, "no_coverage": 0, "errors": 0, "mutation_score": 80.0},
        "BlockStatement":       {"total": 3, "killed": 3, "survived": 0, "no_coverage": 0, "errors": 0, "mutation_score": 100.0},
    }
    survivors_final = (
        [{"category": "survived", "operator": "ArithmeticOperator"}] * 2
        + [{"category": "survived", "operator": "ConditionalExpression"}]
        + [{"category": "survived", "operator": "BooleanLiteral"}]
        + [{"category": "survived", "operator": "StringLiteral"}]
    )
    tests_added_rows = [
        {"file": "src/__tests__/calculator.test.ts", "before": 6, "after": 8, "added": 2, "modified": False},
        {"file": "src/__tests__/stringUtils.test.ts", "before": 10, "after": 12, "added": 2, "modified": False},
        {"file": "src/__tests__/validators.test.ts", "before": 14, "after": 14, "added": 0, "modified": False},
    ]
    steps = {
        "step2": {"wallClockMs": 1000},
        "step1": {"wallClockMs": 8000},
        "step3": {"wallClockMs": 23000},
        "step4": {"wallClockMs": 50000},
        "step5_handoff": {"wallClockMs": 64000},
        "step4b": {"wallClockMs": 18000},
    }
    orchestration = {
        "messages": 14, "inputTokens": 12430, "cacheCreationTokens": 8210,
        "cacheReadTokens": 41560, "outputTokens": 3180, "totalTokens": 65380,
        # Illustrates the round split: round 1 (scoring + hand-off) is a single
        # cheap decision, round 2 (test writing + verify) does the real
        # generative work and dominates the total -- the two rows must still
        # sum to the aggregate totals above.
        "rounds": {
            "round1": {"messages": 2, "inputTokens": 430, "cacheCreationTokens": 210,
                       "cacheReadTokens": 1560, "outputTokens": 180, "totalTokens": 2380},
            "round2": {"messages": 12, "inputTokens": 12000, "cacheCreationTokens": 8000,
                       "cacheReadTokens": 40000, "outputTokens": 3000, "totalTokens": 63000},
        },
    }

    return {
        "run_id": "sample-illustrative",
        "gate": {},
        "results": {"new_tests_count": 4},
        "telemetry": {
            "runStartMs": _ms(2026, 9, 24, 9, 12, 3),
            "runEndMs": _ms(2026, 9, 24, 9, 14, 47),
        },
        "steps": steps,
        "orchestration": orchestration,
        "is_round2": True,
        "summary_before": summary_before,
        "summary_after": summary_after,
        "per_language": {"typescript": summary_after},
        "languages": ["typescript"],
        "tools": ["stryker"],
        "target_files": ["src/calculator.ts"],
        "test_files": [r["file"] for r in tests_added_rows],
        "tests_added_rows": tests_added_rows,
        "tests_added_actual": sum(r["added"] for r in tests_added_rows),
        "survivors_final": survivors_final,
        "mutator_stats": mutator_stats_from_results({"mutator_stats": mutator_stats}),
        "total_tests_run": 132,
        "tests_examined": 18,
        "jira_id": "PROJ-4821",
        "threshold": 80,
        "verdict": "PASS",
    }


# --- CLI -----------------------------------------------------------------------

_REQUIRED_SECTIONS = (
    "Executive Summary", "Remediation Rounds", "Mutator Performance Analysis",
    "Timing Analysis", "Token Consumption", "Next Steps",
)


def verify_report(html_path: Path) -> list:
    """Check a rendered file came from this script, not hand-written. Returns problems (empty = passed); never raises."""
    problems = []
    if not html_path.is_file():
        return [f"no such file: {html_path}"]
    text = _read_text(html_path)
    if STANDARD_FORMAT_MARKER not in text:
        problems.append(
            f"missing the {STANDARD_FORMAT_MARKER} marker -- this file was not "
            "produced by html_report.py. Re-run it; do not describe this file's "
            "contents as the mutation report."
        )
    if "Generated by html_report.py" not in text:
        problems.append("missing the standard footer sentence")
    for name in _REQUIRED_SECTIONS:
        if f">{name}</h2>" not in text:
            problems.append(f"missing required section: {name}")
    return problems


def main() -> int:
    global PROJECT_ROOT
    parser = argparse.ArgumentParser(description="Render a self-contained HTML report for one mutation run")
    parser.add_argument("--run-id", help="Run ID, e.g. run-1790310132")
    parser.add_argument("--out", default=None,
                        help="Output path (default: mutation-reports/<run-id>/mutation-report.html)")
    parser.add_argument("--verify", metavar="PATH",
                        help="Check an existing HTML file is genuinely this script's "
                             "output (has the standard marker, footer, and every "
                             "required section) instead of rendering a new one. "
                             "Exits 0 if it passes, 1 with the reasons if not.")
    parser.add_argument("--sample", action="store_true",
                        help="Maintainer preview: render the layout with fixed "
                             "illustrative data instead of a real run's. Requires "
                             "--out; the file is not shipped with the plugin and "
                             "should not be written into a project.")
    parser.add_argument("--project-root", default=None,
                        help="Override the auto-detected project root. PROJECT_ROOT is "
                             "normally derived from this file's own install depth "
                             "(.claude/skills/run-mutation-workflow/scripts/), which is "
                             "wrong when run.py invokes this script automatically from "
                             "a different install layout (its own source tree, a test's "
                             "synthetic repo) -- run.py always passes its own, correctly "
                             "git-toplevel-anchored PROJECT_ROOT here instead of relying "
                             "on this script's guess.")
    args = parser.parse_args()
    if args.project_root:
        PROJECT_ROOT = Path(args.project_root)

    if args.verify:
        problems = verify_report(Path(args.verify))
        if problems:
            print(f"NOT a standard-format report: {args.verify}")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"OK -- standard-format report: {args.verify}")
        return 0

    if args.sample:
        if not args.out:
            # No default location: a fabricated-data report must not land in a user's project looking real.
            parser.error("--sample requires --out <path> (outside any project you "
                         "care about -- it is a maintainer preview, not a report)")
        run = build_sample_run()
        html = render(run, sample=True, repo_override="acme-corp/billing-service",
                      branch_override="feature/refund-validation", commit_override="a3f9c1d")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"Sample report saved to: {out_path}")
        problems = verify_report(out_path)
        if problems:  # pragma: no cover -- same defensive check as the real path
            print("WARNING: the sample this script just wrote failed its own check:")
            for p in problems:
                print(f"  - {p}")
            return 1
        return 0

    if not args.run_id:
        parser.error("--run-id is required unless --verify or --sample is given")

    artifacts_dir = PROJECT_ROOT / "mutation-reports" / args.run_id
    if not artifacts_dir.is_dir():
        print(f"No run directory at {artifacts_dir}")
        return 1

    run = load_run(args.run_id, artifacts_dir)
    html = render(run)

    # One-shot render: running token_report.py --write afterwards does not update this file; re-run this command.
    orch = run.get("orchestration") or {}
    if _measured_zero(orch):
        print("Note: token_report.py found no agent activity in this run's window "
              "-- Token Consumption shows 0 tokens.")
    elif not orch.get("messages"):
        print(
            "WARNING: Token Consumption will show 'Not yet measured' -- "
            "telemetry.json has no orchestration data yet. If you intend to "
            "measure it, run token_report.py --write BEFORE this command, "
            "then re-run this command. Running token_report.py --write "
            "after this HTML already exists will not update it."
        )

    out_path = Path(args.out) if args.out else artifacts_dir / "mutation-report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML report saved to: {out_path}")

    problems = verify_report(out_path)
    if problems:  # pragma: no cover -- defensive; render() and verify_report()
                  # agreeing is the whole point, this only fires if they drift
        print("WARNING: the file this script just wrote failed its own check:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

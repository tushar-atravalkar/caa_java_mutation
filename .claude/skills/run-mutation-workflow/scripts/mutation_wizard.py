# -*- coding: utf-8 -*-
"""
Wizard-style wrapper around run.py -- same idea as the /wizard skill: move the
noisy, blocking part of the work to its own process, show only clean stage
progress while it runs, and hand back one small result instead of the raw
transcript.

run.py streams raw Maven/PIT chatter straight into whatever process invokes it
-- fine for a human at a terminal, expensive when that process is an LLM agent
paying per token to read it. This wrapper runs run.py in a child process (its
own "terminal"), relays only wizard-style stage announcements while the full
transcript goes to a log file, then hands back ONE clean result: a short
summary plus a single `MUTATION_RESULT: {...}` JSON line carrying the verdict
and score. That line is the only thing the caller needs to parse -- it never
has to read gate.json or raw script output itself.

Exit codes (stable contract for CI scripts):
    0   PASS           gate passed, or nothing in scope to mutate
    1   ERROR          missing engine, engine crash, no language detected
    2   FAIL           gate failed (score below threshold)
    3   MANUAL_REVIEW  gate still failed after round 2 -- no round 3
    4   ALREADY_RUNNING  a run is already in progress in this project
    5   ALREADY_VERIFIED   --verify refused: that run already used its round 2
    6   SONAR_UPLOAD_FAILED  the final Sonar upload failed; the mutation verdict is unchanged
    7   SCOPE_MISMATCH  --pr/--commit named code this checkout does not hold (wrong branch,
                       or the local changes differ); MUTATION_RESULT carries the options
    130 USER_DECLINED  user declined at the interactive scope-confirmation prompt
    255 INTERNAL       unhandled exception in this wrapper (bug, not a gate result)

Usage:
    python mutation_wizard.py --module impl --threshold 80
    python mutation_wizard.py --verify --run-id run-1 --module impl --round 2
    python mutation_wizard.py --interactive --module impl   # pause before LLM handoff

Any flag not recognised here is passed through to run.py unmodified.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info < (3, 9):  # say so plainly instead of dying later on a syntax or typing error
    sys.stderr.write(f"mutation_wizard.py needs Python 3.9 or newer; this is "
                     f"{sys.version.split()[0]}. Run it with a newer python3 / py.\n")
    sys.exit(255)

_HERE = Path(__file__).parent
RUN_PY = str(_HERE / "run.py")  # sibling script: location-relative is correct here

try:  # package-relative imports -- resolve at any install location
    from . import observability
    from . import token_report
    from . import changed_files_scanner
    from . import java_tests
    from . import lang_profiles
    from . import scope_resolver
except ImportError:  # direct invocation: py .../mutation_wizard.py
    sys.path.insert(0, str(_HERE))
    import observability  # noqa: E402
    import token_report  # noqa: E402
    import changed_files_scanner  # noqa: E402
    import java_tests  # noqa: E402
    import lang_profiles  # noqa: E402
    import scope_resolver  # noqa: E402

# Root of the repo under test -- resolved from the invocation context (env var,
# git toplevel, cwd), never from where this script happens to be installed.
PROJECT_ROOT = changed_files_scanner.resolve_project_root()

# --- dynamic timeout estimation ------------------------------------------------

# One regex per language that matches a single test-case declaration line.
_TEST_LINE_PATTERNS = {
    "typescript": re.compile(r"^\s*it\s*[\(\`]|^\s*it\.each\s*[\(\`]|^\s*test\s*[\(\`]|^\s*test\.each\s*[\(\`]"),
    "javascript": re.compile(r"^\s*it\s*[\(\`]|^\s*it\.each\s*[\(\`]|^\s*test\s*[\(\`]|^\s*test\.each\s*[\(\`]"),
    "java":       re.compile(r"^\s*@Test\b|^\s*@ParameterizedTest\b"),
    "kotlin":     re.compile(r"^\s*@Test\b|^\s*@ParameterizedTest\b|^\s*fun\s+`"),
    "python":     re.compile(r"^\s*def\s+test_"),
    "csharp":     re.compile(r"^\s*\[Fact\]|\s*\[Theory\]|\s*\[Test\]|\s*\[TestMethod\]"),
    "go":         re.compile(r"^\s*func\s+Test[A-Z]"),
}

# Suffixes that identify a test file for each language.
_TEST_FILE_SUFFIXES = {
    "typescript": (".spec.ts", ".test.ts"),
    "javascript": (".spec.js", ".test.js", ".spec.mjs", ".test.mjs"),
    "java":       ("Test.java", "Tests.java", "IT.java"),
    "kotlin":     ("Test.kt", "Tests.kt"),
    "python":     (),   # matched by prefix below
    "csharp":     ("Tests.cs", "Test.cs", "Spec.cs"),
    "go":         ("_test.go",),
}
_TEST_FILE_PREFIX = {"python": ("test_",)}

# Directories that are never test-file sources.
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".nyc_output", "coverage"}


def _is_test_file(path: Path, lang: str) -> bool:
    name = path.name
    for suffix in _TEST_FILE_SUFFIXES.get(lang, ()):
        if name.endswith(suffix):
            return True
    for prefix in _TEST_FILE_PREFIX.get(lang, ()):
        if name.startswith(prefix):
            return True
    return False


def _count_tests_in_file(test_file: Path, lang: str) -> int:
    """Test-case declarations in one file; 0 when it is unreadable."""
    pat = _TEST_LINE_PATTERNS.get(lang)
    if pat is None:
        return 0
    try:
        return sum(1 for line in test_file.read_text(encoding="utf-8", errors="replace").splitlines()
                   if pat.match(line))
    except (OSError, PermissionError):
        return 0


def _count_tests_for_languages(languages: list) -> tuple:
    """Count test-case declarations across all test files for the given languages.

    Returns (total_count, sorted list of test-file paths relative to PROJECT_ROOT).
    Silently skips unreadable files; never raises.
    """
    root = Path(PROJECT_ROOT)
    total = 0
    found = []

    for lang in languages:
        if _TEST_LINE_PATTERNS.get(lang) is None:
            continue
        for test_file in root.rglob("*"):
            if not test_file.is_file():
                continue
            if any(part in _SKIP_DIRS for part in test_file.parts):
                continue
            if not _is_test_file(test_file, lang):
                continue
            count = _count_tests_in_file(test_file, lang)
            if count > 0:
                total += count
                try:
                    found.append(str(test_file.relative_to(root)))
                except ValueError:
                    found.append(str(test_file))

    return total, sorted(found)


_EXT_LANG = {".ts": "typescript", ".tsx": "typescript",
             ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
             ".java": "java", ".kt": "kotlin",
             ".py": "python", ".cs": "csharp", ".go": "go"}


def _flag_values(passthrough: list, flag: str) -> list:
    """Values of `flag` in a passthrough argv (`--flag v` or `--flag=v`)."""
    out = []
    for i, tok in enumerate(passthrough):
        if tok == flag and i + 1 < len(passthrough):
            out.append(passthrough[i + 1])
        elif tok.startswith(flag + "="):
            out.append(tok.split("=", 1)[1])
    return out


def _scope_files(passthrough: list) -> list:
    """The source files this run will mutate: the --file flags, else what --base selects.
    Empty when that cannot be told before the run starts."""
    files = _flag_values(passthrough, "--file")
    if files:
        return files
    base = (_flag_values(passthrough, "--base") or [None])[-1]
    if base:
        try:
            return changed_files_scanner.scan(PROJECT_ROOT, base=base)["files"]
        except Exception:  # an estimate must never stop the run
            return []
    return []


def _count_tests_in_scope(files: list, passthrough: list) -> tuple:
    """(test-case count, test files) of the tests that run for `files`: each file's own test and,
    for Java/Kotlin, the test classes that name it (the selection PIT gets). (0, []) when no test
    is found for any file, so the caller falls back to the repo-wide count."""
    root = Path(PROJECT_ROOT)
    tests = {}  # project-relative path -> language of its test-case pattern
    exclude = java_tests.split_globs(_flag_values(passthrough, "--exclude-tests"),
                                     java_tests.excluded_from_env())
    for f in files:
        rel = os.path.relpath(f, root) if os.path.isabs(f) else f
        prof = lang_profiles.profile_for_file(rel)
        if prof is None:
            continue
        own = prof.test_file(PROJECT_ROOT, rel)
        own = os.path.relpath(own, root) if own and os.path.exists(own) else None
        if prof.language == "java":
            module = lang_profiles.build_module_for(PROJECT_ROOT, [rel])
            target = prof.target_id(PROJECT_ROOT, rel)
            if target:
                sel = java_tests.select_tests(
                    PROJECT_ROOT, module.strip(":").replace(":", "/"), [target],
                    own_tests=[own] if own else [], exclude=exclude,
                    full_suite="--full-suite" in passthrough)
                for tf in sel["files"]:
                    tests[tf] = _EXT_LANG.get(Path(tf).suffix, "java")
        if own:
            tests[own] = _EXT_LANG.get(Path(own).suffix, prof.language)
    counted = {tf: _count_tests_in_file(root / tf, lang) for tf, lang in tests.items()}
    found = sorted(tf for tf, n in counted.items() if n > 0)
    return sum(counted.values()), found


def _compute_timeout(test_count: int) -> int:
    """Formula: 1 minute per test case, minimum 10 minutes, maximum 2 hours.

    Rationale: Stryker runs each mutant against the narrowed test subset.
    With per-test coverage analysis the per-mutant overhead scales roughly
    linearly with the test count.  1 min/test is deliberately conservative
    so a slow CI machine never times out a legitimately-running engine.
    """
    return max(600, min(test_count * 60, 7200))


# --- presentation --------------------------------------------------------------

# A Windows console defaults to cp1252, which cannot encode the box-drawing and
# check glyphs below. Without this the FIRST relayed stage line raises
# UnicodeEncodeError and takes the whole wizard down mid-run -- the mutation
# analysis has already started in the child process at that point. Try to switch
# stdout to UTF-8; if that is not possible, fall back to ASCII glyphs rather
# than crash.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):  # pragma: no cover - old/odd streams
    pass


def _supports(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK = _supports("▸ · ✓ ⚠")
ARROW = "▸" if _UNICODE_OK else ">"
MIDDOT = "·" if _UNICODE_OK else "-"
TICK = "✓" if _UNICODE_OK else "OK"
WARNSIGN = "⚠" if _UNICODE_OK else "!"

_TTY = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _TTY else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")
BLUE = _c("\033[34m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
RED = _c("\033[31m")

_STAGE_COUNT = 0
_ACTIVE_SPAN = None  # set by main(); read by stage() to emit run.step events


def stage(name: str) -> None:
    global _STAGE_COUNT
    _STAGE_COUNT += 1
    print(f"{BOLD}{BLUE}{ARROW} Stage {_STAGE_COUNT} {MIDDOT} {name}{RESET}")
    observability.try_step(_ACTIVE_SPAN, name=name, index=_STAGE_COUNT)


def ok(msg: str) -> None:
    print(f"  {GREEN}{TICK}{RESET} {msg}")


def note(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}{WARNSIGN} {msg}{RESET}")


def confirm(question: str) -> bool:
    reply = input(f"  {YELLOW}? {question} [y/N] {RESET}")
    return reply.strip().lower().startswith("y")


# --- relaying run.py's output ---------------------------------------------------

# Maps a line prefix from run.py's own stdout to a human stage label. Only
# these lines (plus RESULT_PATTERNS below) are relayed to the caller; every
# other line -- Maven/PIT noise -- goes to the log file only.
STEP_LABELS = [
    (re.compile(r"^\[Step 1\]"), "Detecting languages and engines"),
    (re.compile(r"^\[Step 2\]"), "Resolving target files"),
    (re.compile(r"^\[Step 3\]"), "Filtering arid lines"),
    (re.compile(r"^\[Step 4b\]"), "Re-running engines to verify kills"),
    (re.compile(r"^\[Step 4\]"), "Running mutation analysis"),
    (re.compile(r"^\[Step 5"), "Writing hand-off prompt for the LLM"),
    (re.compile(r"^\[Step 6\]"), "Generating report"),
]

# `[Step 4]` now names the engine and language it is about to run, e.g.
# "Running stryker for typescript (3 file(s))...". In a polyglot run there is
# one such line per language, so the stage label is taken from the line itself
# rather than a fixed "Running PIT" string that would be wrong for every
# language except Java.
_STEP4_DETAIL = re.compile(r"^\[Step 4b?\]\s+(Running|Re-running)\s+(.*?)\.\.\.\s*$")

RESULT_PATTERNS = re.compile(
    r"Mutation score|GATE:|Threshold met|Threshold NOT met|MANUAL REVIEW|"
    r"NOTHING_TO_MUTATE|Nothing to mutate|Nothing to score|"
    r"gate\.json|prompt\.txt|report\.txt|test strength|"
    r"SURVIVED|NO_COVERAGE|Per language|^\s+\w+\s+\d+(\.\d+)?%|"
    r"is not installed|run failed|scope:|Remediation not enabled|"
    # [Ss]onar|SONAR: Step 7 talks about "sonar-project.properties" and
    # "sonar-scanner" in lowercase, "Sonar target" capitalised, and the failure
    # banner and the SONAR_TOKEN instruction in capitals.
    r"outside the diff|held back|new-file|[Ss]onar|SONAR"
)


def _relay_line(line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    detail = _STEP4_DETAIL.match(stripped)
    if detail:
        stage(detail.group(2)[0].upper() + detail.group(2)[1:])
        return
    for pattern, label in STEP_LABELS:
        if pattern.match(stripped):
            stage(label)
            return
    if RESULT_PATTERNS.search(stripped):
        note(stripped)


def stream_run(cmd: list, log_path: Path) -> int:
    import atexit
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        # The child must agree with this process about the package root, not re-derive it.
        env=dict(os.environ, MUTATION_PROJECT_ROOT=str(PROJECT_ROOT)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def _kill_tree():
        if proc.poll() is None:
            proc.kill()

    atexit.register(_kill_tree)

    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        for line in proc.stdout:
            logf.write(line)
            # Flush per line. A mutation run can take half an hour; if it is
            # interrupted (CI timeout, Ctrl-C, killed process tree) a buffered
            # log leaves an EMPTY file, which is the one moment the transcript
            # is most needed.
            logf.flush()
            _relay_line(line)
    proc.wait()
    return proc.returncode


# --- result capture --------------------------------------------------------------

def _load_gate(run_id: str) -> dict:
    gate_path = Path(PROJECT_ROOT) / "mutation-reports" / run_id / "gate.json"
    if not gate_path.exists():
        return {}
    return json.loads(gate_path.read_text(encoding="utf-8"))


def _print_summary(run_id: str, gate: dict, returncode: int, log_path: Path) -> dict:
    # run.py now writes gate.json on EVERY exit path, including
    # nothing-to-mutate and error. An empty gate here therefore means the child
    # died before it could write one -- say so, rather than reporting the bare
    # "UNKNOWN" that is indistinguishable from a crash.
    verdict = gate.get("verdict") or ("CRASHED" if returncode not in (0,) else "UNKNOWN")
    score = gate.get("score")
    threshold = gate.get("threshold")
    llm_required = gate.get("llmRequired", verdict == "FAIL")
    survived = gate.get("survived", gate.get("survivorsRemaining"))
    no_coverage = gate.get("noCoverage")
    languages = gate.get("languages") or []
    per_language = gate.get("perLanguage") or {}

    color = {
        "PASS": GREEN, "MANUAL_REVIEW": YELLOW,
        "NOTHING_TO_MUTATE": DIM,
    }.get(verdict, RED)
    print()
    print(f"{BOLD}{color}{TICK} Mutation run complete{RESET}")
    note(f"run id      : {run_id}")
    note(f"verdict     : {verdict}")
    if gate.get("reason"):
        note(f"reason      : {gate['reason']}")
    if gate.get("error"):
        note(f"error       : {gate['error']}")
    if languages:
        note(f"languages   : {', '.join(languages)}  "
             f"(engines: {', '.join(gate.get('tools') or [])})")
    if score is not None and threshold is not None:
        note(f"score       : {score}% (threshold {threshold}%)")
    # A merged score can hide one weak language, so show the split whenever the
    # run actually spanned more than one.
    for lang, s in sorted(per_language.items()):
        if len(per_language) > 1:
            note(f"  {lang:<11} {s.get('mutation_score', 0)}% "
                 f"({s.get('killed', 0)}/{s.get('total', 0) - s.get('non_viable', 0)})")
    if isinstance(survived, list):
        note(f"survivors   : {len(survived)}")
    elif survived is not None:
        note(f"survived    : {survived}    no-coverage : {no_coverage}")
    upload = gate.get("sonarUpload") or {}
    if upload.get("ok"):
        note(f"sonar       : uploaded (quality gate {upload.get('qualityGate', '?')})")
    elif upload.get("attempted"):
        note(f"sonar       : UPLOAD FAILED [{upload.get('stage', '?')}] - run incomplete")
    note(f"log         : {log_path}")
    print()

    art = Path(PROJECT_ROOT) / "mutation-reports" / run_id
    telemetry = _read_json(art / "telemetry.json")
    orch = telemetry.get("orchestration") or {}
    rounds = orch.get("rounds") or {}

    def _compact(mutants):
        return [{"language": s.get("language"), "file": s.get("file") or s.get("source_file"),
                 "line": s.get("line"), "operator": s.get("operator")} for s in mutants]

    result = {
        "runId": run_id,
        "verdict": verdict,
        "phase": gate.get("phase", "round1"),
        "score": score,
        "scoreBefore": gate.get("scoreBefore"),
        "threshold": threshold,
        "llmRequired": llm_required,
        "languages": languages,
        "tools": gate.get("tools") or [],
        "perLanguage": {k: v.get("mutation_score") for k, v in per_language.items()},
        # Exact counts from the gate, so a caller never has to estimate them.
        "total": gate.get("total"),
        "killed": gate.get("killed"),
        "survived": len(survived) if isinstance(survived, list) else survived,
        "noCoverage": no_coverage,
        "errors": gate.get("errors", sum((v.get("errors") or 0) for v in per_language.values())
                           if per_language else None),
        "nonViable": gate.get("nonViable"),
        "suppressed": gate.get("suppressed"),
        "outOfDiffScope": gate.get("outOfDiffScope"),
        "reason": gate.get("reason"),
        "error": gate.get("error"),
        "exitCode": returncode,
        "sonarUpload": {"ok": upload.get("ok"), "stage": upload.get("stage"),
                        "qualityGate": upload.get("qualityGate")} if upload else None,
        "incomplete": bool(gate.get("incomplete")),
        "artifactsDir": str(art),
        "logPath": str(log_path),
        # Everything the caller needs for its final message, so it never has to open the pipeline files.
        "stepSeconds": {k: round((v or {}).get("wallClockMs", 0) / 1000, 2)
                        for k, v in (telemetry.get("steps") or {}).items()},
        "measuredTokens": orch.get("totalTokens") if "messages" in orch else None,
        "measuredTokensByRound": ({k: (v or {}).get("totalTokens") for k, v in rounds.items()}
                                  if rounds else None),
        "promptPath": str(art / "prompt.txt") if (art / "prompt.txt").exists() else None,
        "newFileNeeded": _compact(gate.get("newFileNeeded") or []),
    }
    # Round 2 only. Round 1's survivor detail is left out: it belongs to mutation-test-writer via prompt.txt.
    if result["phase"] == "verify" and isinstance(gate.get("survivorsRemaining"), list):
        result["survivorsRemaining"] = _compact(gate["survivorsRemaining"])
    _emit_result(result)
    return result


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _emit_result(result: dict) -> None:
    """Print the single-line MUTATION_RESULT JSON; emitted on every exit path so a waiting caller always gets one."""
    print(f"MUTATION_RESULT: {json.dumps(result)}", flush=True)


# --- one run at a time per project ---
# A caller re-launching a run it thought had not started would otherwise start a second full engine run.

_LOCK_MAX_AGE_S = 3 * 3600  # above the 2-hour engine-timeout ceiling


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows would TERMINATE the process, so ask instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) \
                and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(run_id: str):
    """Return (lock_path, None) once held, or (lock_path, holder) if a live wizard holds it; stale locks are taken over."""
    lock = Path(PROJECT_ROOT) / "mutation-reports" / ".wizard.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_json(lock)
            if (_pid_alive(int(holder.get("pid") or 0))
                    and time.time() - float(holder.get("startedAt") or 0) < _LOCK_MAX_AGE_S):
                return lock, holder
            try:
                lock.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "startedAt": time.time(), "runId": run_id}, fh)
        return lock, None
    return lock, None


def _release_lock(lock: Path) -> None:
    if _read_json(lock).get("pid") == os.getpid():
        try:
            lock.unlink()
        except OSError:
            pass


# --- passthrough arg handling ----------------------------------------------------

def _mark_latest(run_id: str) -> None:
    """Point final_report.py --latest at THIS run, even if it exits before run.py
    writes a gate.json (nothing in scope, declined) -- otherwise --latest would
    silently report a stale earlier run."""
    reports = Path(PROJECT_ROOT) / "mutation-reports"
    (reports / run_id).mkdir(parents=True, exist_ok=True)
    (reports / "LATEST").write_text(run_id, encoding="utf-8")


def _write_stub_gate(run_id: str, verdict: str, reason: str) -> None:
    """Minimal gate.json for the wizard's own early exits (run.py never ran)."""
    gate_path = Path(PROJECT_ROOT) / "mutation-reports" / run_id / "gate.json"
    gate_path.write_text(json.dumps({
        "runId": run_id, "verdict": verdict, "score": None, "threshold": None,
        "llmRequired": False, "reason": reason,
    }, indent=2), encoding="utf-8")


def _normalise_file_args(passthrough: list) -> list:
    """Rewrite every `--file X` to a path relative to PROJECT_ROOT when X (spelled relative to
    the caller's working directory or absolute) names an existing file under it. The child
    process runs from PROJECT_ROOT, which may be a package below the directory the user is in."""
    root = Path(PROJECT_ROOT).resolve()

    def fix(value: str) -> str:
        p = Path(value)
        if not p.is_absolute() and (root / p).exists():
            return p.as_posix()
        cand = p if p.is_absolute() else Path.cwd() / p
        if cand.exists():
            try:
                return cand.resolve().relative_to(root).as_posix()
            except ValueError:
                pass
        return value

    out, i = [], 0
    while i < len(passthrough):
        tok = passthrough[i]
        if tok == "--file" and i + 1 < len(passthrough):
            out += [tok, fix(passthrough[i + 1])]
            i += 2
            continue
        if tok.startswith("--file="):
            out.append("--file=" + fix(tok.split("=", 1)[1]))
        else:
            out.append(tok)
        i += 1
    return out


def _extract_run_id(passthrough: list) -> str:
    for i, tok in enumerate(passthrough):
        if tok == "--run-id" and i + 1 < len(passthrough):
            return passthrough[i + 1]
        if tok.startswith("--run-id="):
            return tok.split("=", 1)[1]
    return ""


def _is_verify(passthrough: list) -> bool:
    return "--verify" in passthrough


def _has_explicit_scope(passthrough: list) -> bool:
    """True iff the caller told us which PR/branch/file to scope to. When this
    is False, there is no PR/link context at all -- resolve_scope() below
    decides what to mutate (local changes, else the last commit) instead of
    leaving the choice to run.py."""
    return any(f in passthrough for f in ("--base", "--file", "--pr", "--commit"))


def _flag_value(passthrough: list, flag: str):
    for i, tok in enumerate(passthrough):
        if tok == flag and i + 1 < len(passthrough):
            return passthrough[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def check_pr_commit_scope(passthrough: list):
    """Preflight for --pr / --commit: (None, []) when there is none or the checkout is the PR/commit's
    code, else (the preflight result, []). Second element: files in scope, for the timeout estimate."""
    for flag, kind in (("--pr", "pr"), ("--commit", "commit")):
        value = _flag_value(passthrough, flag)
        if value:
            pre = scope_resolver.preflight(PROJECT_ROOT, kind, value)
            if pre["status"] != "ok":
                return pre, []
            return None, pre["files"]
    return None, []


def resolve_scope(interactive: bool):
    """No PR/base/file given: scope to local uncommitted changes, falling back
    to the last commit. Returns (files, scope_label) or (None, None) if the
    human declined via --interactive. Prints what it found either way -- this
    is the 'validate with user cli' requirement, but only actually BLOCKS when
    --interactive means a human is present to answer; an unattended agent run
    just gets the informational line."""
    resolved = changed_files_scanner.get_local_or_last_commit_files(PROJECT_ROOT)
    files, scope, detail = resolved["files"], resolved["scope"], resolved["detail"]

    label = {"local": "uncommitted local changes", "last-commit": "last commit",
             "initial-commit": "initial commit"}.get(scope, scope)
    note(f"no PR/base given -- scoping to {label} ({detail})")
    if files:
        # Grouped by language so a polyglot scope is obvious before anything runs.
        for lang, group in (resolved.get("byLanguage") or {"": files}).items():
            note(f"  [{lang or 'source'}]")
            for f in group:
                note(f"    {f}")
    else:
        note("  (no source files matched after filtering)")
        for skipped in (resolved.get("skipped") or [])[:10]:
            note(f"    skipped {skipped['file']} -- {skipped['reason']}")

    if interactive and files:
        if not confirm(f"Proceed to mutate these {len(files)} file(s)?"):
            return None, scope
    return files, scope


def _git_value(*args) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# --- observability: status/pass-fail mapping -------------------------------------

def _pass_fail_status(verdict: str) -> str:
    return {
        "PASS": "pass", "FAIL": "fail", "MANUAL_REVIEW": "partial",
    }.get(verdict, "not_applicable")


def _measure_orchestration_tokens(since_iso: str) -> dict:
    """Best-effort: real usage recovered from the session transcript, same
    mechanism token_report.py already uses standalone. Never raises -- honesty
    label 'measured, when available'; returns None rather than a guess."""
    try:
        transcripts = token_report.find_transcripts(PROJECT_ROOT)
        if not transcripts:
            return None
        since = token_report._parse_ts(since_iso)
        totals = token_report.collect(transcripts[0], since, None)
        return totals if totals["messages"] else None
    except Exception:  # pragma: no cover - defensive only
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wizard-style wrapper around run.py -- clean stage output, "
                     "one JSON result line, full transcript kept in a log file."
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Pause for human confirmation before an LLM test-writing round "
             "would be needed (round-1 fail only; no-op with --verify), and "
             "again before approving creation of any brand-new test file.",
    )
    parser.add_argument(
        "--tests-added", type=int, default=None,
        help="Self-reported: number of @Test methods the agent added this round "
             "(only the agent can know this -- recorded verbatim, not derived).",
    )
    parser.add_argument(
        "--test-file", action="append", dest="test_files", default=None,
        help="Self-reported: path to a test file the agent modified this round. Repeatable.",
    )
    parser.add_argument(
        "--last-commit", action="store_true",
        help="Force scope resolution (local uncommitted changes, else the last "
             "commit) even if --base/--file were also given. Normally automatic "
             "whenever neither is present -- this is an explicit opt-in.",
    )
    args, passthrough = parser.parse_known_args()

    passthrough = _normalise_file_args(passthrough)
    run_id = _extract_run_id(passthrough) or f"run-{int(time.time())}"
    if not _extract_run_id(passthrough):
        passthrough += ["--run-id", run_id]
    verify_round = _is_verify(passthrough)

    if verify_round:
        prior = _load_gate(run_id)
        if prior.get("phase") == "verify":
            _mark_latest(run_id)
            # run.py refuses this too; checking here gives a clear result line under exit 5.
            warn(f"Round 2 was already run for {run_id} (verdict {prior.get('verdict')}, "
                 f"score {prior.get('score')}%). There is no round 3 -- start a new run.")
            _emit_result({
                "runId": run_id, "verdict": "ALREADY_VERIFIED", "exitCode": 5,
                "priorVerdict": prior.get("verdict"), "priorScore": prior.get("score"),
                "reason": "round 2 already used for this run; there is no round 3 -- "
                          "report the earlier verdict and stop, or start a NEW run",
            })
            return 5

    lock, holder = _acquire_lock(run_id)
    if holder:
        warn(f"Another mutation run is already in progress in this project "
             f"(run {holder.get('runId')}, pid {holder.get('pid')}). Not starting a second one.")
        _emit_result({
            "runId": None, "verdict": "ALREADY_RUNNING", "exitCode": 4,
            "runningRunId": holder.get("runId"),
            "runningForS": round(time.time() - float(holder.get("startedAt") or 0)),
            "reason": "a run is already in progress -- wait for ITS result; do not relaunch",
        })
        return 4
    import atexit
    atexit.register(_release_lock, lock)
    # Only now, so a blocked duplicate never repoints LATEST away from the run in progress.
    _mark_latest(run_id)

    had_explicit_scope = _has_explicit_scope(passthrough)
    source_used = "pr_diff" if had_explicit_scope else "manual"

    global _ACTIVE_SPAN
    _ACTIVE_SPAN = observability.try_start_run(
        agent="unified-mutation-testing",
        source=source_used,
        task_id=_git_value("rev-parse", "--abbrev-ref", "HEAD"),
        enduser_id=_git_value("config", "user.email"),
    )

    scope_files = []
    if not verify_round:
        mismatch, scope_files = check_pr_commit_scope(passthrough)
        if mismatch:
            text = scope_resolver.describe(mismatch)
            warn("Stopped before running -- " + text.splitlines()[0])
            for line in text.splitlines()[1:]:
                note(line.strip())
            _write_stub_gate(run_id, "SCOPE_MISMATCH", text.splitlines()[0])
            _gate_path = Path(PROJECT_ROOT) / "mutation-reports" / run_id / "gate.json"
            _gate = _read_json(_gate_path)
            _gate.update(scopeMessage=text, scopePreflight=mismatch)
            _gate_path.write_text(json.dumps(_gate, indent=2), encoding="utf-8")
            observability.try_end_run(_ACTIVE_SPAN, status="ok", pass_fail_status="not_applicable",
                                       kpi={"scope": mismatch["kind"], "scopeMismatch": mismatch["status"]})
            _emit_result({"runId": None, "verdict": "SCOPE_MISMATCH", "exitCode": 7,
                          "status": mismatch["status"], "problems": mismatch["problems"],
                          "recommended": mismatch["recommended"], "options": mismatch["options"],
                          "reason": "the checkout is not the code that was asked for -- show the options "
                                    "to the user and let them choose; do not pick one yourself"})
            return 7

    if not verify_round and (args.last_commit or not had_explicit_scope):
        files, scope = resolve_scope(args.interactive)
        if files is None:
            warn("Stopped before running -- declined at the scope-confirmation prompt.")
            _write_stub_gate(run_id, "DECLINED", "declined at the scope-confirmation prompt")
            observability.try_end_run(_ACTIVE_SPAN, status="ok",
                                       pass_fail_status="not_applicable",
                                       kpi={"scope": scope, "declined": True})
            _emit_result({"runId": None, "verdict": "DECLINED", "exitCode": 130,
                          "reason": "declined at the scope-confirmation prompt"})
            return 130
        if not files:
            note("Nothing to mutate -- no source files in scope.")
            _write_stub_gate(run_id, "NOTHING_TO_MUTATE", "no source files in scope")
            observability.try_end_run(_ACTIVE_SPAN, status="ok",
                                       pass_fail_status="not_applicable",
                                       kpi={"scope": scope, "filesInScope": 0})
            _emit_result({"runId": None, "verdict": "NOTHING_TO_MUTATE", "exitCode": 0,
                          "scope": scope, "reason": "no source files in scope"})
            return 0
        for f in files:
            passthrough += ["--file", f]

    # --- dynamic timeout ---------------------------------------------------------
    # Estimate the engine timeout from the test-case count so the agent knows
    # how long to wait and run.py never kills a legitimately-running Stryker.
    #
    # Formula: 1 minute per test case, min 10 min, max 2 hours.
    # Only applies when the caller has NOT already passed --timeout explicitly.
    _caller_set_timeout = any(
        tok == "--timeout" or tok.startswith("--timeout=")
        for tok in passthrough
    )
    if not _caller_set_timeout:
        # Count only the tests this run executes: the ones for the files in scope (--file, or
        # what --base selects). When that cannot be told, count every test of the languages in
        # scope, which can only over-estimate.
        _scope = _scope_files(passthrough) + list(scope_files)  # + the --pr/--commit diff's files
        _test_count, _test_files_found = (
            (0, []) if "--full-suite" in passthrough else _count_tests_in_scope(_scope, passthrough))
        if not _test_count:
            _langs_in_scope = {_EXT_LANG[Path(_f).suffix.lower()] for _f in _scope
                               if Path(_f).suffix.lower() in _EXT_LANG}
            if not _langs_in_scope:
                _langs_in_scope = {"typescript"}  # safe default for this repo
            _test_count, _test_files_found = _count_tests_for_languages(list(_langs_in_scope))

        _timeout_s = _compute_timeout(_test_count)
        passthrough += ["--timeout", str(_timeout_s)]

        # Print the estimate BEFORE launching run.py so an agent reading the log
        # knows the expected duration and does not start a duplicate run.
        print(f"ESTIMATED_TIMEOUT_S: {_timeout_s}")
        note(
            f"timeout estimate: {_test_count} test case(s) × 60 s = "
            f"{_timeout_s} s ({_timeout_s // 60} min)  "
            f"[{', '.join(_test_files_found) or 'no test files found'}]"
        )

    # --tests-added is consumed by this parser, so forward it explicitly or run.py reports 0 tests added.
    if args.tests_added is not None:
        passthrough += ["--tests-added", str(args.tests_added)]

    # `-u` is not cosmetic. Python block-buffers stdout when it is a pipe rather
    # than a TTY, so without it run.py's stage lines sit in its own 8KB buffer
    # until the process exits -- meaning a 30-minute mutation run relays NOTHING
    # while it works, and the live progress this wrapper exists to provide only
    # appears once there is nothing left to wait for.
    cmd = [sys.executable, "-u", RUN_PY] + passthrough
    log_path = Path(PROJECT_ROOT) / "mutation-reports" / run_id / "mutation-wizard.log"

    banner = "Verifying mutation kills" if verify_round else "Running mutation analysis"
    print(f"\n{BOLD}{BLUE}{banner}{RESET}  {DIM}(run {run_id}){RESET}\n")

    start_iso = datetime.now(timezone.utc).isoformat()

    returncode = stream_run(cmd, log_path)
    gate = _load_gate(run_id)
    result = _print_summary(run_id, gate, returncode, log_path)

    if returncode == 1:
        observability.try_error(_ACTIVE_SPAN, err_type="ScriptError",
                                 message=f"run.py exited 1 for run {run_id}")

    totals = _measure_orchestration_tokens(start_iso)
    kpi = {}
    if result["score"] is not None:
        kpi["mutationScore"] = result["score"]
    if result["threshold"] is not None:
        kpi["threshold"] = result["threshold"]
    if args.tests_added is not None:
        kpi["testsGenerated"] = args.tests_added
    kpi["round"] = 2 if verify_round else 1

    observability.try_end_run(
        _ACTIVE_SPAN,
        status="error" if returncode == 1 else "ok",
        pass_fail_status=_pass_fail_status(result["verdict"]),
        tokens_in=totals["totalInputTokens"] if totals else None,
        tokens_out=totals["outputTokens"] if totals else None,
        system="anthropic" if totals else None,
        kpi=kpi or None,
        artifact_ref=", ".join(args.test_files) if args.test_files else None,
    )

    if args.interactive and result["llmRequired"] and not verify_round:
        if not confirm("Threshold not met -- proceed to LLM test-writing?"):
            warn("Stopped before LLM hand-off at the caller's request.")
            note(f"Re-run manually when ready: python mutation_wizard.py --run-id {run_id} ...")
            return returncode

        new_file_needed = gate.get("newFileNeeded") or []
        if new_file_needed:
            note(f"{len(new_file_needed)} mutant(s) need a brand-new test file that "
                 "doesn't exist yet:")
            for s in new_file_needed[:10]:
                note(f"    [{s.get('language', '?')}] {s.get('file') or s.get('source_file')}"
                     f":{s.get('line')}")
            if confirm(f"Allow creating {len(new_file_needed)} new test file(s)?"):
                rc = subprocess.run(
                    [sys.executable, "-u", RUN_PY, "--regen-prompt",
                     "--allow-new-files", "--run-id", run_id],
                    cwd=PROJECT_ROOT,
                ).returncode
                if rc == 0:
                    ok("prompt.txt updated -- new-file creation approved for this run.")
                else:
                    warn("Failed to regenerate prompt.txt with file creation approved; "
                         "the held-back mutants remain excluded.")
            else:
                note("Declined -- those mutants stay held back; prompt.txt only "
                     "covers survivors with an existing test file to append to.")

    return returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as _exc:  # pragma: no cover - unexpected crash guard
        import traceback
        traceback.print_exc()
        sys.exit(255)

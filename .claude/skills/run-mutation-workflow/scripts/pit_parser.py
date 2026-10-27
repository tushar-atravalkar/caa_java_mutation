# -*- coding: utf-8 -*-
"""
Step 4: Run PIT via Maven and parse its XML output.
No LLM involved -- pure subprocess + XML parsing.

Fixes over the previous revision:
  1. Invokes the pitest goal explicitly (`mvn test` never triggers PIT --
     the mutation-testing profile has no <executions> binding).
  2. Rejects stale mutations.xml via an explicit freshness check.
  3. Reports NO_COVERAGE mutants as actionable, not just SURVIVED.
  4. Searches the directory the pom actually writes to (pit-output/).
  5. Accepts targetClasses so Step 2's file list is honoured.
  6. Arid-line suppression is per source file, not a global line-number set.
  7. Reports mutation score AND test strength (they answer different questions).
  8. Streams Maven output, enforces a timeout, validates the XML.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


# --- PIT status taxonomy -----------------------------------------------------
# A mutant is "covered" only if a test actually executed the mutated line.
KILLED_STATUSES = {"KILLED"}
# Real, actionable test gaps -- these are what Step 5 works on.
ACTIONABLE_STATUSES = {"SURVIVED", "NO_COVERAGE"}
# Infrastructure problems, NOT test-quality signals. Never silently ignore.
ERROR_STATUSES = {"TIMED_OUT", "MEMORY_ERROR", "RUN_ERROR", "NON_VIABLE"}

DEFAULT_TIMEOUT_SEC = 1800

# In quiet mode only these lines are echoed. Everything else goes to maven.log.
# Maven + PIT emit ~200 lines of plugin listings per run; when an agent invokes
# this script that output lands in its context and is billed as input tokens.
QUIET_KEEP = (
    "BUILD SUCCESS", "BUILD FAILURE", "BUILD FAILED", "Completed in",
    "COMPILATION ERROR", "Compiling",
    "Line Coverage", "Test strength", "mutations Killed",
    "Created", "no mutations",
)

# Directories PIT may write mutations.xml to, in order of preference.
# The mutation-testing profile sets reportsDirectory to <repo-root>/pit-output.
SEARCH_SUBDIRS = ["pit-output", "{module}/target/pit-reports", "{module}/build/reports/pitest",
                  "mutation-reports"]


# --- running -----------------------------------------------------------------

def _maven_executable() -> str:
    """Resolve mvn without shell=True (on Windows this finds mvn.cmd)."""
    for candidate in ("mvn", "mvn.cmd", "mvn.bat"):
        exe = shutil.which(candidate)
        if exe:
            return exe
    raise RuntimeError(
        "Maven not found on PATH. Install Maven or add it to PATH before running Step 4."
    )


def run_pit(
    project_root: str,
    module: str = "impl",
    target_classes: list = None,
    reports_dir: str = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    quiet: bool = True,
    log_path: str = None,
    excluded_methods: list = None,
    excluded_tests: list = None,
) -> int:
    """
    Run PIT mutation coverage. Returns the Maven exit code.

    `excluded_tests` are test class globs PIT must not run (-DexcludedTestClasses).

    NOTE: the goal is invoked explicitly. `mvn test -P mutation-testing` does
    NOT run PIT -- the profile configures the plugin but never binds it to a
    lifecycle phase, so Maven compiles, runs Surefire and exits 0 having
    mutated nothing.

    Conversely, invoking the goal ALONE skips compilation entirely, so
    `test-compile` is prepended. Both halves are required.
    """
    mvn = _maven_executable()
    # test-compile MUST run first. Invoking a plugin goal directly does not
    # trigger the lifecycle phases before it, so without this PIT mutates the
    # PREVIOUS build's target/classes and target/test-classes -- newly written
    # tests would silently never execute and the score would not move.
    cmd = [
        mvn,
        "test-compile",
        "org.pitest:pitest-maven:mutationCoverage",
        "-P", "mutation-testing",
    ]
    # `-pl ""` is a Maven error, and a single-module project has no module to
    # select. Only pass -pl when there is actually a module name.
    if module:
        cmd += ["-pl", module]
    if target_classes:
        cmd.append("-DtargetClasses=" + ",".join(target_classes))
    if excluded_tests:
        cmd.append("-DexcludedTestClasses=" + ",".join(excluded_tests))
    if excluded_methods:
        # diff narrowing: skip methods the diff never touched (diff_narrowing.py)
        cmd.append("-DexcludedMethods=" + ",".join(excluded_methods))
    if reports_dir:
        cmd.append("-Dpit.reportsDirectory=" + str(reports_dir))

    return _run_build(cmd, project_root, timeout, quiet, log_path, "Maven")


# Applies and configures gradle-pitest-plugin on the target project; see the file's header.
GRADLE_INIT_SCRIPT = Path(__file__).with_name("pitest-init.gradle")


def run_pit_gradle(
    project_root: str,
    gradle: str,
    project_path: str = ":",
    target_classes: list = None,
    reports_dir: str = None,
    junit5: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    quiet: bool = True,
    log_path: str = None,
    test_globs: list = None,
    excluded_tests: list = None,
) -> int:
    """
    Run PIT on one Gradle project through GRADLE_INIT_SCRIPT. Returns Gradle's exit code.

    No change to the team's build is needed: the init script applies gradle-pitest-plugin to
    `project_path` (or reuses the build's own), and forces XML output into `reports_dir` with no
    timestamped subdirectory. The pitest task depends on testClasses, so new tests are compiled
    first. The wrapper (`gradle`) pins the Gradle version the build expects.

    `test_globs` are the test classes PIT runs (java_tests.select_tests), `excluded_tests` the test
    class globs it must not run. Without `test_globs` the plugin's own default applies.
    """
    task = ":pitest" if project_path in ("", ":") else f"{project_path}:pitest"
    # A wrapper committed from Windows often lacks the executable bit; sh runs it anyway.
    launcher = [gradle] if os.name == "nt" or os.access(gradle, os.X_OK) else ["sh", gradle]
    cmd = [*launcher, task, "--console=plain", "--init-script", str(GRADLE_INIT_SCRIPT),
           f"-PmutationProject={project_path or ':'}",
           f"-PmutationJunit5={'true' if junit5 else 'false'}"]
    if target_classes:
        cmd.append("-PmutationTargets=" + ",".join(target_classes))
    if test_globs:
        cmd.append("-PmutationTests=" + ",".join(test_globs))
    if excluded_tests:
        cmd.append("-PmutationExcludedTests=" + ",".join(excluded_tests))
    if reports_dir:
        cmd.append("-PmutationReportDir=" + str(Path(reports_dir).resolve()))
    for env_key, prop in (("MUTATION_PITEST_VERSION", "mutationPitestVersion"),
                          ("MUTATION_PITEST_JUNIT5_VERSION", "mutationJunit5PluginVersion")):
        if os.environ.get(env_key):
            cmd.append(f"-P{prop}={os.environ[env_key]}")
    return _run_build(cmd, project_root, timeout, quiet, log_path, "Gradle")


def _run_build(cmd: list, project_root: str, timeout: int, quiet: bool, log_path: str, tool: str) -> int:
    print(f"[PIT] {' '.join(cmd)}")
    try:
        if not quiet:
            # Stream everything -- for humans debugging a run directly.
            result = subprocess.run(cmd, cwd=project_root, timeout=timeout)
            return result.returncode

        result = subprocess.run(
            cmd, cwd=project_root, timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        print(f"[PIT] ERROR: mutation run exceeded {timeout}s and was killed.")
        return 124
    except (FileNotFoundError, PermissionError) as exc:
        print(f"[PIT] ERROR: could not execute {tool}: {exc}")
        return 127

    output = (result.stdout or "") + (result.stderr or "")

    # Full transcript always preserved on disk -- quiet must never lose diagnostics.
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(output, encoding="utf-8")

    if result.returncode == 0:
        for line in output.splitlines():
            if any(k in line for k in QUIET_KEEP):
                print(f"  {line.strip()}")
        if log_path:
            print(f"  (full {tool} output: {log_path})")
    else:
        # On failure show the tail so the error is never hidden.
        print("[PIT] run failed -- last 40 lines:")
        for line in output.splitlines()[-40:]:
            print(f"  {line}")

    return result.returncode


# --- locating the report -----------------------------------------------------

def find_mutations_xml(
    project_root: str,
    module: str = "impl",
    reports_dir: str = None,
    min_mtime: float = None,
) -> str:
    """
    Locate the mutations.xml produced by the run that just finished.

    `min_mtime` is the epoch timestamp the run started. Any XML older than that
    is a leftover from a previous run and is rejected rather than returned --
    the previous revision silently reported month-old results as current.
    """
    roots = []
    if reports_dir:
        roots.append(Path(reports_dir))
    base = Path(project_root)
    for sub in SEARCH_SUBDIRS:
        # An empty module (single-module build) must not leave a leading "/", which pathlib
        # would treat as the filesystem root.
        roots.append(base / sub.format(module=module).lstrip("/"))

    candidates = []
    for root in roots:
        if root.exists():
            candidates.extend(root.rglob("mutations.xml"))

    if not candidates:
        searched = "\n  ".join(str(r) for r in roots)
        raise FileNotFoundError(
            f"No mutations.xml found. Searched:\n  {searched}\n"
            "Did PIT actually run? `mvn test` alone does not invoke it."
        )

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    newest = candidates[0]

    if min_mtime is not None and newest.stat().st_mtime < min_mtime:
        age = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest.stat().st_mtime))
        raise RuntimeError(
            f"Only stale results found.\n"
            f"  Newest mutations.xml : {newest}\n"
            f"  Last modified        : {age}\n"
            f"  This run started     : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(min_mtime))}\n"
            "PIT produced no new report -- refusing to report old numbers as current."
        )

    return str(newest)


# --- parsing -----------------------------------------------------------------

def parse_mutations(xml_path: str) -> list:
    """Parse mutations.xml exactly once and return every mutation record."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        raise RuntimeError(f"Malformed PIT report at {xml_path}: {exc}") from exc

    root = tree.getroot()
    mutations = []
    for mutation in root.findall("mutation"):
        mutations.append({
            "status":      mutation.attrib.get("status", ""),
            "detected":    mutation.attrib.get("detected", "").lower() == "true",
            "line":        int(mutation.findtext("lineNumber") or 0),
            "operator":    _short_operator(mutation.findtext("mutator") or ""),
            "description": mutation.findtext("description") or "",
            "class":       mutation.findtext("mutatedClass") or "",
            "method":      mutation.findtext("mutatedMethod") or "",
            "source_file": mutation.findtext("sourceFile") or "",
            "index":       mutation.findtext("index") or "",
            # Per-test data (best effort): PIT reports how many tests ran for
            # the mutant and which one killed it.
            "tests_run":    (int(mutation.attrib["numberOfTestsRun"])
                             if (mutation.attrib.get("numberOfTestsRun") or "").isdigit() else None),
            "killing_test": mutation.findtext("killingTest") or "",
        })
    return mutations


def percent(part: int, whole: int) -> float:
    """part / whole in percent, one decimal, ties rounded UP. Python's round() sends a tie such
    as 56.25 to 56.2; Sonar shows 56.3, so the agent and the dashboard would disagree by 0.1.
    Small runs hit ties often (9 / 16 = 56.25)."""
    # Integer arithmetic: exact, no float tie surprises.
    return (2000 * part + whole) // (2 * whole) / 10 if whole else 0.0


def scored_total(summary: dict) -> int:
    """Mutants that count toward the score: everything generated except non-viable ones.
    This is the denominator of `mutation_score`, so `killed / scored_total` reproduces it."""
    return summary.get("total", 0) - summary.get("non_viable", 0)


def summarize(mutations: list) -> dict:
    """
    Two different questions, two different numbers:

      mutation_score = killed / (total - non_viable)
          Counts NO_COVERAGE as failure. This is the honest overall number.
          NON_VIABLE mutants (Stryker CompileError, PIT NOT_VIABLE) never ran
          a test, so they are excluded from the denominator, as Stryker and
          PIT do. Counting them as undetected makes the gate unpassable by
          any amount of test writing.
      test_strength  = killed / (killed + survived)
          Ignores unreachable mutants -- measures only how good the tests that
          DO run are. This is what PIT prints as "Test strength".

    Gating on mutation_score while feeding Step 5 only SURVIVED mutants makes
    the gate unfixable, so callers must be explicit about which one they use.
    """
    total       = len(mutations)
    killed      = sum(1 for m in mutations if m["status"] in KILLED_STATUSES)
    survived    = sum(1 for m in mutations if m["status"] == "SURVIVED")
    no_coverage = sum(1 for m in mutations if m["status"] == "NO_COVERAGE")
    non_viable  = sum(1 for m in mutations if m["status"] == "NON_VIABLE")
    # Engine errors proper (timeout, memory, run error). Non-viable mutants are reported
    # separately: they are a property of the mutant, not a failure of the engine.
    errors      = sum(1 for m in mutations if m["status"] in ERROR_STATUSES and m["status"] != "NON_VIABLE")
    measured    = total - non_viable
    covered     = killed + survived

    return {
        "total":          total,
        "killed":         killed,
        "survived":       survived,
        "no_coverage":    no_coverage,
        "errors":         errors,
        "non_viable":     non_viable,
        "mutation_score": percent(killed, measured),
        "test_strength":  percent(killed, covered),
        # retained so report.py keeps working
        "kill_ratio":     percent(killed, measured),
    }


def summarize_by(mutations: list, key: str = "language") -> dict:
    """Per-`key` summaries alongside the overall one: {value: summary}.

    A polyglot run merges several engines into one score, which is the right
    gate but hides where the weakness is -- 95% on 200 TypeScript mutants and
    20% on 10 Python ones averages to a passing number. The report shows both."""
    groups = {}
    for m in mutations:
        groups.setdefault(m.get(key) or "unknown", []).append(m)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def mutation_key(mutation: dict) -> str:
    """The identity used to look a mutant's file up in a per-file map.

    Prefers the engine's repo-relative path over the bare basename: two files
    both named `index.ts` in different folders share a basename, and keying on
    it cross-applies one file's arid lines to the other."""
    return mutation.get("file") or mutation.get("source_file") or ""


def _file_keys(mutation: dict) -> tuple:
    """Every key a per-file map might legitimately be keyed under."""
    return tuple(k for k in (mutation.get("file"), mutation.get("source_file")) if k)


def partition_suppressed(
    mutations: list,
    arid_map: dict = None,
    equivalent_map: dict = None,
    is_suppressed=None,
) -> tuple:
    """Split mutations into (counted, suppressed).

    Suppressed mutants are removed from the score's NUMERATOR AND DENOMINATOR,
    not just from the survivor list. Counting a mutant the pipeline then hides
    from the test-writer produces a gate nobody can pass: the score is dragged
    down by mutants that were deliberately never reported as work to do.

    `arid_map`        {file key -> set of line numbers} suppressed outright.
    `equivalent_map`  {file key -> {line -> [operator families]}} suppressed
                      only for mutants whose operator matches -- so a line
                      holding one equivalent mutant and one killable mutant
                      keeps the killable one visible.
    """
    arid_map = arid_map or {}
    equivalent_map = equivalent_map or {}
    counted, suppressed = [], []

    for mutation in mutations:
        arid, equivalents = set(), {}
        for key in _file_keys(mutation):
            arid |= set(arid_map.get(key) or ())
            equivalents.update(equivalent_map.get(key) or {})

        hit = False
        if is_suppressed is not None:
            hit = is_suppressed(mutation["line"], mutation.get("operator", ""),
                                arid, equivalents)
        else:  # no operator-aware callback available: arid lines only
            hit = mutation["line"] in arid

        (suppressed if hit else counted).append(mutation)

    return counted, suppressed


def parse_survivors(
    mutations: list,
    arid_map: dict = None,
    include_no_coverage: bool = True,
) -> list:
    """
    Return actionable mutants.

    `arid_map` maps a file key (repo-relative path, or the basename the engine
    reported) to the set of arid line numbers in that file. A flat set of line
    numbers with no file association means a comment on line 25 of one class
    suppresses a real mutant on line 25 of every other class.

    Callers that already ran `partition_suppressed` should pass `arid_map=None`
    -- suppression has happened, and applying it twice is harmless but pointless.
    """
    wanted = {"SURVIVED"}
    if include_no_coverage:
        wanted.add("NO_COVERAGE")

    survivors = []
    for mutation in mutations:
        if mutation["status"] not in wanted:
            continue

        if arid_map:
            arid_lines = set()
            for key in _file_keys(mutation):
                arid_lines |= set(arid_map.get(key) or ())
            if mutation["line"] in arid_lines:
                continue

        record = dict(mutation)
        record["category"] = (
            "no-coverage" if mutation["status"] == "NO_COVERAGE" else "survived"
        )
        survivors.append(record)

    # survived first (they need judgement), then no-coverage (mechanical)
    survivors.sort(key=lambda m: (m["category"] != "survived",
                                  mutation_key(m), m["line"]))
    return survivors


def parse_all(xml_path: str) -> dict:
    """Convenience wrapper: parse + summarize in one call."""
    return summarize(parse_mutations(xml_path))


def _short_operator(full_name: str) -> str:
    return full_name.split(".")[-1].replace("Mutator", "")


# --- CLI ---------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(description="Step 4: Run PIT and parse survivors")
    parser.add_argument("--project-root", default=str(Path(__file__).parent.parent))
    parser.add_argument("--module", default="impl", help="Maven module (default: impl)")
    parser.add_argument("--xml", help="Parse an existing mutations.xml instead of running PIT")
    parser.add_argument("--target-classes", help="Comma-separated FQCNs to mutate")
    parser.add_argument("--reports-dir", help="Override PIT reportsDirectory")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--no-coverage-excluded", action="store_true",
                        help="Report only SURVIVED, excluding NO_COVERAGE mutants")
    args = parser.parse_args()

    if args.xml:
        xml_path = args.xml
        print(f"[Step 4] Parsing existing report: {xml_path}")
    else:
        run_start = time.time()
        targets = args.target_classes.split(",") if args.target_classes else None
        print(f"[Step 4] Running PIT on module: {args.module}")
        rc = run_pit(args.project_root, args.module, targets, args.reports_dir, args.timeout)
        if rc != 0:
            print(f"ERROR: PIT run failed with exit code {rc}. See Maven output above.")
            return 1
        try:
            xml_path = find_mutations_xml(
                args.project_root, args.module, args.reports_dir, min_mtime=run_start
            )
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            return 1
        print(f"         report: {xml_path}")

    try:
        mutations = parse_mutations(xml_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    summary   = summarize(mutations)
    survivors = parse_survivors(mutations, include_no_coverage=not args.no_coverage_excluded)

    print("")
    print(f"Mutation score : {summary['mutation_score']}%  "
          f"({summary['killed']} killed / {summary['total']} total)")
    print(f"Test strength  : {summary['test_strength']}%  "
          f"(ignores {summary['no_coverage']} unreachable mutants)")
    print(f"  KILLED      : {summary['killed']}")
    print(f"  SURVIVED    : {summary['survived']}")
    print(f"  NO_COVERAGE : {summary['no_coverage']}")
    if summary["errors"]:
        print(f"  ERRORS      : {summary['errors']}  (timeout / memory / run error)")
    if summary.get("non_viable"):
        print(f"  NON_VIABLE  : {summary['non_viable']}  (did not compile; not scored)")

    if survivors:
        print(f"\nActionable mutants ({len(survivors)}):\n")
        for s in survivors:
            tag = "SURVIVED " if s["category"] == "survived" else "NO-COVER "
            print(f"  {tag} {s['source_file']}:{s['line']:<4} | "
                  f"{s['operator']:<28} | {s['description']}")
    else:
        print("\nNo actionable mutants -- every mutant was killed.")

    return 0


if __name__ == "__main__":
    sys.exit(_main())

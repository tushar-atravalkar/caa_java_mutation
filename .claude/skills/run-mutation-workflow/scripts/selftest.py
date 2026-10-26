# -*- coding: utf-8 -*-
"""
Self-test for the polyglot mutation pipeline. Pure stdlib, no engines required.

Why this exists: the only engine installed in any given repo is that repo's own
one. A TypeScript repo can never exercise the PIT, mutmut, gremlins or
Stryker.NET code paths by running them, so those paths used to ship unverified
-- which is how `NOT COVERED` (gremlins prints it with a space) silently became
RUN_ERROR for every Go mutant, and how a second `--mutate` flag silently
replaced the first for Stryker.

Each engine's parser is fed a report in that engine's real on-disk schema and
checked against the normalised record contract. Steps 1-3 and the gate are
exercised against a synthetic polyglot repo built in a temp directory.

    python selftest.py            # run everything
    python selftest.py -v         # show each assertion
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).parent
_SKILLS = _HERE.parent.parent  # skill assets to copy into sandboxes -- NOT the project root

try:  # package-relative imports -- resolve at any install location
    from . import mutation_runners as runners
    from . import pit_parser
    from . import validate_environment
    from .arid_filter import filter_file, is_suppressed, operator_family
    from .changed_files_scanner import (
        group_by_language, _filter_source_files, diff_line_ranges,
        resolve_line_ranges_for_files,
    )
    from .lang_profiles import (
        _is_non_source, _is_test_path, detect_languages, get_profile, profile_for_file,
    )
except ImportError:  # direct invocation: py .../selftest.py
    sys.path.insert(0, str(_HERE))
    import mutation_runners as runners  # noqa: E402
    import pit_parser  # noqa: E402
    import validate_environment  # noqa: E402
    from arid_filter import filter_file, is_suppressed, operator_family  # noqa: E402
    from changed_files_scanner import (  # noqa: E402
        group_by_language, _filter_source_files, diff_line_ranges,
        resolve_line_ranges_for_files,
    )
    from lang_profiles import (  # noqa: E402
        _is_non_source, _is_test_path, detect_languages, get_profile, profile_for_file,
    )

VERBOSE = "-v" in sys.argv
_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition), detail))
    if VERBOSE or not condition:
        mark = "ok  " if condition else "FAIL"
        print(f"  {mark} {name}" + (f"  -- {detail}" if detail and not condition else ""))


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


# --- 1. path classification ---------------------------------------------------

def test_path_classification():
    print("\n[1] path classification (test vs source vs non-source)")
    # Bare-substring filtering used to discard all four of these real source
    # files because their names merely contain the letters t-e-s-t / s-p-e-c.
    for path in ("src/app/latest-news.ts", "src/app/greatest.ts",
                 "src/app/protest-handler.ts", "src/app/specialist.service.ts",
                 "impl/src/main/java/a/Contest.java", "src/a/SPLIT.java"):
        check(f"source kept: {path}", not _is_test_path(path))
    for path in ("src/app/a.service.spec.ts", "src/app/a.test.ts",
                 "impl/src/main/java/a/FooTest.java",
                 "impl/src/main/java/a/FooTests.java",
                 "impl/src/main/java/a/FooIT.java",
                 "src/Foo/BarTests.cs", "pkg/util_test.go",
                 "app/test_calc.py", "app/calc_test.py",
                 "src/test/java/a/Helper.java", "e2e/flow.ts"):
        check(f"test detected: {path}", _is_test_path(path))
    for path in ("src/app/x.d.ts", "karma.conf.js", "src/app/x.module.ts",
                 "src/app/Foo.generated.cs", "conftest.py"):
        check(f"non-source detected: {path}", _is_non_source(path))

    kept, skipped = _filter_source_files([
        "src/app/latest-news.ts", "node_modules/pkg/a.js", ".claude/x.py",
        "src/app/gone.ts", "README.md",
    ], project_root=str(_HERE))  # nothing exists under _HERE, so all drop out
    reasons = {s["file"]: s["reason"] for s in skipped}
    check("node_modules excluded", "dependency" in reasons.get("node_modules/pkg/a.js", ""))
    check("tooling dir excluded", "tooling" in reasons.get(".claude/x.py", ""))
    check("markdown excluded", "extension" in reasons.get("README.md", ""))
    check("missing file excluded, not crashed",
          "disk" in reasons.get("src/app/latest-news.ts", ""))
    eq("nothing kept when nothing on disk", kept, [])


# --- 2. per-file language mapping (the polyglot pivot) ------------------------

def test_language_mapping():
    print("\n[2] per-file language mapping")
    expected = {
        "src/a.ts": "typescript", "src/a.tsx": "typescript",
        "src/a.js": "javascript", "src/a.mjs": "javascript",
        "impl/src/main/java/a/B.java": "java", "impl/src/main/kotlin/a/B.kt": "java",
        "app/calc.py": "python", "src/Foo/Bar.cs": "csharp", "pkg/util.go": "go",
    }
    for path, lang in expected.items():
        prof = profile_for_file(path)
        eq(f"{path} -> {lang}", prof.language if prof else None, lang)

    mixed = list(expected)
    grouped = group_by_language(mixed)
    eq("groups all five languages", sorted(grouped),
       ["csharp", "go", "java", "javascript", "python", "typescript"])
    check("every language has a distinct engine",
          len({get_profile(l).tool for l in grouped}) == 5,
          f"tools: {sorted({get_profile(l).tool for l in grouped})}")


# --- 3. arid filter -----------------------------------------------------------

_ARID_FIXTURES = {
    "t.ts": (
        "import { Injectable } from '@angular/core';\n"      # 1 arid
        "/*\n block\n comment\n*/\n"                          # 2-5 arid
        "export class A {\n"                                  # 6
        "  calc(n: number): number {\n"                       # 7
        "    console.log(n);\n"                                # 8 arid
        "    if (n <= 0) { return 0; }\n"                      # 9 productive
        "    return n * 2;\n"                                  # 10 productive
        "  }\n"                                                # 11 arid (brace)
        "}\n",                                                 # 12 arid (brace)
        {1, 2, 3, 4, 5, 8, 11, 12}, {9, 10},
    ),
    "t.py": (
        "import os\n"                                          # 1 arid
        "def calc(x):\n"                                       # 2
        '    """\n    doc body\n    """\n'                     # 3-5 arid
        "    logger.info(x)\n"                                 # 6 arid
        "    if x > 3:\n"                                      # 7 productive
        "        return x + 1\n"                               # 8 productive
        "    return x\n",                                      # 9 productive
        {1, 3, 4, 5, 6}, {7, 8, 9},
    ),
    "T.java": (
        "package a;\n"                                          # 1 arid
        "/* block\n comment */\n"                               # 2-3 arid
        "public class T {\n"                                    # 4
        "  int calc(int n) {\n"                                 # 5
        "    log.debug(\"x\");\n"                               # 6 arid
        "    if (n >= 2) { return n; }\n"                       # 7 productive
        "    return n - 1;\n"                                   # 8 productive
        "  }\n"                                                 # 9 arid
        "}\n",                                                  # 10 arid
        {1, 2, 3, 6, 9, 10}, {7, 8},
    ),
    "t.go": (
        "package main\n"                                        # 1 arid
        "import \"fmt\"\n"                                      # 2 arid
        "func Add(a, b int) int {\n"                            # 3
        "\tfmt.Println(a)\n"                                    # 4 arid
        "\ttime.Sleep(1)\n"                                     # 5 arid
        "\tif a > b {\n"                                        # 6 productive
        "\t\treturn a + b\n"                                    # 7 productive
        "\t}\n"                                                 # 8 arid
        "\treturn b\n"                                          # 9 productive
        "}\n",                                                  # 10 arid
        {1, 2, 4, 5, 8, 10}, {6, 7, 9},
    ),
    "T.cs": (
        "using System;\n"                                       # 1 arid
        "namespace A {\n"                                       # 2 arid
        "  [Serializable]\n"                                    # 3 arid
        "  public class T {\n"                                  # 4
        "    int Calc(int n) {\n"                               # 5
        "      _logger.LogInformation(\"x\");\n"                # 6 arid
        "      if (n > 1) { return n; }\n"                      # 7 productive
        "      return n * 3;\n"                                 # 8 productive
        "    }\n"                                               # 9 arid
        "  }\n"                                                 # 10 arid
        "}\n",                                                  # 11 arid
        {1, 2, 3, 6, 9, 10, 11}, {7, 8},
    ),
}


def test_arid(tmp):
    print("\n[3] arid filter, per language")
    for name, (body, must_be_arid, must_be_productive) in _ARID_FIXTURES.items():
        path = tmp / name
        path.write_text(body, encoding="utf-8")
        result = filter_file(str(path))
        arid = set(result["arid_lines"])
        missing = sorted(must_be_arid - arid)
        leaked = sorted(must_be_productive & arid)
        check(f"{name}: comments/logging/boilerplate marked arid", not missing,
              f"expected arid but productive: {missing}")
        check(f"{name}: mutable logic NOT marked arid", not leaked,
              f"productive lines wrongly marked arid: {leaked}")

    # A missing file must degrade, not raise: Step 2 can hand over a stale path.
    result = filter_file(str(tmp / "does-not-exist.ts"))
    check("missing file degrades gracefully", result["error"] and not result["arid_lines"])

    # Operator-aware equivalence: one line can hold both an unkillable
    # relational mutant and a perfectly killable arithmetic one.
    mixed = tmp / "mixed.ts"
    mixed.write_text("export const f = (a: number[], n: number) =>\n"
                     "  a.length == 0 ? 0 : n * 2;\n", encoding="utf-8")
    result = filter_file(str(mixed))
    equivalents = {int(k): v for k, v in result["equivalent_operators"].items()}
    check("equivalence found on the mixed line", 2 in equivalents)
    check("relational mutant suppressed on that line",
          is_suppressed(2, "EqualityOperator", set(result["arid_lines"]), equivalents))
    check("arithmetic mutant NOT suppressed on that line",
          not is_suppressed(2, "ArithmeticOperator", set(result["arid_lines"]), equivalents))
    check("unknown operator never suppressed",
          not is_suppressed(2, "SomethingNew", set(result["arid_lines"]), equivalents))

    print("\n[3b] operator family mapping across all five engines")
    for operator, want in {
        "EqualityOperator": {"relational"},          # Stryker
        "ConditionalsBoundaryMutator": {"relational"},  # PIT
        "NegateConditionalsMutator": {"relational"},    # PIT
        "MathMutator": {"arithmetic"},                  # PIT
        "ArithmeticOperator": {"arithmetic"},           # Stryker
        "ARITHMETIC_BASE": {"arithmetic"},              # gremlins
        "LogicalOperator": {"logical"},                 # Stryker
        "BooleanTrueReturnVals": {"return"},            # PIT
        "BlockStatement": set(),                        # Stryker, no family
        "mutmut": set(),
    }.items():
        eq(f"family({operator})", operator_family(operator), want)


# --- 4. engine report parsing, one real schema per engine ---------------------

def test_pit_parser(tmp):
    print("\n[4a] pitest -> mutations.xml")
    xml = tmp / "mutations.xml"
    xml.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<mutations>
 <mutation detected='true' status='KILLED' numberOfTestsRun='2'>
  <sourceFile>PolicyUtils.java</sourceFile><mutatedClass>com.a.PolicyUtils</mutatedClass>
  <mutatedMethod>isValid</mutatedMethod><lineNumber>42</lineNumber>
  <mutator>org.pitest.mutationtest.engine.gregor.mutators.MathMutator</mutator>
  <description>Replaced integer addition with subtraction</description><index>7</index>
 </mutation>
 <mutation detected='false' status='SURVIVED'>
  <sourceFile>PolicyUtils.java</sourceFile><mutatedClass>com.a.PolicyUtils</mutatedClass>
  <mutatedMethod>isValid</mutatedMethod><lineNumber>68</lineNumber>
  <mutator>org.pitest.mutationtest.engine.gregor.mutators.BooleanTrueReturnValsMutator</mutator>
  <description>replaced boolean return with true</description><index>3</index>
 </mutation>
 <mutation detected='false' status='NO_COVERAGE'>
  <sourceFile>PolicyUtils.java</sourceFile><mutatedClass>com.a.PolicyUtils</mutatedClass>
  <mutatedMethod>unused</mutatedMethod><lineNumber>90</lineNumber>
  <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
  <description>changed conditional boundary</description><index>1</index>
 </mutation>
</mutations>""", encoding="utf-8")
    muts = pit_parser.parse_mutations(str(xml))
    eq("pit: 3 records", len(muts), 3)
    eq("pit: operator short name", muts[0]["operator"], "Math")
    eq("pit: statuses", [m["status"] for m in muts],
       ["KILLED", "SURVIVED", "NO_COVERAGE"])
    summary = pit_parser.summarize(muts)
    eq("pit: mutation score counts NO_COVERAGE against you",
       summary["mutation_score"], 33.3)
    eq("pit: test strength ignores NO_COVERAGE", summary["test_strength"], 50.0)


def _stryker_report(path, file_key):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({
        "schemaVersion": "1.0",
        "files": {
            file_key: {
                "language": "typescript", "source": "x",
                "mutants": [
                    {"id": "1", "mutatorName": "ArithmeticOperator",
                     "replacement": "-", "status": "Killed",
                     "location": {"start": {"line": 26, "column": 9},
                                  "end": {"line": 26, "column": 40}}},
                    {"id": "2", "mutatorName": "EqualityOperator",
                     "replacement": "<", "status": "Survived",
                     "location": {"start": {"line": 23, "column": 5},
                                  "end": {"line": 23, "column": 22}}},
                    {"id": "3", "mutatorName": "BlockStatement",
                     "replacement": "{}", "status": "NoCoverage",
                     "location": {"start": {"line": 87, "column": 1},
                                  "end": {"line": 87, "column": 9}}},
                    {"id": "4", "mutatorName": "StringLiteral",
                     "replacement": "\"\"", "status": "Ignored",
                     "location": {"start": {"line": 9, "column": 1},
                                  "end": {"line": 9, "column": 5}}},
                    {"id": "5", "mutatorName": "ConditionalExpression",
                     "replacement": "true", "status": "Timeout",
                     "location": {"start": {"line": 44, "column": 1},
                                  "end": {"line": 44, "column": 5}}},
                ],
            }
        },
    }), encoding="utf-8")


def test_stryker_parser(tmp):
    print("\n[4b] stryker / stryker.net -> mutation.json")
    root = tmp / "repo"
    rel = "src/app/a.service.ts"
    (root / "src" / "app").mkdir(parents=True, exist_ok=True)
    report = root / "reports" / "mutation" / "mutation.json"
    _stryker_report(report, str(root / rel))

    muts = runners._parse_stryker_json(report, str(root))
    eq("stryker: Ignored mutants dropped", len(muts), 4)
    eq("stryker: repo-relative path recovered", muts[0]["file"], rel)
    eq("stryker: basename kept for PIT-style lookup", muts[0]["source_file"], "a.service.ts")
    eq("stryker: line from location.start", muts[0]["line"], 26)
    eq("stryker: status map", [m["status"] for m in muts],
       ["KILLED", "SURVIVED", "NO_COVERAGE", "TIMED_OUT"])

    # jsonReporter.fileName in the project's own config must be honoured.
    (root / "stryker.conf.json").write_text(json.dumps({
        "jsonReporter": {"fileName": "custom/out/mutation.json"}
    }), encoding="utf-8")
    _stryker_report(root / "custom" / "out" / "mutation.json", str(root / rel))
    targets = [str(p) for p in runners._stryker_json_targets(str(root), str(root / "x"))]
    check("configured jsonReporter path is searched",
          any("custom" in t for t in targets), targets)
    found = runners._newest(runners._stryker_json_targets(str(root), str(root / "x")),
                            "mutation.json")
    check("report discovered via config path", found is not None)

    # A second --mutate silently REPLACES the first, so all targets must go into
    # one comma-separated value.
    specs = []
    for src, tid in [("a.ts", "a.ts"), ("b.ts", "b.ts")]:
        specs.extend(runners._stryker_mutate_spec(tid, None))
    eq("stryker: one comma-joined --mutate value", ",".join(specs), "a.ts,b.ts")
    eq("stryker: productive lines become mutation ranges",
       runners._stryker_mutate_spec("a.ts", [7, 8, 9, 20]),
       ["a.ts:7-9", "a.ts:20-20"])
    eq("stryker: too many ranges falls back to whole file",
       runners._stryker_mutate_spec("a.ts", list(range(1, 400, 2))), ["a.ts"])


def test_test_narrowing(tmp):
    print("\n[4b2] narrowing the test suite Stryker executes")
    root = tmp / "ngrepo"
    svc = root / "src" / "app" / "shared"
    svc.mkdir(parents=True)
    (root / "src" / "app" / "other").mkdir(parents=True)
    (root / "e2e").mkdir(parents=True)

    src = "src/app/shared/calc.service.ts"
    (root / src).write_text("export class CalcService { add(a,b){return a+b;} }\n",
                            encoding="utf-8")
    # 1. its own co-located spec
    (svc / "calc.service.spec.ts").write_text(
        "import { CalcService } from './calc.service';\n"
        "describe('CalcService', () => { it('adds', () => {}); });\n", encoding="utf-8")
    # 2. a DIFFERENT spec that also imports it -- a mutant this spec kills must
    #    not be reported as surviving just because its killer was excluded.
    (root / "src" / "app" / "other" / "widget.component.spec.ts").write_text(
        "import { CalcService } from '../shared/calc.service';\n"
        "describe('Widget', () => { it('uses calc', () => {}); });\n", encoding="utf-8")
    # 3. an unrelated spec -- must NOT be selected
    (root / "src" / "app" / "other" / "unrelated.spec.ts").write_text(
        "import { Other } from './other';\ndescribe('Other', () => {});\n",
        encoding="utf-8")
    # 4. an e2e spec -- excluded by directory
    (root / "e2e" / "flow.spec.ts").write_text(
        "import { CalcService } from '../src/app/shared/calc.service';\n",
        encoding="utf-8")

    targets = [(src, src)]
    test_files = {src: "src/app/shared/calc.service.spec.ts"}
    specs = runners._related_specs(str(root), targets, test_files)

    check("own co-located spec selected",
          "src/app/shared/calc.service.spec.ts" in specs, specs)
    check("other spec that imports the target is also selected -- a mutant it "
          "kills must not look like a survivor",
          "src/app/other/widget.component.spec.ts" in specs, specs)
    check("unrelated spec NOT selected",
          "src/app/other/unrelated.spec.ts" not in specs, specs)
    check("e2e spec NOT selected (excluded directory)",
          "e2e/flow.spec.ts" not in specs, specs)

    eq("single spec needs no braces",
       runners._include_glob(["a/b.spec.ts"]), "a/b.spec.ts")
    eq("several specs become one brace glob (ng test --include takes one string)",
       runners._include_glob(["a.spec.ts", "b.spec.ts"]), "{a.spec.ts,b.spec.ts}")
    check("empty selection yields no glob", runners._include_glob([]) is None)

    # --- generated config ----------------------------------------------------
    (root / "stryker.conf.json").write_text(json.dumps({
        "$schema": "./node_modules/@stryker-mutator/core/schema/stryker-schema.json",
        "testRunner": "karma",
        "karma": {"projectType": "angular-cli", "configFile": "tests/karma/karma.conf.js",
                  "config": {"browsers": ["ChromeHeadless"]}},
        "coverageAnalysis": "perTest",
        "concurrency": 2,
    }, indent=2), encoding="utf-8")
    original = (root / "stryker.conf.json").read_text(encoding="utf-8")

    reports = root / "mutation-reports" / "r1" / "pit"
    path, note = runners._scoped_stryker_config(str(root), str(reports),
                                                targets, test_files)
    check("scoped config generated", path is not None, note)
    scoped = json.loads(Path(path).read_text(encoding="utf-8"))
    include = scoped["karma"]["ngConfig"]["testArguments"]["include"]
    check("include glob covers both relevant specs",
          "calc.service.spec.ts" in include and "widget.component.spec.ts" in include,
          include)
    check("unrelated spec absent from the glob", "unrelated" not in include, include)
    eq("existing karma settings preserved",
       scoped["karma"]["config"]["browsers"], ["ChromeHeadless"])
    eq("unrelated settings preserved", scoped["concurrency"], 2)
    check("$schema dropped -- its relative path would not resolve from the "
          "generated file's location", "$schema" not in scoped)
    eq("jsonReporter pinned to this run's report dir",
       scoped["jsonReporter"]["fileName"],
       "mutation-reports/r1/pit/mutation.json")
    eq("the project's own stryker.conf.json is NEVER modified",
       (root / "stryker.conf.json").read_text(encoding="utf-8"), original)

    # --- refusals: never narrow to zero tests --------------------------------
    orphan = "src/app/shared/orphan.service.ts"
    (root / orphan).write_text("export class Orphan {}\n", encoding="utf-8")
    path2, note2 = runners._scoped_stryker_config(
        str(root), str(reports), [(orphan, orphan)], {})
    check("no spec found -> do NOT narrow (zero tests would mark every mutant "
          "as survived)", path2 is None, note2)
    check("reason explains the fallback", "full suite" in note2, note2)

    (root / "stryker.conf.json").write_text(json.dumps({
        "testRunner": "jest", "karma": {"projectType": "custom"},
    }), encoding="utf-8")
    path3, note3 = runners._scoped_stryker_config(str(root), str(reports),
                                                  targets, test_files)
    check("non-angular-cli runner -> narrowing not applicable", path3 is None, note3)

    (root / "stryker.conf.json").unlink()
    path4, note4 = runners._scoped_stryker_config(str(root), str(reports),
                                                  targets, test_files)
    check("no stryker config -> no narrowing, no crash", path4 is None, note4)


def test_mutmut_parser(tmp):
    print("\n[4c] mutmut -> junitxml (v2) and results (v3)")
    junit = tmp / "mutmut-junit.xml"
    junit.write_text("""<?xml version="1.0" ?>
<testsuites><testsuite name="mutmut" tests="3">
 <testcase classname="app/calc.py" name="app/calc.py:12" time="0.1"/>
 <testcase classname="app/calc.py" name="app/calc.py:20" time="0.1">
   <failure message="Survived">--- app/calc.py:20\n+++ mutant\n-  a + b\n+  a - b</failure>
 </testcase>
 <testcase classname="app/calc.py" name="app/calc.py:33" time="0">
   <skipped message="no tests"/>
 </testcase>
</testsuite></testsuites>""", encoding="utf-8")
    eq("mutmut v3: space-separated `no tests` is NO_COVERAGE, not dropped",
       runners._parse_mutmut_v3_line("    app.calc.x_sub__mutmut_1: no tests"),
       ("app.calc.x_sub__mutmut_1", "NO_COVERAGE"))
    eq("mutmut v3: survived still parsed",
       runners._parse_mutmut_v3_line("    app.calc.x_add__mutmut_3: survived"),
       ("app.calc.x_add__mutmut_3", "SURVIVED"))
    eq("mutmut v3: non-result lines ignored", runners._parse_mutmut_v3_line("Running..."), None)
    eq("mutmut v3: file path becomes a mutant-name glob",
       runners._mutmut_v3_patterns("app/calc.py"), ["app.calc.*"])
    eq("mutmut v3: src layout offered with and without the prefix",
       runners._mutmut_v3_patterns("src/pkg/mod.py"), ["src.pkg.mod.*", "pkg.mod.*"])
    eq("mutmut v3: package __init__ maps to the package",
       runners._mutmut_v3_patterns("app/__init__.py"), ["app.*"])
    src = ["", "def add(a, b):", "    return a + b", "", "def sub(a, b):",
           "    if a > b:", "        return a - b", "    return 0"]
    show = ("# app.calc.x_sub__mutmut_1: no tests\n--- app/calc.py\n+++ app/calc.py\n"
            "@@ -1,4 +1,4 @@\n def sub(a, b):\n-    if a > b:\n+    if a >= b:\n"
            "         return a - b\n     return 0\n")
    eq("mutmut v3: file line = def line + function-relative offset",
       runners._mutmut_line_from_show(show, src, "app.calc.x_sub__mutmut_1"), 6)
    eq("mutmut v3: method mutant name resolves its function",
       runners._mutmut_line_from_show(show.replace("sub", "add"), ["class A:", "    def add(a):", "        if a:"],
                                      "app.calc.x\u01c1A\u01c1add__mutmut_2"), 3)
    eq("mutmut v3: unknown function stays 0, never invented",
       runners._mutmut_line_from_show(show, src, "app.calc.x_nope__mutmut_1"), 0)
    muts = runners._parse_mutmut_junit(junit, [("app/calc.py", "app/calc.py")])
    eq("mutmut v2: 3 records", len(muts), 3)
    eq("mutmut v2: pass=killed, failure=survived, skipped=no-coverage",
       [m["status"] for m in muts], ["KILLED", "SURVIVED", "NO_COVERAGE"])
    eq("mutmut v2: line numbers recovered, not 0",
       [m["line"] for m in muts], [12, 20, 33])
    eq("mutmut v2: repo-relative path", muts[0]["file"], "app/calc.py")

    eq("mutmut: dotted mutant id -> module path",
       runners._mutmut_locate("pkg.mod.calc__mutmut_3", "d.py", "a/d.py"),
       ("mod.py", "pkg/mod.py", 0))
    eq("mutmut: path:line mutant id", runners._mutmut_locate("pkg/mod.py:42", "", ""),
       ("mod.py", "pkg/mod.py", 42))
    check("mutmut v3 status map covers killed/survived/no_tests/timeout",
          {"killed", "survived", "no_tests", "timeout"}
          <= set(runners._MUTMUT_STATUS_V3))

    # `mutmut results` hides killed mutants unless asked: without --all the score never saw a kill.
    import unittest.mock as mock
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="    app.calc.x_add__mutmut_1: killed\n"
                           "    app.calc.x_add__mutmut_2: survived\n", stderr="")

    with mock.patch.object(runners.subprocess, "run", fake_run):
        rc, v3, err = runners._mutmut_results_v3(["mutmut"], str(tmp), [("app/calc.py", "app/calc.py")], "")
    results_call = next((c for c in calls if "results" in c), [])
    check("mutmut v3: results requested with --all so killed mutants are listed",
          "--all" in results_call and results_call[results_call.index("--all") + 1] == "true", results_call)
    eq("mutmut v3: killed mutant reaches the records",
       sorted(m["status"] for m in v3), ["KILLED", "SURVIVED"])


def test_gremlins_parser(tmp):
    print("\n[4d] gremlins -> gremlins.json")
    root = tmp / "gorepo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    report = root / "gremlins.json"
    # gremlins prints statuses WITH SPACES. Keying the status map on underscores
    # turned every Go mutant into RUN_ERROR and zeroed the score.
    report.write_text(json.dumps({
        "files": [{
            "filename": str(root / "pkg" / "util.go"),
            "mutations": [
                {"status": "KILLED", "type": "ARITHMETIC_BASE", "line": 12},
                {"status": "LIVED", "type": "CONDITIONALS_BOUNDARY", "line": 18},
                {"status": "NOT COVERED", "type": "INVERT_NEGATIVES", "line": 25},
                {"status": "TIMED OUT", "type": "ARITHMETIC_BASE", "line": 31},
                {"status": "NOT VIABLE", "type": "INVERT_LOOPCTRL", "line": 40},
            ],
        }],
    }), encoding="utf-8")
    muts = runners._parse_gremlins_json(report, str(root))
    eq("gremlins: space-separated statuses normalised",
       [m["status"] for m in muts],
       ["KILLED", "SURVIVED", "NO_COVERAGE", "TIMED_OUT", "NON_VIABLE"])
    eq("gremlins: no mutant misread as RUN_ERROR",
       sum(1 for m in muts if m["status"] == "RUN_ERROR"), 0)
    eq("gremlins: repo-relative path", muts[0]["file"], "pkg/util.go")

    # current gremlins: `file_name` relative to the unleashed package (not an absolute `filename`)
    (root / "calc").mkdir(parents=True, exist_ok=True)
    (root / "calc" / "calc.go").write_text("package calc\n", encoding="utf-8")
    (root / "other").mkdir(parents=True, exist_ok=True)
    (root / "other" / "calc.go").write_text("package other\n", encoding="utf-8")
    rep = root / "gremlins-new.json"
    rep.write_text(json.dumps({"go_module": "x", "files": [{"file_name": "calc.go", "mutations": [
        {"type": "CONDITIONALS_NEGATION", "status": "NOT COVERED", "line": 8, "column": 7},
        {"type": "ARITHMETIC_BASE", "status": "TIMED OUT", "line": 4, "column": 11}]}]}), encoding="utf-8")
    new = runners._parse_gremlins_json(rep, str(root), package_dir="./calc")
    eq("gremlins (file_name): resolved against the unleashed package",
       sorted({m["file"] for m in new}), ["calc/calc.go"])
    eq("gremlins (file_name): same basename in another package resolves by the package it ran on",
       {m["file"] for m in runners._parse_gremlins_json(rep, str(root), package_dir="./other")}, {"other/calc.go"})
    eq("gremlins (file_name): statuses kept", [m["status"] for m in new], ["NO_COVERAGE", "TIMED_OUT"])
    eq("gremlins (file_name): root package './...' or '.' maps to the root itself",
       runners._gremlins_rel_path("main.go", root, "./..."), "main.go")
    import run as _run_mod  # diff-scope keys must now match a changed-line map
    kept, out_of_diff = _run_mod._filter_to_diff_scope(new, {"calc/calc.go": {6, 7, 8}})
    eq("gremlins (file_name): diff-scope now drops the unchanged-line mutant instead of keeping it",
       ([m["line"] for m in kept], len(out_of_diff)), ([8], 1))


# --- 4b. score rounding matches Sonar ------------------------------------------

def test_score_rounding():
    print("\n[4b] score rounding: ties go up, like the Sonar plugin")
    pct = pit_parser.percent
    check("9/16 = 56.25 rounds to 56.3 (Python round() gives 56.2)", pct(9, 16) == 56.3, pct(9, 16))
    check("7/16 = 43.75 rounds to 43.8", pct(7, 16) == 43.8, pct(7, 16))
    check("1/3 and 2/3", (pct(1, 3), pct(2, 3)) == (33.3, 66.7), (pct(1, 3), pct(2, 3)))
    check("exact values and the edges", (pct(0, 5), pct(5, 5), pct(4, 5)) == (0.0, 100.0, 80.0))
    check("no denominator is 0.0, not an error", pct(3, 0) == 0.0)
    check("1599/2000 = 79.95 rounds to 80.0 (a tie at the gate edge goes up too)", pct(1599, 2000) == 80.0, pct(1599, 2000))
    muts = [{"status": "KILLED"}] * 9 + [{"status": "SURVIVED"}] * 7
    s = pit_parser.summarize(muts)
    check("summarize uses it for score and kill ratio", (s["mutation_score"], s["kill_ratio"]) == (56.3, 56.3),
          (s["mutation_score"], s["kill_ratio"]))
    check("test strength too: 9 / 16 covered", s["test_strength"] == 56.3, s["test_strength"])


# --- 5. suppression arithmetic and the gate ----------------------------------

def test_suppression_and_gate():
    print("\n[5] suppression arithmetic and the gate")
    muts = [
        {"status": "KILLED", "line": 10, "operator": "ArithmeticOperator",
         "file": "src/a.ts", "source_file": "a.ts", "language": "typescript"},
        {"status": "SURVIVED", "line": 3, "operator": "StringLiteral",
         "file": "src/a.ts", "source_file": "a.ts", "language": "typescript"},
        {"status": "SURVIVED", "line": 20, "operator": "EqualityOperator",
         "file": "src/a.ts", "source_file": "a.ts", "language": "typescript"},
        {"status": "SURVIVED", "line": 20, "operator": "ArithmeticOperator",
         "file": "src/a.ts", "source_file": "a.ts", "language": "typescript"},
        {"status": "KILLED", "line": 5, "operator": "Math",
         "file": "", "source_file": "B.java", "language": "java"},
        {"status": "NO_COVERAGE", "line": 9, "operator": "Math",
         "file": "", "source_file": "B.java", "language": "java"},
    ]
    arid_map = {"src/a.ts": {3}, "a.ts": {3}}
    equiv_map = {"src/a.ts": {20: ["relational"]}, "a.ts": {20: ["relational"]}}

    counted, suppressed = pit_parser.partition_suppressed(
        muts, arid_map, equiv_map, is_suppressed=is_suppressed)
    eq("arid line 3 suppressed", len(suppressed), 2)
    eq("suppressed = arid line + matching-operator equivalent",
       sorted((m["line"], m["operator"]) for m in suppressed),
       [(3, "StringLiteral"), (20, "EqualityOperator")])
    check("non-matching operator on an equivalent line survives suppression",
          any(m["line"] == 20 and m["operator"] == "ArithmeticOperator" for m in counted))

    summary = pit_parser.summarize(counted)
    # 4 counted: 2 KILLED, 1 SURVIVED, 1 NO_COVERAGE -> 50%. Suppressed mutants
    # leave BOTH numerator and denominator; leaving them in the denominator
    # while hiding them from the test-writer makes the gate unpassable.
    eq("score excludes suppressed from the denominator too",
       summary["mutation_score"], 50.0)

    nv = counted + [{"status": "NON_VIABLE", "line": 1, "operator": "ConditionalExpression",
                     "file": "src/a.ts", "source_file": "a.ts", "language": "typescript"}] * 4
    eq("NON_VIABLE (compile errors) leave the score denominator",
       pit_parser.summarize(nv)["mutation_score"], 50.0)
    eq("NON_VIABLE still reported as generated and as errors",
       (pit_parser.summarize(nv)["total"], pit_parser.summarize(nv)["non_viable"]), (8, 4))

    per_lang = pit_parser.summarize_by(counted, "language")
    eq("per-language split present", sorted(per_lang), ["java", "typescript"])
    eq("java sub-score", per_lang["java"]["mutation_score"], 50.0)
    eq("typescript sub-score", per_lang["typescript"]["mutation_score"], 50.0)

    survivors = pit_parser.parse_survivors(counted, arid_map=None)
    eq("survivors listed, survived before no-coverage",
       [s["category"] for s in survivors], ["survived", "no-coverage"])
    check("PIT basename-only records still resolve their file key",
          pit_parser.mutation_key(counted[-1]) == "B.java")


# --- 6. end-to-end: synthetic polyglot repo through run.py -------------------

_STUB_ENGINE = '''
"""Test double for mutation_runners: returns canned mutants, runs no engine."""
def execute(profile, project_root, module, targets, reports_dir, timeout=1800,
            quiet=True, log_path=None, productive_lines=None,
            skip_install_check=False, test_files=None, narrow_tests=True, exclude_tests=()):
    muts = []
    for source_file, _tid in targets:
        productive = sorted((productive_lines or {}).get(source_file) or [1])
        # one KILLED and one SURVIVED per file, both on productive lines
        for i, status in enumerate(("KILLED", "SURVIVED")):
            line = productive[min(i, len(productive) - 1)]
            muts.append({
                "status": status, "detected": status == "KILLED", "line": line,
                "operator": "ArithmeticOperator", "description": "stub",
                "class": "", "method": "", "index": str(i),
                "source_file": source_file.rsplit("/", 1)[-1], "file": source_file,
                "language": profile.language, "tool": profile.tool,
            })
    return 0, muts, None
'''

# Same shape as _STUB_ENGINE, but every mutant is KILLED -- the stand-in for a
# round 2 in which the agent's new tests actually worked. Swapped in mid-test to
# produce a clean verify run following a dirty round 1.
_STUB_ENGINE_ALL_KILLED = '''
"""Test double for mutation_runners: every mutant is killed."""
def execute(profile, project_root, module, targets, reports_dir, timeout=1800,
            quiet=True, log_path=None, productive_lines=None,
            skip_install_check=False, test_files=None, narrow_tests=True, exclude_tests=()):
    muts = []
    for source_file, _tid in targets:
        productive = sorted((productive_lines or {}).get(source_file) or [1])
        for i in range(2):
            line = productive[min(i, len(productive) - 1)]
            muts.append({
                "status": "KILLED", "detected": True, "line": line,
                "operator": "ArithmeticOperator", "description": "stub",
                "class": "", "method": "", "index": str(i),
                "source_file": source_file.rsplit("/", 1)[-1], "file": source_file,
                "language": profile.language, "tool": profile.tool,
            })
    return 0, muts, None
'''


def test_end_to_end(tmp):
    print("\n[6] end-to-end: synthetic polyglot repo through run.py")
    repo = tmp / "polyglot"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "app").mkdir(parents=True)

    # An Angular-shaped repo that ALSO carries a pom.xml purely for packaging --
    # the exact shape that made marker-priority detection report "java/pitest"
    # and mutate none of the TypeScript.
    (repo / "pom.xml").write_text(
        "<project><packaging>pom</packaging><artifactId>web</artifactId></project>",
        encoding="utf-8")
    (repo / "package.json").write_text('{"name":"app","devDependencies":{}}', encoding="utf-8")
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")

    (repo / "src" / "app" / "calc.service.ts").write_text(
        "export class CalcService {\n"
        "  add(a: number, b: number): number {\n"
        "    if (a <= 0) { return b; }\n"
        "    return a + b;\n"
        "  }\n}\n", encoding="utf-8")
    (repo / "src" / "app" / "calc.service.spec.ts").write_text(
        "describe('CalcService', () => { it('adds', () => {}); });\n", encoding="utf-8")
    (repo / "app" / "calc.py").write_text(
        "def add(a, b):\n    if a <= 0:\n        return b\n    return a + b\n",
        encoding="utf-8")
    (repo / "app" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n", encoding="utf-8")
    # A source file whose name merely contains "test" -- must NOT be dropped.
    (repo / "src" / "app" / "latest-news.ts").write_text(
        "export const pick = (n: number) => n > 1 ? n : 0;\n", encoding="utf-8")
    (repo / "src" / "app" / "latest-news.spec.ts").write_text(
        "describe('pick', () => { it('picks', () => {}); });\n", encoding="utf-8")

    detected = detect_languages(str(repo), probe_install=False)
    langs = [e["language"] for e in detected]
    check("java NOT reported: pom.xml with zero .java files", "java" not in langs, langs)
    check("typescript detected", "typescript" in langs, langs)
    check("python detected", "python" in langs, langs)

    # Copy the pipeline next to the synthetic repo and stub out the engines, so
    # the orchestrator, gate, report and Sonar export all run for real.
    skills = repo / ".claude" / "skills"
    for sub in ("run-mutation-workflow",):
        shutil.copytree(_SKILLS / sub, skills / sub)
    (skills / "run-mutation-workflow" / "scripts" / "mutation_runners.py").write_text(
        _STUB_ENGINE, encoding="utf-8")

    run_py = skills / "run-mutation-workflow" / "scripts" / "run.py"
    files = ["src/app/calc.service.ts", "app/calc.py", "src/app/latest-news.ts"]
    cmd = [sys.executable, str(run_py), "--threshold", "80", "--run-id", "selftest",
           "--skip-engine-check", "--remediate", "--skip-sonar"]
    for f in files:
        cmd += ["--file", f]
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                          timeout=300, check=False, encoding="utf-8", errors="replace")
    out = proc.stdout + proc.stderr

    eq("exit 2 on a failing gate (50% < 80%)", proc.returncode, 2)
    gate_path = repo / "mutation-reports" / "selftest" / "gate.json"
    check("gate.json written", gate_path.exists(), out[-1500:])
    if not gate_path.exists():
        return
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    eq("gate metric is mutation score, never test strength",
       gate["gateMetric"], "mutation_score")
    eq("verdict FAIL", gate["verdict"], "FAIL")
    eq("both languages mutated in ONE run", sorted(gate["languages"]),
       ["python", "typescript"])
    eq("both engines invoked", sorted(set(gate["tools"])), ["mutmut", "stryker"])
    eq("all three files targeted", len(gate["targetFiles"]), 3)
    check("latest-news.ts not dropped by a substring filter",
          any("latest-news" in f for f in gate["targetFiles"]), gate["targetFiles"])
    eq("per-language breakdown present", sorted(gate["perLanguage"]),
       ["python", "typescript"])
    eq("survivors handed to the agent", len(gate["survivorsForLlm"]), 3)
    check("every survivor names its own language",
          all(s.get("language") for s in gate["survivorsForLlm"]))
    check("prompt.txt written",
          (repo / "mutation-reports" / "selftest" / "prompt.txt").exists())
    prompt = (repo / "mutation-reports" / "selftest" / "prompt.txt").read_text(
        encoding="utf-8", errors="replace")
    check("prompt is polyglot-aware", "POLYGLOT" in prompt and "python" in prompt)

    # --- verify round: the stub still returns one survivor per file, so the
    # gate stays FAIL and round 2 must terminate as MANUAL_REVIEW, not loop.
    proc2 = subprocess.run(
        [sys.executable, str(run_py), "--verify", "--run-id", "selftest",
         "--threshold", "80", "--round", "2", "--tests-added", "3",
         "--skip-engine-check", "--skip-sonar"],
        cwd=str(repo), capture_output=True, text=True, timeout=300, check=False,
        encoding="utf-8", errors="replace")
    eq("exit 3 after round 2 -- terminal, no round 3", proc2.returncode, 3)
    gate2 = json.loads(gate_path.read_text(encoding="utf-8"))
    eq("verdict MANUAL_REVIEW", gate2["verdict"], "MANUAL_REVIEW")
    eq("round recorded", gate2["round"], 2)
    check("scoreBefore compared like-with-like (mutation score, not strength)",
          gate2["scoreBefore"] == gate["score"],
          f"{gate2.get('scoreBefore')} vs {gate['score']}")

    art = repo / "mutation-reports" / "selftest"
    for name in ("report.txt", "results.json", "telemetry.json"):
        check(f"artifact written: {name}", (art / name).exists())
    report_txt = (art / "report.txt").read_text(encoding="utf-8")
    check("report.txt gates on mutation score", "mutation score >= 80%" in report_txt)
    check("report.txt has a per-language section", "PER LANGUAGE" in report_txt)


# --- 7. nothing-to-mutate always writes a gate -------------------------------

def test_no_work_writes_gate(tmp):
    print("\n[7] every exit path writes gate.json")
    repo = tmp / "empty"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name":"e"}', encoding="utf-8")
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")

    skills = repo / ".claude" / "skills"
    for sub in ("run-mutation-workflow",):
        shutil.copytree(_SKILLS / sub, skills / sub)
    run_py = skills / "run-mutation-workflow" / "scripts" / "run.py"

    # a.ts has no spec file, so it is skipped -> nothing to mutate.
    proc = subprocess.run(
        [sys.executable, str(run_py), "--run-id", "nowork", "--file", "src/a.ts",
         "--skip-engine-check"],
        cwd=str(repo), capture_output=True, text=True, timeout=180, check=False,
        encoding="utf-8", errors="replace")
    eq("exit 0 when there is nothing to mutate", proc.returncode, 0)
    gate_path = repo / "mutation-reports" / "nowork" / "gate.json"
    check("gate.json still written", gate_path.exists(),
          (proc.stdout + proc.stderr)[-800:])
    if gate_path.exists():
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        eq("verdict NOTHING_TO_MUTATE, not UNKNOWN", gate["verdict"], "NOTHING_TO_MUTATE")
        check("llmRequired false", gate["llmRequired"] is False)
        check("reason recorded", bool(gate.get("reason")))


def test_validate_environment(tmp):
    print("\n[9] validate_environment: option-B engine check (language-scoped)")

    # --- 9a. Java-only project: only pitest should be checked ---
    java_repo = tmp / "java_only"
    (java_repo / "impl" / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (java_repo / "pom.xml").write_text(
        "<project><artifactId>impl</artifactId>"
        "<build><plugins><plugin>"
        "<groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8")
    (java_repo / "impl" / "src" / "main" / "java" / "com" / "example" / "Calc.java").write_text(
        "public class Calc { public int add(int a, int b) { return a + b; } }",
        encoding="utf-8")

    result_java = validate_environment.check_engines(str(java_repo))
    langs_java = result_java["detectedLanguages"]
    engines_java = {e["name"] for e in result_java["engines"]}

    check("java detected in java-only repo", "java" in langs_java, langs_java)
    check("only pitest checked for java project",
          engines_java == {"pitest"}, engines_java)
    check("stryker NOT checked for java project",
          "stryker" not in engines_java, engines_java)
    check("mutmut NOT checked for java project",
          "mutmut" not in engines_java, engines_java)
    check("gremlins NOT checked for java project",
          "gremlins" not in engines_java, engines_java)
    check("dotnet-stryker NOT checked for java project",
          "dotnet-stryker" not in engines_java, engines_java)

    # --- 9b. result shape is always correct ---
    for e in result_java["engines"]:
        check(f"result has required keys: {e['name']}",
              all(k in e for k in ("name", "languages", "available", "install_hint")))
        check(f"available is bool: {e['name']}", isinstance(e["available"], bool))

    # --- 9c. empty project: no languages detected, no engines checked ---
    empty_repo = tmp / "empty_env"
    empty_repo.mkdir(parents=True)
    result_empty = validate_environment.check_engines(str(empty_repo))
    check("no languages detected in empty repo",
          result_empty["detectedLanguages"] == [], result_empty["detectedLanguages"])
    check("no engines checked in empty repo",
          result_empty["engines"] == [], result_empty["engines"])
    check("allAvailable true when no engines required",
          result_empty["allAvailable"] is True)

    # --- 9d. JSON output is valid and contains expected keys ---
    ts_repo = tmp / "ts_only"
    (ts_repo / "src").mkdir(parents=True)
    (ts_repo / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (ts_repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (ts_repo / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    result_ts = validate_environment.check_engines(str(ts_repo))
    check("typescript detected in ts-only repo",
          "typescript" in result_ts["detectedLanguages"], result_ts["detectedLanguages"])
    check("stryker checked for typescript project",
          any(e["name"] == "stryker" for e in result_ts["engines"]))
    check("pitest NOT checked for ts-only project",
          not any(e["name"] == "pitest" for e in result_ts["engines"]))
    check("missingEngines key present", "missingEngines" in result_ts)
    check("allAvailable key present", "allAvailable" in result_ts)


def test_final_report(tmp):
    print("\n[7b] final_report.py renders the one block the agent relays")
    import final_report as fr
    fr.REPORTS = tmp / "reports"
    adir = fr.REPORTS / "r1"
    adir.mkdir(parents=True)
    tel = {"totalWallClockMs": 2500, "steps": {"step4": {"wallClockMs": 2000}}}
    (adir / "telemetry.json").write_text(json.dumps(tel), encoding="utf-8")

    def gate(**kw):
        (adir / "gate.json").write_text(json.dumps(kw), encoding="utf-8")
        return fr.render("r1")

    out = gate(verdict="PASS", score=91.0, threshold=80,
               perLanguage={"python": {"mutation_score": 91.0, "killed": 10, "total": 11}})
    check("pass: score + per-language + timing", "91.0% (threshold 80%)" in out
          and "python" in out and "TOTAL" in out, out)
    eq("pass: NEXT STOP", out.splitlines()[-1], "NEXT: STOP")

    out = gate(verdict="FAIL", score=40, threshold=80, llmRequired=True, newFileNeeded=[{"file": "a.py"}])
    check("fail+remediate: handoff block carries runId and newFileNeeded",
          'MUTATION_HANDOFF: {"runId": "r1"' in out and "newFileNeeded" in out, out)
    eq("fail+remediate: NEXT HANDOFF", out.splitlines()[-1], "NEXT: HANDOFF")

    out = gate(verdict="FAIL", score=40, threshold=80, llmRequired=False)
    check("fail without remediation: no handoff", "HANDOFF" not in out, out)

    out = gate(verdict="FAIL", score=40, threshold=80, llmRequired=True,
               survivorsForLlm=[{"language": "go", "file": "a.go", "line": 3, "operator": "ARITH"}],
               noCoverageMutants=[{"language": "ts", "file": "b.ts", "line": 9, "operator": "COND"}])
    check("fail: every mutant needing tests is listed for the human",
          "[go] a.go:3" in out and "[ts] b.ts:9" in out and "2 mutant(s)" in out, out)

    out = gate(verdict="MANUAL_REVIEW", phase="verify", score=60, scoreBefore=40, threshold=80,
               llmRequired=False,
               survivorsRemaining=[{"language": "go", "file": "x.go", "line": 7, "operator": "ARITH"}])
    check("exit-3: lists survivors with language/file/line and shows before->after",
          "[go] x.go:7" in out and "40% -> 60%" in out, out)
    eq("exit-3: NEXT STOP", out.splitlines()[-1], "NEXT: STOP")
    check("exit-3: framed as a bug with no further LLM run",
          "BUG to report" in out and "no further LLM run" in out, out)
    bug = (adir / "bug-report.md")
    check("exit-3: bug-report.md drafted with the surviving mutant",
          bug.exists() and "x.go:7" in bug.read_text(encoding="utf-8"))
    check("exit-3: never emits a handoff", "MUTATION_HANDOFF" not in out, out)

    out = gate(verdict="PASS", score=90, threshold=80,
               sonarUpload={"attempted": True, "ok": True, "qualityGate": "OK"})
    check("sonar ok: quality gate shown, commit reminder",
          "sonar: uploaded (quality gate OK)" in out and ".sonar/mutation-report.json" in out, out)
    out = gate(verdict="PASS", score=90, threshold=80, incomplete=True,
               sonarUpload={"attempted": True, "ok": False, "stage": "token-missing",
                            "message": "SONAR_TOKEN is not set\nexport it"})
    check("sonar failed: stage and cause shown, verdict unchanged",
          "UPLOAD FAILED [token-missing]" in out and "SONAR_TOKEN is not set" in out
          and "verdict: PASS" in out, out)
    check("no sonar section when the upload was skipped", "sonar" not in gate(verdict="PASS", score=90, threshold=80))

    out = gate(verdict="PASS", score=90, threshold=80)
    check("artifacts: relative paths, existing files only",
          "artifacts: mutation-reports/r1/gate.json" in out and "report.txt" not in out, out)

    (adir / "gate.json").unlink()
    check("missing gate.json reported as CRASHED", "CRASHED" in fr.render("r1"))

    # A blocked duplicate launch leaves LATEST on the run still in progress: report that, not CRASHED.
    lock = fr.REPORTS / ".wizard.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "startedAt": time.time(), "runId": "r1"}),
                    encoding="utf-8")
    out = fr.render("r1")
    check("live lock on this run: ALREADY_RUNNING, wait for its report",
          "ALREADY_RUNNING" in out and "CRASHED" not in out, out)
    eq("live lock: NEXT STOP", out.splitlines()[-1], "NEXT: STOP")
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "startedAt": time.time(), "runId": "r1"}),
                    encoding="utf-8")
    check("stale lock (dead pid) is ignored", "CRASHED" in fr.render("r1"))
    lock.unlink()


def test_duplicate_launch_keeps_latest(tmp):
    print("\n[7c] a blocked duplicate wizard launch leaves LATEST on the run in progress")
    repo = tmp / "dup_repo"
    reports = repo / "mutation-reports"
    reports.mkdir(parents=True)
    (reports / "LATEST").write_text("live1", encoding="utf-8")
    (reports / ".wizard.lock").write_text(
        json.dumps({"pid": os.getpid(), "startedAt": time.time(), "runId": "live1"}),
        encoding="utf-8")
    env = dict(os.environ, MUTATION_PROJECT_ROOT=str(repo))
    p = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "mutation_wizard.py"), "--run-id", "dup1"],
        cwd=str(repo), capture_output=True, text=True, timeout=120, check=False,
        encoding="utf-8", errors="replace", env=env)
    eq("duplicate launch exits 4 (ALREADY_RUNNING)", p.returncode, 4)
    eq("LATEST still names the run in progress",
       (reports / "LATEST").read_text(encoding="utf-8").strip(), "live1")
    check("no directory created for the blocked run", not (reports / "dup1").exists())


def _git_repo(root, quiet=True):
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(root), check=True,
                       capture_output=True, text=True)


def _git_commit(root, msg):
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(root), check=True,
                   capture_output=True, text=True)


# --- 7b. project root nested below the git toplevel ---------------------------

def _mkfile(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_nested_project_root(tmp):
    print("\n[7b] project root resolution when the package lives below the git toplevel")
    import changed_files_scanner as cfs
    saved = os.environ.pop("MUTATION_PROJECT_ROOT", None)
    saved_argv = sys.argv
    try:
        sys.argv = ["x"]
        repo = (tmp / "nested").resolve()
        _mkfile(repo / "README.md", "root\n")
        _mkfile(repo / "src" / "package.json", "{}")
        _mkfile(repo / "src" / "app" / "a.ts", "export const a = 1;\n")
        _git_repo(repo)
        _git_commit(repo, "init")
        _mkfile(repo / "src" / "app" / "a.ts", "export const a = 2;\n")

        eq("run from src/ -> src/ (no marker at toplevel)",
           cfs.resolve_project_root(str(repo / "src")), str(repo / "src"))
        eq("run from src/app -> the package dir src/",
           cfs.resolve_project_root(str(repo / "src" / "app")), str(repo / "src"))
        eq("run from the toplevel, change in src/ -> src/ (session started at the repo root)",
           cfs.resolve_project_root(str(repo)), str(repo / "src"))
        eq("run from the toplevel with no hints -> toplevel",
           cfs.resolve_project_root(str(repo), hint_files=[]), str(repo))
        eq("diff paths relative to the nested root",
           cfs._diff_files(str(repo / "src"), "HEAD"), ["app/a.ts"])
        eq("untracked files are also relative to the nested root",
           (_mkfile(repo / "src" / "app" / "new.ts", "x\n"), cfs._untracked(str(repo / "src")))[1],
           ["app/new.ts"])

        # explicit --file given from the toplevel, spelled relative to it
        sys.argv = ["x", "--file", "src/app/a.ts"]
        eq("--file from the toplevel -> its package",
           cfs.resolve_project_root(str(repo)), str(repo / "src"))
        sys.argv = ["x", "--file=src/app/a.ts"]
        eq("--file=value form", cfs.resolve_project_root(str(repo)), str(repo / "src"))
        sys.argv = ["x"]

        _mkfile(repo / "package.json", "{}")
        eq("toplevel with its own marker is kept (multi-module repos)",
           cfs.resolve_project_root(str(repo / "src")), str(repo))
        (repo / "package.json").unlink()

        os.environ["MUTATION_PROJECT_ROOT"] = str(repo / "src")
        eq("env var still wins", cfs.resolve_project_root(str(repo)), str(repo / "src"))
        del os.environ["MUTATION_PROJECT_ROOT"]

        # every ecosystem's marker, and outermost-not-nearest
        mono = (tmp / "mono").resolve()
        _mkfile(mono / "README.md", "root\n")
        _mkfile(mono / "svc" / "pom.xml", "<project/>")
        _mkfile(mono / "svc" / "mod-a" / "pom.xml", "<project/>")
        _mkfile(mono / "svc" / "mod-a" / "src" / "main" / "java" / "A.java", "class A {}\n")
        _mkfile(mono / "dotnet" / "App.sln", "")
        _mkfile(mono / "dotnet" / "src" / "Lib" / "Lib.csproj", "<Project/>")
        _mkfile(mono / "dotnet" / "src" / "Lib" / "C.cs", "class C {}\n")
        _mkfile(mono / "py" / "pyproject.toml", "")
        _mkfile(mono / "py" / "pkg" / "m.py", "x = 1\n")
        _mkfile(mono / "go" / "go.mod", "module x\n")
        _mkfile(mono / "go" / "internal" / "u.go", "package internal\n")
        _mkfile(mono / "gradle" / "settings.gradle", "")
        _mkfile(mono / "gradle" / "app" / "build.gradle", "")
        _mkfile(mono / "gradle" / "app" / "src" / "G.java", "class G {}\n")
        _mkfile(mono / "legacy" / "requirements.txt", "")
        _mkfile(mono / "legacy" / "lib" / "l.py", "y = 2\n")
        _git_repo(mono)
        _git_commit(mono, "init")
        for hint, want in (("svc/mod-a/src/main/java/A.java", "svc"),
                           ("dotnet/src/Lib/C.cs", "dotnet"),
                           ("py/pkg/m.py", "py"),
                           ("go/internal/u.go", "go"),
                           ("gradle/app/src/G.java", "gradle"),
                           ("legacy/lib/l.py", "legacy")):
            eq(f"{hint.split('/')[0]}: outermost marker dir {want}/ (from the repo root)",
               cfs.resolve_project_root(str(mono), hint_files=[hint]), str(mono / want))
        eq("cwd inside a Maven module -> the aggregator, not the module",
           cfs.resolve_project_root(str(mono / "svc" / "mod-a" / "src"), hint_files=[]), str(mono / "svc"))
        eq("several packages changed: the one with most files wins, deterministically",
           cfs.resolve_project_root(str(mono), hint_files=["py/pkg/m.py", "go/internal/u.go", "go/internal/u.go"]),
           str(mono / "go"))
        eq("a hint in no package leaves the toplevel",
           cfs.resolve_project_root(str(mono), hint_files=["README.md"]), str(mono))

        # changes in another package are reported, not silently dropped
        _mkfile(mono / "py" / "pkg" / "m.py", "x = 99\n")
        _mkfile(mono / "go" / "internal" / "u.go", "package internal\n// changed\n")
        outside = cfs.changes_outside_root(str(mono / "py"))
        _mkfile(mono / "sonar-project.properties", "sonar.projectKey=x\n")
        _mkfile(mono / "go" / "internal" / "u_test.go", "package internal\n")
        _mkfile(mono / "README.md", "changed\n")
        outside = cfs.changes_outside_root(str(mono / "py"))
        eq("only source files of other packages are listed (not README, config or tests)",
           outside, ["go/internal/u.go"])
        eq("nothing outside when the root is the toplevel", cfs.changes_outside_root(str(repo)), [])
    finally:
        sys.argv = saved_argv
        os.environ.pop("MUTATION_PROJECT_ROOT", None)
        if saved is not None:
            os.environ["MUTATION_PROJECT_ROOT"] = saved


def test_non_viable_and_sonar_monorepo(tmp):
    print("\n[7c] non-viable mutants leave the score everywhere; Sonar config found above the package")
    import sonar_report
    import sonar_upload
    mk = lambda st, ln=1: {"status": st, "line": ln, "operator": "Op", "file": "a.ts",
                           "source_file": "a.ts", "language": "typescript"}
    muts = [mk("KILLED")] * 4 + [mk("NON_VIABLE")] * 2
    summ = pit_parser.summarize(muts)
    eq("score ignores non-viable", summ["mutation_score"], 100.0)
    eq("scored_total is the score's denominator", pit_parser.scored_total(summ), 4)
    eq("non-viable are not engine errors", (summ["errors"], summ["non_viable"]), (0, 2))
    summ = pit_parser.summarize(muts + [mk("TIMED_OUT")])
    eq("a real engine error still counts as one", (summ["errors"], summ["non_viable"]), (1, 2))
    counts = sonar_report._counts(muts)
    eq("Sonar total excludes non-viable (matches the gate)", (counts["total"], counts["unknown"]), (4, 0))
    counts = sonar_report._counts(muts + [mk("SURVIVED")])
    check("Sonar parts still sum within total",
          counts["killed"] + counts["survived"] <= counts["total"], counts)

    mono = (tmp / "sonar_mono").resolve()
    _mkfile(mono / "sonar-project.properties", "sonar.projectKey=demo\nsonar.host.url=http://127.0.0.1:9\n")
    _mkfile(mono / "svc" / "package.json", "{}")
    _git_repo(mono)
    pkg = mono / "svc"
    eq("properties file found in a parent up to the repo root",
       sonar_report.find_properties_dir(pkg), mono)
    eq("no properties file -> None", sonar_report.find_properties_dir(tmp / "nowhere_here"), None)
    eq("report path is spelled relative to the properties file",
       sonar_report.report_path_from(mono, pkg), "svc/.sonar/mutation-report.json")
    eq("... and plain when they are the same dir",
       sonar_report.report_path_from(pkg, pkg), ".sonar/mutation-report.json")
    target = sonar_upload.detect_target(pkg, use_pr=False)
    check("target detected from the parent's properties", target.get("project") == "demo"
          and target.get("base") == mono, target)
    eq("wired once into the parent file", sonar_report.wire_properties(pkg), "added")
    eq("... and idempotent", sonar_report.wire_properties(pkg), "present")
    check("property holds the package-relative path",
          "sonar.mutation.reportPath=svc/.sonar/mutation-report.json"
          in (mono / "sonar-project.properties").read_text(encoding="utf-8"))
    check("nothing written into the package dir", not (pkg / "sonar-project.properties").exists())


def test_dotnet_mutate_spans(tmp):
    print("\n[7f] Stryker.NET --mutate: line ranges become character spans")
    import mutation_runners as runners
    src = (tmp / "cs_spans"); src.mkdir(exist_ok=True)

    def spans(name, data: bytes, ranges):
        f = src / name
        f.write_bytes(data)
        return runners._line_ranges_to_spans(f, ranges)

    lf = b"line1\nline2\nline3\n"
    eq("LF: line 2 is chars 6..11 (its text plus the line break)", spans("lf.cs", lf, [(2, 2)]), [(6, 11)])
    eq("LF: lines 1-3 run to the end of the file", spans("lf2.cs", lf, [(1, 3)]), [(0, 18)])
    eq("CRLF counts two characters per newline", spans("crlf.cs", b"ab\r\ncd\r\nef\r\n", [(2, 2)]), [(4, 7)])
    eq("a UTF-8 BOM is not counted (Roslyn positions exclude it)",
       spans("bom.cs", b"\xef\xbb\xbfab\ncd\n", [(2, 2)]), [(3, 6)])
    eq("non-BMP characters count as two UTF-16 units",
       spans("astral.cs", "a\U0001F600b\ncd\n".encode("utf-8"), [(2, 2)]), [(5, 8)])
    eq("last line without a trailing newline", spans("nonl.cs", b"ab\ncd", [(2, 2)]), [(3, 5)])
    eq("a line past the end of the file is dropped", spans("past.cs", b"ab\n", [(9, 9)]), [])
    eq("unreadable file -> None (caller mutates the whole file)",
       runners._line_ranges_to_spans(src / "missing.cs", [(1, 1)]), None)

    proj = tmp / "cs_proj"
    (proj / "Lib").mkdir(parents=True, exist_ok=True)
    (proj / "Lib" / "Calc.cs").write_bytes(b"class C {\n  int Half(int x) {\n    return x / 2;\n  }\n}\n")
    got = runners._dotnet_mutate_specs(proj / "Lib" / "Calc.cs", proj, {2, 3, 4})
    eq("spec uses character spans, not line numbers", got, ["**/Lib/Calc.cs{10..51}"])
    (proj / "Lib" / "Two.cs").write_bytes(b"a\nb\nc\nd\ne\n")
    eq("several productive ranges collapse to ONE bounding span (Stryker.NET intersects repeated ranges)",
       runners._dotnet_mutate_specs(proj / "Lib" / "Two.cs", proj, {1, 4}), ["**/Lib/Two.cs{0..7}"])
    eq("no productive lines -> whole file",
       runners._dotnet_mutate_specs(proj / "Lib" / "Calc.cs", proj, None), ["**/Lib/Calc.cs"])
    eq("unreadable source -> whole file",
       runners._dotnet_mutate_specs(proj / "Lib" / "Gone.cs", proj, {1}), ["**/Lib/Gone.cs"])


def test_gradle(tmp):
    print("\n[7g] Gradle: build tool, wrapper, project path, JUnit Platform, PIT command")
    import lang_profiles as lp
    g = (tmp / "gradle_multi").resolve()
    _mkfile(g / "settings.gradle.kts", 'include("core", "core:api")\n')
    _mkfile(g / "build.gradle.kts", "")
    _mkfile(g / "core/build.gradle.kts", "plugins { java }\n")
    _mkfile(g / "core/src/main/java/com/acme/Calc.java", "package com.acme; class Calc {}\n")
    _mkfile(g / "core/api/build.gradle.kts", "plugins { java }\n")
    _mkfile(g / "core/api/src/main/java/com/acme/Api.java", "package com.acme; class Api {}\n")
    _mkfile(g / "src/main/java/com/acme/Root.java", "package com.acme; class Root {}\n")
    eq("build.gradle.kts / settings.gradle.kts -> gradle", lp.java_build_tool(str(g)), "gradle")
    only_settings = (tmp / "gradle_settings_only").resolve()
    _mkfile(only_settings / "settings.gradle", "rootProject.name = 'x'\n")
    eq("settings.gradle alone -> gradle", lp.java_build_tool(str(only_settings)), "gradle")
    both = (tmp / "gradle_and_pom").resolve()
    _mkfile(both / "pom.xml", "<project/>")
    _mkfile(both / "build.gradle", "")
    eq("a root pom.xml keeps Maven (unchanged behaviour)", lp.java_build_tool(str(both)), "maven")
    eq("no build file -> maven (the old default)", lp.java_build_tool(str(tmp / "nothing_here")), "maven")

    eq("root project -> ':'", lp.gradle_project_for(str(g), ["src/main/java/com/acme/Root.java"]), ":")
    eq("subproject -> ':core'", lp.gradle_project_for(str(g), ["core/src/main/java/com/acme/Calc.java"]), ":core")
    eq("nested subproject -> ':core:api'",
       lp.gradle_project_for(str(g), ["core/api/src/main/java/com/acme/Api.java"]), ":core:api")
    eq("build_module_for dispatches to Gradle",
       lp.build_module_for(str(g), ["core/src/main/java/com/acme/Calc.java"]), ":core")
    eq("--module spellings: core/api, core:api, :core:api, '' ",
       [lp.gradle_project_path(m) for m in ("core/api", "core:api", ":core:api", "", None)],
       [":core:api", ":core:api", ":core:api", ":", ":"])

    wrapper = g / "gradlew"
    _mkfile(wrapper, "#!/bin/sh\n")
    eq("the wrapper is preferred over gradle on PATH", lp.gradle_executable(str(g)), str(wrapper))
    check("Gradle build with a wrapper counts as PIT installed (the init script brings the plugin)",
          lp._pitest_installed(str(g)))
    eq("engine command is the wrapper", lp._pitest_cmd(str(g)), [str(wrapper)])

    j4 = (tmp / "gradle_j4").resolve()
    _mkfile(j4 / "build.gradle", "dependencies { testImplementation 'junit:junit:4.13.2' }\n")
    _mkfile(j4 / "src/test/java/ATest.java", "import org.junit.Test;\n")
    check("JUnit 4 only -> not JUnit Platform", not lp.uses_junit_platform(str(j4)))
    j5 = (tmp / "gradle_j5").resolve()
    _mkfile(j5 / "build.gradle", "test { useJUnitPlatform() }\n")
    check("useJUnitPlatform() in the build -> JUnit Platform", lp.uses_junit_platform(str(j5)))
    conv = (tmp / "gradle_conv").resolve()
    _mkfile(conv / "build.gradle.kts", 'plugins { id("acme.java-conventions") }\n')
    _mkfile(conv / "buildSrc/src/main/kotlin/acme.java-conventions.gradle.kts", "tasks.test { useJUnitPlatform() }\n")
    check("useJUnitPlatform() in a buildSrc convention plugin -> JUnit Platform", lp.uses_junit_platform(str(conv)))
    imp = (tmp / "gradle_imports").resolve()
    _mkfile(imp / "build.gradle", "apply plugin: 'acme'\n")
    _mkfile(imp / "src/test/kotlin/ATest.kt", "import org.junit.jupiter.api.Test\n")
    check("a Jupiter import in the tests -> JUnit Platform", lp.uses_junit_platform(str(imp)))
    noise = (tmp / "gradle_noise").resolve()
    _mkfile(noise / "build.gradle", "")
    _mkfile(noise / "build/tmp/x.gradle", "useJUnitPlatform()\n")
    check("build output is not read", not lp.uses_junit_platform(str(noise)))

    import java_tests
    eq("package fallback: every test in the target's package, once",
       java_tests.package_globs(["com.acme.Calc", "com.acme.Other", "org.x.Y", "Top"]),
       ["com.acme.*", "org.x.*", "*"])
    init = pit_parser.GRADLE_INIT_SCRIPT
    text = init.read_text(encoding="utf-8") if init.exists() else ""
    check("pitest-init.gradle ships next to pit_parser and pins the plugin versions: 1.19.0, and 1.15.0 below Gradle 8.4",
          "'1.19.0'" in text and "'1.15.0'" in text and "GradleVersion.version('8.4')" in text, str(init))
    check("the init script honours a plugin mirror (MUTATION_GRADLE_PLUGIN_REPO)", "MUTATION_GRADLE_PLUGIN_REPO" in text)
    check("the init script sets dependency locking to LENIENT after the build is evaluated, never writes locks",
          "LockMode.LENIENT" in text and "write-locks" not in text
          and text.index("LockMode.LENIENT") > text.index("p.afterEvaluate"))
    check("the init script passes excluded test classes to PIT", "excludedTestClasses" in text)

    # The command the agent runs, through a fake Gradle that records its argv and writes a report.
    fake = (tmp / "fake_gradle_proj").resolve()
    _mkfile(fake / "build.gradle", "")
    argv_file = tmp / "gradle_argv.json"
    helper = tmp / "fake_gradle.py"
    _mkfile(helper, f"""import json, sys, pathlib
json.dump(sys.argv[1:], open({str(argv_file)!r}, "w"))
out = [a.split("=", 1)[1] for a in sys.argv if a.startswith("-PmutationReportDir=")][0]
pathlib.Path(out).mkdir(parents=True, exist_ok=True)
pathlib.Path(out, "mutations.xml").write_text("<mutations/>")
""")
    gradle = fake / "gradlew"
    _mkfile(gradle, f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n')
    os.chmod(gradle, 0o755)
    rep = tmp / "gradle_reports"
    rc = pit_parser.run_pit_gradle(str(fake), str(gradle), project_path=":core", target_classes=["com.acme.Calc"],
                                   reports_dir=str(rep), junit5=True, timeout=60, quiet=True,
                                   log_path=str(tmp / "gradle.log"),
                                   test_globs=["com.acme.CalcTest", "com.acme.CalcTest$*"],
                                   excluded_tests=["*IntegrationTest"])
    argv = json.loads(argv_file.read_text()) if argv_file.exists() else []
    eq("Gradle run exit code", rc, 0)
    check("task is :core:pitest with the init script", argv[:1] == [":core:pitest"] and str(init) in argv, argv[:4])
    check("project, targets, tests, JUnit Platform and an absolute report dir are passed",
          {"-PmutationProject=:core", "-PmutationTargets=com.acme.Calc",
           "-PmutationTests=com.acme.CalcTest,com.acme.CalcTest$*", "-PmutationExcludedTests=*IntegrationTest",
           "-PmutationJunit5=true", f"-PmutationReportDir={rep.resolve()}"} <= set(argv), argv)
    check("the report is found in the run's own directory",
          pit_parser.find_mutations_xml(str(fake), "core", str(rep)).endswith("mutations.xml"))
    os.chmod(gradle, 0o644)
    argv_file.unlink()
    rc = pit_parser.run_pit_gradle(str(fake), str(gradle), project_path=":", reports_dir=str(rep), timeout=60,
                                   quiet=True, log_path=str(tmp / "gradle.log"))
    argv = json.loads(argv_file.read_text()) if argv_file.exists() else []
    check("a wrapper without the executable bit still runs (through sh), root task is :pitest",
          rc == 0 and argv[:1] == [":pitest"] or os.name == "nt", (rc, argv[:1]))
    default_dir = (tmp / "gradle_default_dir").resolve()
    _mkfile(default_dir / "core/build/reports/pitest/mutations.xml", "<mutations/>")
    check("the plugin's default report dir is searched too",
          pit_parser.find_mutations_xml(str(default_dir), "core").endswith("core/build/reports/pitest/mutations.xml"))


def test_java_test_selection(tmp):
    print("\n[7g2] which tests PIT runs: the ones covering the class, no Docker tests without Docker")
    import java_tests as jt
    root = (tmp / "jsel").resolve()
    T = "src/test/java/com/acme/"
    _mkfile(root / "build.gradle", "")
    _mkfile(root / "src/main/java/com/acme/Calc.java", "package com.acme;\npublic class Calc {}\n")
    _mkfile(root / T / "CalcTest.java", "package com.acme;\nclass CalcTest { Calc c; @Test void a() {} }\n")
    _mkfile(root / T / "UnrelatedTest.java", "package com.acme;\nclass UnrelatedTest { @Test void a() {} }\n")
    _mkfile(root / T / "CalcNestedTest.java",
            "package com.acme;\nclass CalcNestedTest { @Nested class Deep { Calc c; } }\n")
    _mkfile(root / "src/test/java/com/other/ImportsCalcTest.java",
            "package com.other;\nimport com.acme.Calc;\nclass ImportsCalcTest { Calc c; }\n")
    _mkfile(root / "src/test/java/com/other/WildcardTest.java",
            "package com.other;\nimport com.acme.*;\nclass WildcardTest { Calc c; }\n")
    _mkfile(root / "src/test/java/com/other/OtherCalcTest.java",
            "package com.other;\nclass OtherCalcTest { Calc c; }\n")  # another class named Calc
    # container tests: direct, through a shared base class, through a support class
    _mkfile(root / T / "CalcContainerIT.java",
            "package com.acme;\n@Testcontainers\nclass CalcContainerIT { Calc c; }\n")
    _mkfile(root / T / "PgBase.java", "package com.acme;\nimport org.testcontainers.containers.PostgreSQLContainer;\n"
            "abstract class PgBase {}\n")
    _mkfile(root / T / "CalcViaBaseTest.java", "package com.acme;\nclass CalcViaBaseTest extends PgBase { Calc c; }\n")

    def sel(**kw):
        kw.setdefault("docker_ok", False)
        return jt.select_tests(str(root), "", ["com.acme.Calc"], **kw)

    r = sel()
    eq("mode: tests that name the class", r["mode"], "referencing")
    eq("selected: own package, import, wildcard import; not unrelated, not another Calc",
       sorted(n.rsplit(".", 1)[-1] for n in r["selected"]),
       ["CalcNestedTest", "CalcTest", "ImportsCalcTest", "WildcardTest"])
    eq("container tests found directly and through a base class",
       sorted(n.rsplit(".", 1)[-1] for n in r["container"]), ["CalcContainerIT", "CalcViaBaseTest"])
    check("nested classes are run with their test class", "com.acme.CalcTest$*" in r["tests"], r["tests"])
    check("container classes are passed to PIT as excluded",
          "com.acme.CalcContainerIT" in r["excluded"] and "com.acme.CalcViaBaseTest$*" in r["excluded"], r["excluded"])
    eq("files: project-relative paths of the selected tests",
       sorted(r["files"])[0], "src/test/java/com/acme/CalcNestedTest.java")

    r = sel(docker_ok=True)
    check("Docker reachable: container tests run", {"CalcContainerIT", "CalcViaBaseTest"} <=
          {n.rsplit(".", 1)[-1] for n in r["selected"]} and not r["container"], r)
    r = sel(exclude=["*ImportsCalcTest", "com.other.Wild*"])
    eq("--exclude-tests removes selected tests", sorted(n.rsplit(".", 1)[-1] for n in r["selected"]),
       ["CalcNestedTest", "CalcTest"])
    eq("and says which", sorted(n.rsplit(".", 1)[-1] for n in r["user_excluded"]), ["ImportsCalcTest", "WildcardTest"])
    r = sel(full_suite=True)
    eq("--full-suite: every test, container ones still left out", (r["mode"], r["tests"]), ("full", ["*"]))
    check("--full-suite keeps the exclusions", "com.acme.CalcContainerIT" in r["excluded"])
    eq("comma separated and repeated globs merge",
       jt.split_globs("a, b", ["b", "c,d"]), ["a", "b", "c", "d"])

    # a class no test names: the package's tests (minus what is left out)
    lone = (tmp / "jsel_lone").resolve()
    _mkfile(lone / "src/main/java/com/acme/Lone.java", "package com.acme;\npublic class Lone {}\n")
    _mkfile(lone / T / "SomethingTest.java", "package com.acme;\nclass SomethingTest {}\n")
    r = jt.select_tests(str(lone), "", ["com.acme.Lone"], docker_ok=False)
    eq("no test names it -> package fallback", (r["mode"], r["tests"]), ("package", ["com.acme.*"]))
    # the own test found by name counts even when its text does not name the class
    _mkfile(lone / T / "LoneTest.java", "package com.acme;\nclass LoneTest {}\n")
    r = jt.select_tests(str(lone), "", ["com.acme.Lone"], own_tests=[T + "LoneTest.java"], docker_ok=False)
    eq("own test file -> selected", r["selected"], ["com.acme.LoneTest"])
    # every covering test is a container test
    _mkfile(lone / T / "LoneContainerTest.java", "package com.acme;\n@Testcontainers\nclass LoneContainerTest { Lone l; }\n")
    (lone / T / "LoneTest.java").unlink()
    r = jt.select_tests(str(lone), "", ["com.acme.Lone"], docker_ok=False)
    eq("only container tests cover it, no Docker -> package fallback, flagged",
       (r["mode"], r["all_covering_left_out"]), ("package", True))
    # a subproject: its own src/test
    sub = (tmp / "jsel_sub").resolve()
    _mkfile(sub / "core/src/main/java/com/acme/Calc.java", "package com.acme;\npublic class Calc {}\n")
    _mkfile(sub / "core/src/test/java/com/acme/CalcTest.java", "package com.acme;\nclass CalcTest { Calc c; }\n")
    _mkfile(sub / "api/src/test/java/com/acme/ApiCalcTest.java", "package com.acme;\nclass ApiCalcTest { Calc c; }\n")
    r = jt.select_tests(str(sub), "core", ["com.acme.Calc"], docker_ok=True)
    eq("a subproject reads its own src/test only", r["selected"], ["com.acme.CalcTest"])
    old = os.environ.get(jt.EXCLUDE_ENV)
    os.environ[jt.EXCLUDE_ENV] = "*Slow*, *IT"
    try:
        eq("MUTATION_EXCLUDED_TESTS is read", jt.excluded_from_env(), ["*Slow*", "*IT"])
    finally:
        os.environ.pop(jt.EXCLUDE_ENV, None)
        if old:
            os.environ[jt.EXCLUDE_ENV] = old

    # the timeout estimate counts only the tests that run for the files in scope
    import mutation_wizard as wiz
    old_root = wiz.PROJECT_ROOT
    wiz.PROJECT_ROOT = str(root)
    try:
        _mkfile(root / T / "CalcTest.java",
                "package com.acme;\nclass CalcTest {\n @Test void a() {}\n @Test void b() {}\n Calc c;\n}\n")
        _mkfile(root / T / "UnrelatedTest.java",
                "package com.acme;\nclass UnrelatedTest {\n" + " @Test void x() {}\n" * 50 + "}\n")
        n, files = wiz._count_tests_in_scope(["src/main/java/com/acme/Calc.java"], [])
        check("timeout: only tests that cover the changed file are counted (not the 50 unrelated)",
              n == 2 and "src/test/java/com/acme/UnrelatedTest.java" not in files, (n, files))
        eq("timeout: nothing found -> (0, []) so the caller falls back to the repo-wide count",
           wiz._count_tests_in_scope(["README.md"], []), (0, []))
        eq("timeout: --file and --base values are read from the passthrough",
           (wiz._flag_values(["--file", "a", "--file=b", "--base", "x"], "--file"),
            wiz._scope_files(["--file", "a.java"])), (["a", "b"], ["a.java"]))
    finally:
        wiz.PROJECT_ROOT = old_root


def test_changes_spanning_modules(tmp):
    print("\n[7h] a change spanning several modules runs the engine once per module")
    import argparse
    import run as run_mod
    java = get_profile("java")
    py = get_profile("python")
    mvn = (tmp / "span_mvn").resolve()
    for mod in ("business-logic", "impl", "lib"):
        _mkfile(mvn / mod / "pom.xml", "<project/>")
        _mkfile(mvn / mod / f"src/main/java/{mod.replace('-', '')}/A.java", "class A {}\n")
    _mkfile(mvn / "pom.xml", "<project/>")
    files = ["business-logic/src/main/java/businesslogic/A.java", "impl/src/main/java/impl/A.java",
             "impl/src/main/java/impl/B.java", "lib/src/main/java/lib/A.java"]
    old_root = run_mod.PROJECT_ROOT
    try:
        run_mod.PROJECT_ROOT = str(mvn)
        auto = argparse.Namespace(module=None)
        by_mod = run_mod._modules_by_file(auto, java, files)
        eq("Maven: files grouped by their own module", {k: len(v) for k, v in by_mod.items()},
           {"business-logic": 1, "impl": 2, "lib": 1})
        eq("--module forces one module for every file",
           list(run_mod._modules_by_file(argparse.Namespace(module="impl"), java, files)), ["impl"])
        eq("a single module stays one group (no per-module report dirs)",
           list(run_mod._modules_by_file(auto, java, [files[1], files[2]])), ["impl"])
        eq("another language has one module whatever the files", len(run_mod._modules_by_file(auto, py, ["a.py", "b/c.py"])), 1)
    finally:
        run_mod.PROJECT_ROOT = old_root
    gr = (tmp / "span_gradle").resolve()
    _mkfile(gr / "settings.gradle.kts", 'include("core", "api")\n')
    _mkfile(gr / "build.gradle.kts", "")
    for mod in ("core", "api"):
        _mkfile(gr / mod / "build.gradle.kts", "plugins { java }\n")
    try:
        run_mod.PROJECT_ROOT = str(gr)
        by_mod = run_mod._modules_by_file(argparse.Namespace(module=None), java,
                                          ["core/src/main/java/A.java", "api/src/main/java/B.java"])
        eq("Gradle: files grouped by their own project", sorted(by_mod), [":api", ":core"])
    finally:
        run_mod.PROJECT_ROOT = old_root
    eq("module names make safe directory names",
       [run_mod._module_slug(m) for m in (":core:api", "business-logic", "", "a/b")], ["core-api", "business-logic", "root", "a-b"])

    # _run_engines: one engine call per module, each with only that module's targets and its own report dir.
    calls = []
    real = run_mod.mutation_runners.execute

    def fake(prof, root, module, targets, reports_dir, **kw):
        calls.append((module, [f for f, _ in targets], Path(reports_dir).name))
        return 0, [{"status": "KILLED", "file": targets[0][0], "line": 1}], None

    run_mod.mutation_runners.execute = fake
    try:
        run_mod.PROJECT_ROOT = str(mvn)
        args = argparse.Namespace(module=None, timeout=60, verbose=False, skip_engine_check=True, full_suite=False)
        targets = [(f, f) for f in files]
        muts, tools, err = run_mod._run_engines(targets, {f: java for f in files}, args, tmp / "span_reports")
    finally:
        run_mod.mutation_runners.execute = real
        run_mod.PROJECT_ROOT = old_root
    eq("one engine call per changed module", [c[0] for c in calls], ["business-logic", "impl", "lib"])
    eq("each call gets only its module's files", [len(c[1]) for c in calls], [1, 2, 1])
    eq("each module has its own report directory", [c[2] for c in calls], ["business-logic", "impl", "lib"])
    eq("mutants of every module are merged, the tool is listed once", (len(muts), tools, err), (3, ["pitest"], None))

    def failing(prof, root, module, targets, reports_dir, **kw):
        return (1, None, "boom") if module == "impl" else (0, [], None)

    run_mod.mutation_runners.execute = failing
    try:
        run_mod.PROJECT_ROOT = str(mvn)
        _, _, err = run_mod._run_engines(targets, {f: java for f in files}, args, tmp / "span_reports2")
    finally:
        run_mod.mutation_runners.execute = real
        run_mod.PROJECT_ROOT = old_root
    check("a failing module stops the run and names the module", err is not None and "module impl" in err and "boom" in err, err)


def test_module_file_and_dotnet_env(tmp):
    print("\n[7e] Maven module inference, --file spelling, DOTNET_ROOT")
    import lang_profiles as lp
    import mutation_runners as runners
    single = (tmp / "mvn_single").resolve()
    _mkfile(single / "pom.xml", "<project/>")
    _mkfile(single / "src/main/java/A.java", "class A {}\n")
    eq("single-module build -> no -pl (the old hard-coded 'impl' broke every such repo)",
       lp.maven_module_for(str(single), ["src/main/java/A.java"]), "")
    multi = (tmp / "mvn_multi").resolve()
    _mkfile(multi / "pom.xml", "<project/>")
    _mkfile(multi / "mod-a/pom.xml", "<project/>")
    _mkfile(multi / "mod-a/src/main/java/A.java", "class A {}\n")
    _mkfile(multi / "mod-b/pom.xml", "<project/>")
    _mkfile(multi / "mod-b/src/main/java/B.java", "class B {}\n")
    _mkfile(multi / "impl/pom.xml", "<project/>")
    _mkfile(multi / "impl/src/main/java/I.java", "class I {}\n")
    eq("multi-module: the file's own module", lp.maven_module_for(str(multi), ["mod-a/src/main/java/A.java"]), "mod-a")
    eq("multi-module: the legacy 'impl' module is still found by its pom",
       lp.maven_module_for(str(multi), ["impl/src/main/java/I.java"]), "impl")
    eq("multi-module: the module with most files wins",
       lp.maven_module_for(str(multi), ["mod-a/src/main/java/A.java", "mod-b/src/main/java/B.java",
                                        "mod-b/src/main/java/B.java"]), "mod-b")
    eq("no pom.xml anywhere -> no module", lp.maven_module_for(str(tmp / "no_pom_here"), ["x/Y.java"]), "")

    # wizard: --file may be spelled from the caller's cwd while the child runs from the package
    import mutation_wizard as wiz
    old_root, old_cwd = wiz.PROJECT_ROOT, os.getcwd()
    top = (tmp / "wiz_top").resolve()
    _mkfile(top / "pkg/app/a.ts", "x\n")
    try:
        wiz.PROJECT_ROOT = str(top / "pkg")
        os.chdir(top)
        eq("--file spelled from the repo root is rewritten relative to the package",
           wiz._normalise_file_args(["--file", "pkg/app/a.ts", "--threshold", "80"]),
           ["--file", "app/a.ts", "--threshold", "80"])
        eq("--file=value form", wiz._normalise_file_args(["--file=pkg/app/a.ts"]), ["--file=app/a.ts"])
        eq("already relative to the package: unchanged", wiz._normalise_file_args(["--file", "app/a.ts"]),
           ["--file", "app/a.ts"])
        eq("absolute path", wiz._normalise_file_args(["--file", str(top / "pkg/app/a.ts")]), ["--file", "app/a.ts"])
        eq("a path that does not exist is left for the pipeline to report",
           wiz._normalise_file_args(["--file", "nope.ts"]), ["--file", "nope.ts"])
    finally:
        os.chdir(old_cwd)
        wiz.PROJECT_ROOT = old_root

    # DOTNET_ROOT for a package-manager install (the apphost cannot find the runtime otherwise)
    fake = (tmp / "fake_dotnet_root").resolve()
    (fake / "sdk").mkdir(parents=True)
    _mkfile(fake / "dotnet", "#!/bin/sh\n")
    (fake / "dotnet").chmod(0o755)
    shim = (tmp / "dotnet_shim_bin").resolve()
    shim.mkdir()
    if hasattr(os, "symlink"):
        (shim / "dotnet").symlink_to(fake / "dotnet")
        saved = {k: os.environ.pop(k, None) for k in ("DOTNET_ROOT", "DOTNET_ROOT_ARM64")}
        saved_path = os.environ["PATH"]
        os.environ["PATH"] = str(shim) + os.pathsep + saved_path
        try:
            env = runners._dotnet_env(str(tmp))
            eq("DOTNET_ROOT derived from the dotnet on PATH (symlink resolved)",
               (env or {}).get("DOTNET_ROOT"), str(fake))
            os.environ["DOTNET_ROOT"] = "/somewhere/else"
            env = runners._dotnet_env(str(tmp))
            check("an explicit DOTNET_ROOT is never overridden",
                  env is None or env.get("DOTNET_ROOT") == "/somewhere/else")
        finally:
            os.environ["PATH"] = saved_path
            os.environ.pop("DOTNET_ROOT", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def test_test_file_lookup(tmp):
    print("\n[7d] test-file lookup across layouts, languages and test frameworks")
    import lang_profiles as lp

    def tree(name, files):
        root = (tmp / name).resolve()
        for rel in files:
            _mkfile(root / rel, "x\n")
        return str(root)

    def found(root, src, fn):
        got = fn(root, src)
        return None if got is None else os.path.relpath(got, root).replace(os.sep, "/")

    # --- TypeScript / JavaScript: jest, mocha, vitest, jasmine, karma layouts
    for label, spec in (
        ("co-located *.spec.ts (Angular/Jasmine/Karma)", "app/utils/timing.spec.ts"),
        ("__tests__/ next to the source (Jest)", "app/utils/__tests__/timing.test.ts"),
        ("tests/ mirroring src/ (Mocha)", "tests/utils/timing.test.ts"),
        ("flat tests/unit-test/ (#765, Jest)", "tests/unit-test/timing.test.ts"),
        ("flat test/unit/ (Mocha)", "test/unit/timing.spec.ts"),
        ("nested tests/unit-test/<mirrored>/ (Vitest)", "tests/unit-test/app/utils/timing.test.ts"),
        ("spec/ directory (Jasmine)", "spec/timing.spec.ts"),
        ("tests/<app dir mirrored>/ (not under src/)", "tests/app/utils/timing.test.ts"),
    ):
        root = tree("ts_" + str(abs(hash(label))), ["app/utils/timing.ts", spec])
        eq(f"ts: {label}", found(root, "app/utils/timing.ts", lp._ts_test), spec)
    root = tree("ts_prio", ["app/a.ts", "app/a.spec.ts", "tests/unit-test/a.test.ts"])
    eq("ts: existing layouts keep priority over the fallback", found(root, "app/a.ts", lp._ts_test), "app/a.spec.ts")
    root = tree("ts_amb", ["app/a.ts", "tests/unit/a.test.ts", "tests/integration/a.test.ts"])
    eq("ts: unit/ preferred when the same spec name exists twice", found(root, "app/a.ts", lp._ts_test),
       "tests/unit/a.test.ts")
    root = tree("ts_tie", ["app/a.ts", "tests/x/a.test.ts", "tests/y/a.test.ts"])
    eq("ts: a genuine tie binds nothing (falls back to the would-be new file)",
       found(root, "app/a.ts", lp._ts_test), "app/a.spec.ts")
    root = tree("ts_none", ["app/auth.ts", "tests/unit-test/authService.test.ts"])
    check("ts: stem mismatch (auth vs authService) is not guessed",
          not os.path.exists(lp._ts_test(root, "app/auth.ts")))
    root = tree("ts_jsx", ["src/ui/Btn.jsx", "tests/ui/Btn.test.jsx"])
    eq("js: extension follows the source (.jsx)", found(root, "src/ui/Btn.jsx", lp._ts_test), "tests/ui/Btn.test.jsx")
    root = tree("py_mutants", ["calc.py", "tests/unit/test_calc.py", "mutants/tests/unit/test_calc.py",
                               "mutants/calc.py"])
    eq("python: mutmut's mutants/ copy of the tests is not a second candidate",
       found(root, "calc.py", lp._python_test), "tests/unit/test_calc.py")
    root = tree("ts_nm", ["app/a.ts", "node_modules/pkg/tests/a.test.ts"])
    check("ts: node_modules is never searched", not os.path.exists(lp._ts_test(root, "app/a.ts")))

    # --- Python: pytest / unittest layouts
    for label, spec in (
        ("tests/ flat (pytest)", "tests/test_mod.py"),
        ("tests/unit/ flat", "tests/unit/test_mod.py"),
        ("tests/<pkg>/ mirrored", "tests/pkg/test_mod.py"),
        ("unittest-style mod_test.py in tests/", "tests/mod_test.py"),
        ("co-located test_mod.py", "pkg/test_mod.py"),
    ):
        root = tree("py_" + str(abs(hash(label))), ["pkg/mod.py", spec])
        eq(f"python: {label}", found(root, "pkg/mod.py", lp._python_test), spec)
    root = tree("py_src", ["src/pkg/mod.py", "tests/unit/pkg/test_mod.py"])
    eq("python: src/ layout, tests mirrored under tests/unit/",
       found(root, "src/pkg/mod.py", lp._python_test), "tests/unit/pkg/test_mod.py")

    # --- Java / Kotlin: JUnit, TestNG, Spock/Kotest naming
    for label, spec in (
        ("JUnit mirrored *Test", "src/test/java/com/acme/Calc" + "Test.java"),
        ("*Tests", "src/test/java/com/acme/CalcTests.java"),
        ("*IT", "src/test/java/com/acme/CalcIT.java"),
        ("*Spec", "src/test/java/com/acme/CalcSpec.java"),
        ("test in another package", "src/test/java/com/acme/it/CalcTest.java"),
    ):
        root = tree("java_" + str(abs(hash(label))), ["src/main/java/com/acme/Calc.java", spec])
        eq(f"java: {label}", found(root, "src/main/java/com/acme/Calc.java", lp._java_test), spec)
    root = tree("java_mm", ["a/src/main/java/p/Calc.java", "a/src/test/java/q/CalcTest.java",
                            "b/src/test/java/q/CalcTest.java"])
    eq("java: multi-module, the module's own test wins over a same-named one elsewhere",
       found(root, "a/src/main/java/p/Calc.java", lp._java_test), "a/src/test/java/q/CalcTest.java")
    root = tree("kt", ["src/main/kotlin/p/Svc.kt", "src/test/kotlin/p/SvcTest.kt"])
    eq("kotlin: *Test.kt", found(root, "src/main/kotlin/p/Svc.kt", lp._java_test), "src/test/kotlin/p/SvcTest.kt")

    # --- PIT report discovery for a single-module build (empty module name)
    import time as _t
    pit_root = tree("pit_single", ["target/pit-reports/mutations.xml"])
    found_xml = pit_parser.find_mutations_xml(pit_root, module="", reports_dir=None, min_mtime=_t.time() - 60)
    eq("PIT: single-module report found under <root>/target/pit-reports (empty module)",
       os.path.relpath(found_xml, pit_root).replace(os.sep, "/"), "target/pit-reports/mutations.xml")
    pit_multi = tree("pit_multi", ["mod-a/target/pit-reports/mutations.xml"])
    eq("PIT: multi-module report found under <root>/<module>/target/pit-reports",
       os.path.relpath(pit_parser.find_mutations_xml(pit_multi, module="mod-a", min_mtime=_t.time() - 60),
                       pit_multi).replace(os.sep, "/"), "mod-a/target/pit-reports/mutations.xml")

    # --- Go and C#: conventions already covered, pinned here so they stay that way
    root = tree("go", ["internal/u.go", "internal/u_test.go"])
    eq("go: <name>_test.go next to the source", found(root, "internal/u.go", lp._go_test), "internal/u_test.go")
    root = tree("cs", ["src/Lib/Calc.cs", "tests/Lib.Tests/CalcTests.cs", "tests/Lib.Tests/CalcV2Tests.cs"])
    eq("c#: <Name>Tests.cs in a separate test project", found(root, "src/Lib/Calc.cs", lp._csharp_test),
       "tests/Lib.Tests/CalcTests.cs")


# --- 8. diff-scoped line filtering, --remediate gate, new-file confirmation --

def test_new_guardrails(tmp):
    print("\n[8] diff-scoped lines, --remediate gate, new-file confirmation")

    # -- 8a. pure function: hunk parsing turns a real `git diff -U0` into the
    # exact new-file line numbers changed, nothing more.
    repo = tmp / "diffscope"
    (repo / "app").mkdir(parents=True)
    src = repo / "app" / "calc.py"
    src.write_text(
        "def add(a, b):\n"      # 1
        "    if a <= 0:\n"      # 2
        "        return b\n"    # 3
        "    return a + b\n",   # 4
        encoding="utf-8")
    _git_repo(repo)
    _git_commit(repo, "baseline")

    src.write_text(
        "def add(a, b):\n"          # 1  unchanged
        "    if a <= 0:\n"          # 2  unchanged
        "        return b - 1\n"    # 3  CHANGED
        "    return a + b\n",       # 4  unchanged
        encoding="utf-8")

    ranges = resolve_line_ranges_for_files(str(repo), ["app/calc.py"])
    eq("changed-line set is exactly the one modified line", ranges["app/calc.py"], {3})

    # A brand-new (untracked) file has no diff to compute against -- the whole
    # file must count, not nothing.
    (repo / "app" / "new_mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ranges2 = resolve_line_ranges_for_files(str(repo), ["app/new_mod.py"])
    check("untracked file scopes to the whole file (None), not zero lines",
          ranges2["app/new_mod.py"] is None, ranges2)

    # -- 8b/8c. --remediate gate and the new-file confirmation gate, exercised
    # through the real orchestrator with the stub engine.
    poly = tmp / "guardrails"
    (poly / "app").mkdir(parents=True)
    (poly / "pyproject.toml").write_text("[project]\nname='g'\n", encoding="utf-8")
    (poly / "app" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (poly / "app" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n", encoding="utf-8")
    # A second module with NO test file at all -- this is the one that needs
    # a brand-new file, and only reaches Step 4 at all via --include-untested.
    (poly / "app" / "brandnew.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")

    skills = poly / ".claude" / "skills"
    for sub in ("run-mutation-workflow",):
        shutil.copytree(_SKILLS / sub, skills / sub)
    (skills / "run-mutation-workflow" / "scripts" / "mutation_runners.py").write_text(
        _STUB_ENGINE, encoding="utf-8")
    run_py = skills / "run-mutation-workflow" / "scripts" / "run.py"

    base_cmd = [sys.executable, str(run_py), "--threshold", "80",
               "--skip-engine-check", "--include-untested", "--skip-sonar",
               "--file", "app/calc.py", "--file", "app/brandnew.py"]

    # Without --remediate: gate fails, but prompt.txt must NOT be written, and
    # llmRequired must be false -- invoking the pipeline is not consent for AI
    # to start editing test files.
    proc = subprocess.run(base_cmd + ["--run-id", "noremediate"], cwd=str(poly),
                          capture_output=True, text=True, timeout=120, check=False,
                          encoding="utf-8", errors="replace")
    eq("exit 2 on gate fail even without --remediate", proc.returncode, 2)
    gate_path = poly / "mutation-reports" / "noremediate" / "gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
    check("llmRequired is false without --remediate", gate.get("llmRequired") is False, gate)
    check("prompt.txt NOT written without --remediate",
          not (poly / "mutation-reports" / "noremediate" / "prompt.txt").exists())

    # With --remediate: prompt.txt IS written, but the survivor in brandnew.py
    # (no existing test file) is held back, not silently auto-created.
    proc2 = subprocess.run(base_cmd + ["--run-id", "remediate", "--remediate"],
                           cwd=str(poly), capture_output=True, text=True, timeout=120,
                           check=False, encoding="utf-8", errors="replace")
    eq("exit 2 on gate fail with --remediate", proc2.returncode, 2)
    art = poly / "mutation-reports" / "remediate"
    gate2 = json.loads((art / "gate.json").read_text(encoding="utf-8"))
    check("llmRequired is true with --remediate", gate2.get("llmRequired") is True)
    new_needed = gate2.get("newFileNeeded") or []
    check("brandnew.py's survivor held back as newFileNeeded", len(new_needed) > 0, new_needed)
    prompt = (art / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    check("prompt.txt names the held-back file in a HELD BACK note (not silent)",
          "HELD BACK" in prompt and "brandnew.py" in prompt, prompt[-600:])
    check("prompt.txt does NOT embed the held-back file's full source",
          "SOURCE FILE (app/brandnew.py)" not in prompt, prompt[-600:])
    check("prompt.txt still includes the file with an existing test",
          "SOURCE FILE (app/calc.py)" in prompt)

    # A human approves creation after the fact: --regen-prompt --allow-new-files
    # rewrites prompt.txt WITHOUT re-running any engine.
    proc3 = subprocess.run(
        [sys.executable, str(run_py), "--regen-prompt", "--allow-new-files",
         "--run-id", "remediate"],
        cwd=str(poly), capture_output=True, text=True, timeout=60, check=False,
        encoding="utf-8", errors="replace")
    eq("--regen-prompt exits 0", proc3.returncode, 0)
    prompt2 = (art / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    check("after approval, prompt.txt includes the previously held-back file",
          "brandnew.py" in prompt2, prompt2[-600:])


# --- 12-16. Sonar report + upload ---------------------------------------------

def _validate(value, schema, root, path="$"):
    """Minimal JSON-Schema checker (type, required, properties,
    additionalProperties, items, enum, const, minimum, maximum, $ref, allOf). The repo
    has no jsonschema dependency, and the contract only needs these keywords."""
    errs = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _validate(value, node, root, path)
    for sub in schema.get("allOf", []):
        errs += _validate(value, sub, root, path)
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}
    if "type" in schema:
        ok = isinstance(value, types[schema["type"]]) and not isinstance(value, bool)
        if not ok:
            return errs + [f"{path}: expected {schema['type']}"]
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errs.append(f"{path}: {value} < {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)) and value > schema["maximum"]:
        errs.append(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errs += _validate(value[key], sub, root, f"{path}.{key}")
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, v in value.items():
                if key not in props:
                    errs += _validate(v, extra, root, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += _validate(v, schema["items"], root, f"{path}[{i}]")
    return errs


def _counts_ok(c):
    """The plugin's own rule: total >= the sum of its parts."""
    parts = sum(c.get(k, 0) for k in
                ("killed", "timedOut", "memoryError", "survived", "noCoverage", "unknown"))
    return c.get("total", parts) >= parts


def _m(status, line, file="src/a.ts", language="typescript", operator="Eq", **extra):
    rec = {"status": status, "detected": status == "KILLED", "line": line, "operator": operator,
           "description": "d", "class": "", "method": "", "index": "", "source_file":
           file.rsplit("/", 1)[-1], "file": file, "language": language, "tool": "t"}
    rec.update(extra)
    return rec


def _build(muts, diff_map, **kw):
    import sonar_report
    from run import _filter_to_diff_scope  # noqa: PLC0415 - run.py has no side effects on import
    return sonar_report.build_report(
        run_id="r1", threshold=80, round_no=1, verdict="FAIL", tools=["stryker", "pit"],
        agent_version="9.9", project_root=Path("."), all_mutations=muts,
        scope_filter=_filter_to_diff_scope, diff_map=diff_map,
        arid_map=kw.get("arid_map", {}), equivalent_map={},
        target_files=kw.get("target_files", ["src/a.ts", "tool/b.py"]),
        productive=kw.get("productive", {"src/a.ts": list(range(1, 41)), "tool/b.py": list(range(1, 21))}),
        score_before=kw.get("score_before"), duration_sec=kw.get("duration_sec"))


def test_sonar_report_content_and_schema(tmp):
    print("\n[12] sonar_report: scopes, per-file, issues, per-test data, schema contract")
    import sonar_report
    muts = [
        _m("KILLED", 10), _m("SURVIVED", 11), _m("NO_COVERAGE", 12), _m("TIMED_OUT", 13),
        _m("KILLED", 90), _m("SURVIVED", 91),                        # outside the diff
        _m("RUN_ERROR", 14), _m("MEMORY_ERROR", 15),
        _m("SURVIVED", 5, file="tool/b.py", language="python", operator="Block"),
    ]
    diff = {"src/a.ts": {10, 11, 12, 13, 14, 15}, "tool/b.py": None}
    r = _build(muts, diff)
    o, n = r["summary"]["overall"], r["summary"]["new"]
    eq("overall counts everything", (o["total"], o["killed"], o["survived"]), (9, 2, 3))
    eq("new counts only changed lines", (n["total"], n["killed"], n["survived"]), (7, 1, 2))
    eq("timeouts / memory errors / unknown kept apart",
       (n["timedOut"], n["memoryError"], n["unknown"], n["noCoverage"]), (1, 1, 1, 1))
    check("no per-test data -> keys absent, not zero", "testsExecuted" not in o)
    check("counts satisfy the plugin's total >= parts rule",
          all(_counts_ok(c) for c in (o, n)))
    eq("languages split", sorted(r["languages"]), ["python", "typescript"])
    a = r["files"]["src/a.ts"]
    eq("file lines = analyzable lines, unscoped", a["lines"], 40)
    eq("file changedLines from the diff", a["changedLines"], 6)
    eq("whole-file entry (diff None) reports all lines changed",
       r["files"]["tool/b.py"]["changedLines"], 20)
    eq("issues: diff-scoped survivors and no-coverage only",
       [(m["file"], m["line"], m["status"]) for m in r["mutants"]],
       [("src/a.ts", 11, "SURVIVED"), ("src/a.ts", 12, "NO_COVERAGE"), ("tool/b.py", 5, "SURVIVED")])
    eq("run metadata", (r["run"]["runId"], r["run"]["round"], r["run"]["agentVersion"]),
       ("r1", 1, "9.9"))

    # suppression is applied to both scopes, counted separately
    r2 = _build([_m("SURVIVED", 11), _m("KILLED", 10)], {"src/a.ts": {10, 11}},
                arid_map={"src/a.ts": {11}})
    eq("arid mutant suppressed from total", r2["summary"]["overall"]["total"], 1)
    eq("suppressed counted separately", r2["summary"]["overall"]["suppressed"], 1)

    # PIT reports bare basenames: resolved only when unambiguous
    pit = _m("SURVIVED", 3, file="", language="java", source_file="Calc.java")
    r3 = _build([pit], {}, target_files=["a/Calc.java"], productive={"a/Calc.java": [1, 2, 3]})
    eq("basename resolved to the repo path", list(r3["files"]), ["a/Calc.java"])
    r4 = _build([pit], {}, target_files=["a/Calc.java", "b/Calc.java"])
    eq("ambiguous basename not guessed (no file entry, no issue)",
       (list(r4["files"]), r4["mutants"]), ([], []))
    eq("...but still counted in the totals", r4["summary"]["overall"]["total"], 1)

    # per-test data only when the engine supplied it
    r5 = _build([_m("KILLED", 10, tests_run=3, killing_test="t1"),
                 _m("SURVIVED", 11, tests_run=5)], {})
    eq("tests executed summed", r5["summary"]["overall"]["testsExecuted"], 8)
    eq("test kills = killed mutants with a killing test", r5["summary"]["overall"]["testKills"], 1)

    # round 1's score and the whole run's time: optional, one decimal / whole seconds, never guessed
    check("scoreBefore / durationSec absent when not given",
          "scoreBefore" not in r["run"] and "durationSec" not in r["run"])
    r6 = _build([_m("KILLED", 10)], {}, score_before=73.456, duration_sec=12.6)
    eq("scoreBefore rounded to one decimal", r6["run"]["scoreBefore"], 73.5)
    eq("durationSec in whole seconds", r6["run"]["durationSec"], 13)
    r7 = _build([_m("KILLED", 10)], {}, score_before=0, duration_sec=0)
    eq("a real 0% / 0 s is kept, not treated as missing", (r7["run"]["scoreBefore"], r7["run"]["durationSec"]), (0.0, 0))
    for label, bad in (("None", None), ("NaN", float("nan")), ("above 100", 100.5), ("negative", -1),
                       ("bool", True), ("string", "80")):
        check(f"scoreBefore {label} is left out",
              "scoreBefore" not in _build([_m("KILLED", 10)], {}, score_before=bad)["run"])
    for label, bad in (("None", None), ("NaN", float("nan")), ("infinity", float("inf")), ("negative", -5),
                       ("bool", True), ("string", "9")):
        check(f"durationSec {label} is left out",
              "durationSec" not in _build([_m("KILLED", 10)], {}, duration_sec=bad)["run"])

    schema = json.loads((_SKILLS / "run-mutation-workflow" / "schema" /
                         "mutation-report.schema.json").read_text(encoding="utf-8"))
    for label, rep in (("rich", r), ("suppressed", r2), ("per-test", r5), ("before/after", r6)):
        errs = _validate(rep, schema, schema)
        check(f"{label} report validates against the vendored schema", not errs, "; ".join(errs[:3]))
    bad = dict(r)
    bad["mutants"] = [{"file": "x", "line": 1, "status": "KILLED"}]
    check("schema rejects a KILLED mutant in mutants[]", _validate(bad, schema, schema) != [])
    over = json.loads(json.dumps(r6))
    over["run"]["scoreBefore"] = 101
    check("schema rejects a scoreBefore above 100", _validate(over, schema, schema) != [])

    # write + wire
    proj = tmp / "report_proj"
    proj.mkdir()
    (proj / "sonar-project.properties").write_text("sonar.projectKey=demo\n", encoding="utf-8")
    path = sonar_report.write_report(proj, proj / "mutation-reports" / "r1", r)
    eq("stable path", path, proj / ".sonar" / "mutation-report.json")
    check("archive copy written", (proj / "mutation-reports" / "r1" / "sonar" /
                                   "mutation-report.json").exists())
    eq("property added", sonar_report.wire_properties(proj), "added")
    eq("property idempotent", sonar_report.wire_properties(proj), "present")
    (proj / "sonar-project.properties").write_text(
        "sonar.projectKey=demo\nsonar.mutation.reportPath=old.json\n", encoding="utf-8")
    eq("stale property corrected", sonar_report.wire_properties(proj), "updated")
    check("exactly one property line",
          (proj / "sonar-project.properties").read_text(encoding="utf-8")
          .count("sonar.mutation.reportPath") == 1)
    eq("no properties file", sonar_report.wire_properties(tmp), "no-properties-file")


def test_per_test_capture(tmp):
    print("\n[13] engine parsers capture per-test data (PIT, Stryker)")
    xml = tmp / "pit-tests.xml"
    xml.write_text("""<?xml version="1.0"?><mutations>
 <mutation detected='true' status='KILLED' numberOfTestsRun='4'><sourceFile>A.java</sourceFile>
  <mutatedClass>a.A</mutatedClass><mutatedMethod>m</mutatedMethod><lineNumber>1</lineNumber>
  <mutator>x.MathMutator</mutator><description>d</description><index>1</index>
  <killingTest>a.ATest.testX</killingTest></mutation>
 <mutation detected='false' status='SURVIVED'><sourceFile>A.java</sourceFile>
  <mutatedClass>a.A</mutatedClass><mutatedMethod>m</mutatedMethod><lineNumber>2</lineNumber>
  <mutator>x.MathMutator</mutator><description>d</description><index>2</index></mutation>
</mutations>""", encoding="utf-8")
    pm = pit_parser.parse_mutations(str(xml))
    eq("pit: tests run + killing test", (pm[0]["tests_run"], pm[0]["killing_test"]), (4, "a.ATest.testX"))
    eq("pit: absent -> None / empty", (pm[1]["tests_run"], pm[1]["killing_test"]), (None, ""))
    sj = tmp / "stryker-tests.json"
    sj.write_text(json.dumps({"files": {"src/a.ts": {"mutants": [
        {"id": "1", "mutatorName": "Eq", "status": "Killed", "killedBy": ["t7"], "testsCompleted": 3,
         "location": {"start": {"line": 4}}},
        {"id": "2", "mutatorName": "Eq", "status": "Survived",
         "location": {"start": {"line": 5}}}]}}}), encoding="utf-8")
    sm = runners._parse_stryker_json(str(sj), str(tmp))
    eq("stryker: tests completed + killedBy", (sm[0]["tests_run"], sm[0]["killing_test"]), (3, "t7"))
    eq("stryker: absent -> None / empty", (sm[1]["tests_run"], sm[1]["killing_test"]), (None, ""))


# A fake SonarQube + sonar-scanner, so the upload step runs end to end offline.

_FAKE_SCANNER = '''#!{python}
import os, sys, json
from pathlib import Path
Path(os.environ["FAKE_ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]))
pf = Path("sonar-project.properties")
props = (pf.read_text() if pf.exists() else "") + " ".join(sys.argv[1:])
if os.environ.get("FAKE_SCAN_FAIL") == "1":
    print("ERROR: boom"); sys.exit(3)
Path(".scannerwork").mkdir(exist_ok=True)
Path(".scannerwork/report-task.txt").write_text(
    "ceTaskUrl=" + os.environ["FAKE_SONAR"] + "/api/ce/task?id=T1\\n"
    "dashboardUrl=" + os.environ["FAKE_SONAR"] + "/dashboard?id=demo\\n")
if os.environ.get("FAKE_PUBLISH", "1") == "1" and "sonar.mutation.reportPath" in props:
    print("INFO  Mutation Report: published mutation_score=50.0 (1 files, 1 issues)")
print("ANALYSIS SUCCESSFUL")
'''


class _FakeSonar:
    def __init__(self, plugins=("mutationreport",), token="tok123", gate="ERROR"):
        import http.server
        import threading
        outer = self
        outer.plugins, outer.token, outer.gate = plugins, token, gate

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401); self.end_headers(); return
                if self.path.startswith("/api/plugins/installed"):
                    body = {"plugins": [{"key": k} for k in outer.plugins]}
                elif self.path.startswith("/api/ce/task"):
                    body = {"task": {"status": "SUCCESS", "analysisId": "A1"}}
                elif self.path.startswith("/api/qualitygates/project_status"):
                    body = {"projectStatus": {"status": outer.gate, "conditions": [
                        {"status": "ERROR", "metricKey": "new_mutation_score",
                         "actualValue": "50", "errorThreshold": "80"}]}}
                else:
                    self.send_response(404); self.end_headers(); return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _sonar_env(tmp, fake, token="tok123", **extra):
    bindir = tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    scanner = bindir / "sonar-scanner"
    scanner.write_text(_FAKE_SCANNER.format(python=sys.executable), encoding="utf-8")
    scanner.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env["FAKE_SONAR"] = fake.url
    env["FAKE_ARGV_LOG"] = str(tmp / "argv.json")
    env.pop("SONAR_TOKEN", None)
    env.pop("SONAR_HOST_URL", None)
    if token:
        env["SONAR_TOKEN"] = token
    env.update(extra)
    return env


def _write_report_proj(root, props):
    root.mkdir(parents=True, exist_ok=True)
    (root / "sonar-project.properties").write_text(props, encoding="utf-8")
    (root / ".sonar").mkdir(exist_ok=True)
    (root / ".sonar" / "mutation-report.json").write_text('{"schemaVersion":1,"summary":{"overall":{}}}')


def test_sonar_circuit_breaker(tmp):
    print("\n[14b] sonar_upload: circuit breaker around the Sonar HTTP calls")
    import io
    import unittest.mock as mock
    import urllib.error
    import sonar_upload as su

    now = [0.0]
    br = su.CircuitBreaker(threshold=3, reset_s=30, clock=lambda: now[0])
    calls = []

    def down():
        calls.append(1)
        raise OSError("down")

    def http(code):
        def fn():
            calls.append(1)
            raise urllib.error.HTTPError("http://sonar/x", code, "err", {}, io.BytesIO(b""))
        return fn

    def attempt(fn):
        try:
            return br.call(fn)
        except OSError as exc:
            return exc

    for _ in range(3):
        attempt(down)
    eq("trips after 3 consecutive failures", len(calls), 3)
    out = attempt(down)
    check("open: fails fast with CircuitOpenError, no call made",
          isinstance(out, su.CircuitOpenError) and len(calls) == 3, out)
    now[0] = 31
    eq("half-open after the reset window: one trial call goes through", attempt(lambda: 5), 5)
    attempt(down)
    check("a success closed it: one new failure does not re-open it",
          not isinstance(attempt(lambda: 7), su.CircuitOpenError))

    br.reset()
    calls.clear()
    for _ in range(6):
        attempt(http(404))
    check("HTTP 4xx (Sonar answered) never trips the breaker", len(calls) == 6)
    for _ in range(3):
        attempt(http(503))
    check("HTTP 5xx counts as a dependency failure",
          isinstance(attempt(down), su.CircuitOpenError))

    br.reset()
    attempt(down)
    attempt(down)
    attempt(lambda: 1)
    attempt(down)
    attempt(down)
    check("a success resets the failure streak",
          not isinstance(attempt(lambda: 2), su.CircuitOpenError))

    # The analysis-polling loop rides out a blip, and gives up once the breaker opens.
    proj = tmp / "breaker_proj"
    (proj / ".scannerwork").mkdir(parents=True)
    (proj / ".scannerwork" / "report-task.txt").write_text(
        "ceTaskUrl=http://sonar/api/ce/task?id=1\ndashboardUrl=http://sonar/dashboard\n", encoding="utf-8")
    success = {"task": {"status": "SUCCESS", "analysisId": "a1"}}

    def scripted(seq):
        items = list(seq)

        def fetch(url, token, timeout):
            item = items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return fetch

    with mock.patch.object(su.time, "sleep", lambda s: None):
        su.SONAR_BREAKER.reset()
        with mock.patch.object(su, "_fetch_json", scripted([OSError("blip"), OSError("blip"), success])):
            task, err = su._wait_for_task(proj, {}, "http://sonar", "t", 60)
        check("polling survives two transient failures", err is None and task["analysisId"] == "a1", err)

        su.SONAR_BREAKER.reset()
        with mock.patch.object(su, "_fetch_json", scripted([OSError("down")] * 6)):
            task, err = su._wait_for_task(proj, {}, "http://sonar", "t", 60)
        check("polling gives up once the breaker opens",
              task is None and err["stage"] == "analysis" and "circuit breaker open" in err["message"], err)

        su.SONAR_BREAKER.reset()
        denied = urllib.error.HTTPError("http://sonar/x", 403, "forbidden", {}, io.BytesIO(b""))
        with mock.patch.object(su, "_fetch_json", scripted([denied])):
            task, err = su._wait_for_task(proj, {}, "http://sonar", "t", 60)
        check("polling stops at once on a 4xx", task is None and err["stage"] == "analysis", err)
    su.SONAR_BREAKER.reset()


def test_sonar_upload(tmp):
    print("\n[14] sonar_upload: target detection, token/plugin/scanner checks, scan, gate")
    import sonar_upload
    fake = _FakeSonar()
    try:
        proj = tmp / "up1"
        props = f"sonar.projectKey=demo\nsonar.host.url={fake.url}\nsonar.sources=.\n"
        _write_report_proj(proj, props)

        t = sonar_upload.detect_target(proj, use_pr=False)
        eq("target from the properties file", (t["host"], t["project"], t["pr"]), (fake.url, "demo", None))
        _write_report_proj(tmp / "up_env", "sonar.projectKey=demo\n")
        old = os.environ.get("SONAR_HOST_URL")
        os.environ["SONAR_HOST_URL"] = "http://env.example/"
        try:
            eq("host falls back to SONAR_HOST_URL (trailing slash trimmed)",
               sonar_upload.detect_target(tmp / "up_env", False)["host"], "http://env.example")
        finally:
            os.environ.pop("SONAR_HOST_URL", None)
            if old:
                os.environ["SONAR_HOST_URL"] = old
        check("no host anywhere -> error", "error" in sonar_upload.detect_target(tmp / "up_env", False))
        _write_report_proj(tmp / "up_nokey", f"sonar.host.url={fake.url}\n")
        check("no project key -> error", "error" in sonar_upload.detect_target(tmp / "up_nokey", False))
        check("no properties file and nothing else -> error", "error" in sonar_upload.detect_target(tmp / "nowhere", False))
        err = sonar_upload.detect_target(tmp / "nowhere", False)["error"]
        check("the error names the ways out: flags, environment, --skip-sonar",
              all(w in err for w in ("--sonar-project-key", "SONAR_PROJECT_KEY", "--skip-sonar")), err)

        # no sonar-project.properties (Sonar configured in the Gradle build): flags / environment
        t = sonar_upload.detect_target(tmp / "nowhere", False, project_key="k", host="http://h/")
        eq("flags alone are enough", (t.get("project"), t.get("host"), t.get("has_file")), ("k", "http://h", False))
        os.environ["SONAR_PROJECT_KEY"], os.environ["SONAR_HOST_URL"] = "envkey", "http://env.example"
        try:
            t = sonar_upload.detect_target(tmp / "nowhere", False)
            eq("environment alone is enough", (t["project"], t["host"]), ("envkey", "http://env.example"))
            t = sonar_upload.detect_target(proj, False)
            eq("the properties file wins over the environment", (t["project"], t["host"]), ("demo", fake.url))
            t = sonar_upload.detect_target(proj, False, project_key="flagkey", host="http://flag.example")
            eq("a flag wins over the file", (t["project"], t["host"]), ("flagkey", "http://flag.example"))
        finally:
            os.environ.pop("SONAR_PROJECT_KEY", None)
            os.environ.pop("SONAR_HOST_URL", None)
        _write_report_proj(tmp / "up_partial", "sonar.sources=.\n")
        t = sonar_upload.detect_target(tmp / "up_partial", False, project_key="k", host="http://h")
        eq("a properties file without key or host is completed by flags", (t.get("project"), t.get("host")),
           ("k", "http://h"))

        # failures, each with a distinct stage
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake, token=""))
        eq("missing token", (r["ok"], r["stage"]), (False, "token-missing"))
        check("token-missing tells the user how to fix it", "export" in r["message"])
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake, token="wrong"))
        eq("rejected token", r["stage"], "token-rejected")
        fake.plugins = ()
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake))
        eq("plugin not installed", r["stage"], "plugin-missing")
        fake.plugins = ("mutationreport",)
        r = sonar_upload.upload(tmp / "empty_proj", use_pr=False, env=_sonar_env(tmp, fake))
        eq("no report on disk", r["stage"], "no-report")
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake, FAKE_SCAN_FAIL="1"))
        eq("scanner failure", r["stage"], "scan-failed")
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake, FAKE_PUBLISH="0"))
        eq("scan ok but plugin did not publish", r["stage"], "report-not-read")

        # success
        r = sonar_upload.upload(proj, use_pr=False, env=_sonar_env(tmp, fake))
        check("upload succeeds end to end", r["ok"], str(r))
        eq("quality gate read and returned, not folded in", (r["qualityGate"], len(r["qualityGateFailed"])),
           ("ERROR", 1))
        argv = json.loads((tmp / "argv.json").read_text(encoding="utf-8"))
        check("token never in the scanner's argv", not any("tok123" in a for a in argv), str(argv))
        check("host and project passed", f"-Dsonar.projectKey=demo" in argv)
        check("report property wired into sonar-project.properties",
              "sonar.mutation.reportPath=.sonar/mutation-report.json"
              in (proj / "sonar-project.properties").read_text(encoding="utf-8"))

        # a repo with no sonar-project.properties: the report path goes to the scanner as an argument
        nofile = tmp / "up_nofile"
        (nofile / ".git").mkdir(parents=True)
        (nofile / ".sonar").mkdir()
        (nofile / ".sonar" / "mutation-report.json").write_text('{"schemaVersion":1,"summary":{"overall":{}}}')
        r = sonar_upload.upload(nofile, use_pr=False, env=_sonar_env(tmp, fake),
                                project_key="gradle-proj", host=fake.url)
        check("no properties file: upload succeeds with --sonar-project-key / --sonar-host", r["ok"], str(r))
        argv = json.loads((tmp / "argv.json").read_text(encoding="utf-8"))
        check("project key, host and the report path are scanner arguments",
              "-Dsonar.projectKey=gradle-proj" in argv and f"-Dsonar.host.url={fake.url}" in argv
              and "-Dsonar.mutation.reportPath=.sonar/mutation-report.json" in argv, str(argv))
        check("no properties file is created", not (nofile / "sonar-project.properties").exists())
        r = sonar_upload.upload(nofile, use_pr=False, env=_sonar_env(tmp, fake))
        eq("no properties file and no flags: still no-sonar-config", r["stage"], "no-sonar-config")

        # coverage warning
        warns = sonar_upload.coverage_warnings(
            proj, {"sonar.javascript.lcov.reportPaths": "coverage/lcov.info"})
        eq("missing coverage input flagged", len(warns), 1)
        (proj / "coverage").mkdir()
        (proj / "coverage" / "lcov.info").write_text("x")
        eq("present coverage input not flagged", sonar_upload.coverage_warnings(
            proj, {"sonar.javascript.lcov.reportPaths": "coverage/lcov.info"}), [])

        # monorepo: sonar-project.properties at the repo root, the package (and its report) below it
        mono = tmp / "mono_up"
        pkg = mono / "svc"
        (mono / ".git").mkdir(parents=True)
        (mono / "sonar-project.properties").write_text(
            f"sonar.projectKey=demo\nsonar.host.url={fake.url}\nsonar.sources=svc\n", encoding="utf-8")
        (pkg / ".sonar").mkdir(parents=True)
        (pkg / ".sonar" / "mutation-report.json").write_text('{"schemaVersion":1,"summary":{"overall":{}}}')
        r = sonar_upload.upload(pkg, use_pr=False, env=_sonar_env(tmp, fake))
        check("monorepo: upload succeeds with the properties file above the package", r["ok"], str(r))
        check("monorepo: the scanner ran from the properties dir (repo root)",
              (mono / ".scannerwork" / "report-task.txt").exists()
              and not (pkg / ".scannerwork").exists())
        check("monorepo: report path wired relative to the repo root",
              "sonar.mutation.reportPath=svc/.sonar/mutation-report.json"
              in (mono / "sonar-project.properties").read_text(encoding="utf-8"))
        check("monorepo: no properties file created inside the package",
              not (pkg / "sonar-project.properties").exists())
    finally:
        fake.close()


def _sonar_run_repo(tmp, name, fake_url, engine=None):
    repo = tmp / name
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "calc.py").write_text(
        "def add(a, b):\n    if a <= 0:\n        return b\n    return a + b\n", encoding="utf-8")
    (repo / "app" / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    (repo / "sonar-project.properties").write_text(
        f"sonar.projectKey=demo\nsonar.host.url={fake_url}\nsonar.sources=app\n", encoding="utf-8")
    skills = repo / ".claude" / "skills"
    shutil.copytree(_SKILLS / "run-mutation-workflow", skills / "run-mutation-workflow")
    scripts = skills / "run-mutation-workflow" / "scripts"
    (scripts / "mutation_runners.py").write_text(engine or _STUB_ENGINE, encoding="utf-8")
    return repo, scripts / "run.py"


def _run(run_py, repo, env, *extra):
    return subprocess.run(
        [sys.executable, str(run_py), "--skip-engine-check", "--threshold", "80", *extra],
        cwd=str(repo), capture_output=True, text=True, timeout=180, check=False,
        encoding="utf-8", errors="replace", env=env)


def test_sonar_run_integration(tmp):
    print("\n[15] run.py Step 7: report written, uploaded, exit codes")
    fake = _FakeSonar()
    try:
        repo, run_py = _sonar_run_repo(tmp, "run_ok", fake.url)
        env = _sonar_env(tmp, fake)
        p = _run(run_py, repo, env, "--file", "app/calc.py", "--run-id", "s1")
        out = p.stdout + p.stderr
        eq("gate FAIL (no --remediate) + upload ok keeps exit 2", p.returncode, 2)
        report = repo / ".sonar" / "mutation-report.json"
        check("stable report written", report.exists(), out[-1500:])
        rep = json.loads(report.read_text(encoding="utf-8"))
        eq("report carries the final verdict and round", (rep["run"]["verdict"], rep["run"]["round"]),
           ("FAIL", 1))
        check("survivor became an issue entry", len(rep["mutants"]) == 1, str(rep["mutants"]))
        gate = json.loads((repo / "mutation-reports" / "s1" / "gate.json").read_text(encoding="utf-8"))
        check("gate.json records the upload", gate["sonarUpload"]["ok"] is True and gate["incomplete"] is False)
        eq("verdict untouched by the upload", gate["verdict"], "FAIL")
        eq("no remediation: scoreBefore is the final score", rep["run"].get("scoreBefore"), gate["score"])
        check("durationSec is whole seconds", isinstance(rep["run"].get("durationSec"), int)
              and 0 <= rep["run"]["durationSec"] < 120, str(rep["run"]))
        check("quality gate printed", "Sonar quality gate: ERROR" in out, out[-1500:])
        check("host/project/branch printed before the scan", "Sonar target" in out)

        # missing token -> exit 6, verdict preserved, instruction shown
        repo2, run_py2 = _sonar_run_repo(tmp, "run_notoken", fake.url)
        p = _run(run_py2, repo2, _sonar_env(tmp, fake, token=""), "--file", "app/calc.py", "--run-id", "s2")
        eq("missing token -> exit 6", p.returncode, 6)
        check("tells the user to export SONAR_TOKEN", "SONAR_TOKEN is not set" in p.stdout, p.stdout[-1200:])
        g2 = json.loads((repo2 / "mutation-reports" / "s2" / "gate.json").read_text(encoding="utf-8"))
        check("run marked incomplete, verdict preserved",
              g2["incomplete"] is True and g2["verdict"] == "FAIL" and g2["sonarUpload"]["stage"] == "token-missing")
        check("report still written for a later upload", (repo2 / ".sonar" / "mutation-report.json").exists())

        # explicit opt-out
        repo3, run_py3 = _sonar_run_repo(tmp, "run_skip", fake.url)
        p = _run(run_py3, repo3, _sonar_env(tmp, fake, token=""), "--file", "app/calc.py",
                 "--run-id", "s3", "--skip-sonar")
        eq("--skip-sonar keeps the gate's own exit code", p.returncode, 2)
        check("--skip-sonar writes no report", not (repo3 / ".sonar").exists())

        # remediate: round 1 FAIL is not final -> no upload yet
        repo4, run_py4 = _sonar_run_repo(tmp, "run_remed", fake.url)
        p = _run(run_py4, repo4, _sonar_env(tmp, fake), "--file", "app/calc.py", "--run-id", "s4",
                 "--remediate")
        eq("remediation hand-off exits 2", p.returncode, 2)
        check("no upload on a non-final round", not (repo4 / ".sonar").exists(), p.stdout[-800:])
        round1 = json.loads((repo4 / "mutation-reports" / "s4" / "gate.json").read_text(encoding="utf-8"))
        # The test-writing round happens between the two runs; pretend round 1 started 5 minutes ago.
        tel_path = repo4 / "mutation-reports" / "s4" / "telemetry.json"
        tel = json.loads(tel_path.read_text(encoding="utf-8"))
        tel["runStartMs"] -= 300_000
        tel_path.write_text(json.dumps(tel), encoding="utf-8")

        # verify round 2 PASS -> final state uploaded
        (repo4 / "app" / "calc.py").write_text(
            "def add(a, b):\n    if a <= 0:\n        return b\n    return a + b\n", encoding="utf-8")
        (run_py4.parent / "mutation_runners.py").write_text(_STUB_ENGINE_ALL_KILLED, encoding="utf-8")
        p = _run(run_py4, repo4, _sonar_env(tmp, fake), "--verify", "--run-id", "s4", "--round", "2",
                 "--tests-added", "1")
        eq("round 2 PASS exits 0 with a successful upload", p.returncode, 0)
        rep = json.loads((repo4 / ".sonar" / "mutation-report.json").read_text(encoding="utf-8"))
        eq("final report is round 2, PASS, no stale survivors",
           (rep["run"]["round"], rep["run"]["verdict"], rep["mutants"]), (2, "PASS", []))
        check("round 2 report carries round 1's score, not its own",
              rep["run"].get("scoreBefore") == round1["score"] and rep["run"]["scoreBefore"] < 100,
              str(rep["run"]))
        check("round 2 report carries the whole run's time (round 1 start to now)",
              300 <= rep["run"].get("durationSec", -1) < 420, str(rep["run"]))
        props = (repo4 / "sonar-project.properties").read_text(encoding="utf-8")
        eq("report property wired exactly once", props.count("sonar.mutation.reportPath"), 1)

        # verify exhausted: manual review (3) is final; upload failure -> 4
        repo5, run_py5 = _sonar_run_repo(tmp, "run_exh", fake.url)
        _run(run_py5, repo5, _sonar_env(tmp, fake, token=""), "--file", "app/calc.py", "--run-id", "s5",
             "--remediate")
        p = _run(run_py5, repo5, _sonar_env(tmp, fake), "--verify", "--run-id", "s5", "--round", "2")
        eq("manual review exit 3, upload ok", p.returncode, 3)
        rep5 = json.loads((repo5 / ".sonar" / "mutation-report.json").read_text(encoding="utf-8"))
        eq("final report verdict MANUAL_REVIEW", rep5["run"]["verdict"], "MANUAL_REVIEW")
        g5 = json.loads((repo5 / "mutation-reports" / "s5" / "gate.json").read_text(encoding="utf-8"))
        check("manual review keeps round 1's score and a duration",
              rep5["run"].get("scoreBefore") == g5["scoreBefore"] and "durationSec" in rep5["run"], str(rep5["run"]))
    finally:
        fake.close()


def main() -> int:
    print("=" * 66)
    print("  POLYGLOT MUTATION PIPELINE -- SELF TEST")
    print("=" * 66)
    with tempfile.TemporaryDirectory(prefix="mutsel-") as td:
        tmp = Path(td)
        test_path_classification()
        test_language_mapping()
        test_arid(tmp)
        test_pit_parser(tmp)
        test_stryker_parser(tmp)
        test_test_narrowing(tmp)
        test_mutmut_parser(tmp)
        test_gremlins_parser(tmp)
        test_score_rounding()
        test_suppression_and_gate()
        test_end_to_end(tmp)
        test_no_work_writes_gate(tmp)
        test_final_report(tmp)
        test_duplicate_launch_keeps_latest(tmp)
        test_nested_project_root(tmp)
        test_non_viable_and_sonar_monorepo(tmp)
        test_test_file_lookup(tmp)
        test_module_file_and_dotnet_env(tmp)
        test_gradle(tmp)
        test_java_test_selection(tmp)
        test_changes_spanning_modules(tmp)
        test_dotnet_mutate_spans(tmp)
        test_new_guardrails(tmp)
        test_validate_environment(tmp)
        test_sonar_report_content_and_schema(tmp)
        test_per_test_capture(tmp)
        test_sonar_circuit_breaker(tmp)
        test_sonar_upload(tmp)
        test_sonar_run_integration(tmp)

    failed = [r for r in _RESULTS if not r[1]]
    print("\n" + "=" * 66)
    print(f"  {len(_RESULTS) - len(failed)}/{len(_RESULTS)} checks passed")
    if failed:
        print(f"  {len(failed)} FAILED:")
        for name, _, detail in failed:
            print(f"    - {name}  ({detail})")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
Central language registry -- the single source of truth for everything the
mutation pipeline needs to know that differs per language.

Every step that used to hard-code Java/PIT assumptions asks a `LangProfile`
instead:

    Step 1 (language_detector)      -- which languages are actually present
    Step 2 (changed_files_scanner)  -- which extensions count as source
    Step 3 (arid_filter)            -- which lines are unproductive to mutate
    Step 4 (mutation_runners)       -- which engine binary + how to name a target
    orchestrator (run.py)           -- a file's test path and its tool target id

Supported: java/kotlin (pitest), python (mutmut), typescript + javascript
(stryker), csharp (stryker.net), go (gremlins).

POLYGLOT BY DESIGN
------------------
`detect_languages()` returns EVERY language present in a repo, not one winner.
A marker file alone is not proof of a language: this repo, for instance, ships
a `pom.xml` purely to drive `frontend-maven-plugin` for an Angular app and
contains zero `.java` files. A language therefore counts as present only when a
marker AND at least one non-test source file of that language both exist. That
rule is what stops `pom.xml` from shadowing TypeScript.

`profile_for_file()` maps each individual changed file to its own engine by
extension, so one changeset can drive several engines in a single run.

Pure stdlib. No LLM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# --- directories never worth walking or mutating -----------------------------
# Shared by detection (Step 1) and change scoping (Step 2) so the two agree on
# what counts as project source.
EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "jspm_packages",
    "target", "build", "dist", "out", "bin", "obj",
    "vendor", "third_party",
    ".venv", "venv", "env", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".nyc_output", "htmlcov",
    ".angular", ".next", ".nuxt", ".svelte-kit", ".cache",
    ".stryker-tmp", "StrykerOutput", "pit-output", "mutation-reports",
    "mutants",  # mutmut 3 works on a full copy of the project, tests included
    "coverage", "test-results", "reports", "logs",
    ".idea", ".vscode", ".gradle",
    # the pipeline's own tooling -- never mutate the thing doing the mutating
    ".claude", ".github",
})


# --- arid-line pattern sets, per language ------------------------------------
# A line matching any of these produces a mutant that is equivalent, untestable
# or not worth killing (logging, sleeps, config reads, boilerplate, comments).
# Block comments and docstrings are handled structurally by `arid_lines()`
# below rather than by regex, so a `/* ... */` body whose lines do not start
# with `*` is still recognised.

_ARID = {
    "java": [
        r'\b(log|logger|LOG|LOGGER)\.(trace|debug|info|warn|error|fatal)\s*\(',
        r'\bSystem\.(out|err)\.(print|println|printf)\s*\(',
        r'\bThread\.sleep\s*\(',
        r'\bTimeUnit\.\w+\.sleep\s*\(',
        r'\bawait\w*\.\w*sleep',
        r'@Value\s*\(',
        r'\bgetenv\s*\(',
        r'\bSystem\.getProperty\s*\(',
        r'\.getProperty\s*\(',
        r'\bPreconditions\.check',
        r'^\s*assert\b',
        r'\bAssert\.\w+\s*\(',
        r'public\s+String\s+toString\s*\(\s*\)',
        r'public\s+int\s+hashCode\s*\(\s*\)',
        r'public\s+boolean\s+equals\s*\(',
        r'^\s*(package|import)\s+',
        r'^\s*(//|/\*|\*)',
        r'^\s*$',
        r'^\s*@\w+',
        r'^\s*[{}();]+\s*$',
    ],
    "python": [
        r'\b(log|logger|logging|_log|LOG)\.(trace|debug|info|warning|warn|error|critical|exception|fatal)\s*\(',
        r'\btime\.sleep\s*\(',
        r'\basyncio\.sleep\s*\(',
        r'\bos\.getenv\s*\(',
        r'\bos\.environ\b',
        r'\bgetenv\s*\(',
        r'^\s*assert\b',
        r'\bprint\s*\(',
        r'^\s*(import|from)\s+\w',
        r'def\s+__(repr|str|eq|hash|format)__\s*\(',
        r'^\s*#',
        r'^\s*$',
        r'^\s*@\w+',
        r'^\s*(pass|\.\.\.)\s*$',
        r'^\s*(raise\s+NotImplementedError|else\s*:|try\s*:)\s*$',
    ],
    "typescript": [
        r'\bconsole\.(log|debug|info|warn|error|trace|assert|table|dir)\s*\(',
        r'\b(log|logger|_logger|\$log)\.(trace|debug|info|warn|error|fatal)\s*\(',
        r'\bsetTimeout\s*\(',
        r'\bsetInterval\s*\(',
        r'\bclear(Timeout|Interval)\s*\(',
        r'\bsleep\s*\(',
        r'\bprocess\.env\b',
        r'\bimport\.meta\.env\b',
        r'\bdebugger\b',
        r'\btoString\s*\(\s*\)\s*[:{]',
        # declarations carry no executable behaviour to mutate
        r'^\s*(import|export)\s+(type\s+)?[{*]',
        r'^\s*import\s+[\'"]',
        r'^\s*export\s+(type|interface|enum|declare)\b',
        r'^\s*(interface|type)\s+\w+',
        r'^\s*//',
        r'^\s*/\*',
        r'^\s*\*',
        r'^\s*$',
        # Angular/NestJS decorators and their object-literal bodies
        r'^\s*@\w+',
        r'^\s*(providedIn|selector|templateUrl|styleUrls|standalone|imports|declarations|exports|providers)\s*:',
        r'^\s*[{}()\[\];,]+\s*$',
    ],
    "csharp": [
        r'\b(_?[Ll]og(ger)?|Log|Console|Trace|Debug)\.(LogTrace|LogDebug|LogInformation|'
        r'LogWarning|LogError|LogCritical|WriteLine|Write|TraceInformation)\s*\(',
        r'\bThread\.Sleep\s*\(',
        r'\bTask\.Delay\s*\(',
        r'\bEnvironment\.GetEnvironmentVariable\s*\(',
        r'\b_?[Cc]onfiguration\s*\[',
        r'\.GetSection\s*\(',
        r'\bDebug\.Assert\s*\(',
        r'\bContract\.(Assert|Requires)\s*\(',
        r'public\s+override\s+string\s+ToString\s*\(\s*\)',
        r'public\s+override\s+int\s+GetHashCode\s*\(\s*\)',
        r'public\s+override\s+bool\s+Equals\s*\(',
        r'^\s*(using|namespace)\s+',
        r'^\s*#(if|else|endif|region|endregion|pragma)\b',
        r'^\s*\[\w+',
        r'^\s*(//|/\*|\*)',
        r'^\s*$',
        r'^\s*[{}();]+\s*$',
    ],
    "go": [
        r'\b(log|logger|logrus|zap|slog|klog)\.(Print|Printf|Println|Debug|Debugf|Info|Infof|'
        r'Warn|Warnf|Error|Errorf|Fatal|Fatalf|Panic)\w*\s*\(',
        r'\bfmt\.(Print|Printf|Println)\s*\(',
        r'\btime\.Sleep\s*\(',
        r'\bos\.Getenv\s*\(',
        r'\bos\.LookupEnv\s*\(',
        r'func\s+\(\s*\w+\s+\*?\w+\s*\)\s+String\s*\(\s*\)\s+string',
        r'^\s*(package|import)\s+',
        r'^\s*//',
        r'^\s*$',
        r'^\s*[{}()]+\s*$',
    ],
}
# javascript shares the typescript rule set.
_ARID["javascript"] = _ARID["typescript"]


# Block-comment delimiters per language. `arid_lines()` walks these statefully
# so every line of a multi-line comment or docstring is marked, not just the
# lines that happen to begin with a comment token.
_BLOCK_COMMENTS = {
    "java":       [("/*", "*/")],
    "csharp":     [("/*", "*/")],
    "typescript": [("/*", "*/")],
    "javascript": [("/*", "*/")],
    "go":         [("/*", "*/")],
    "python":     [('"""', '"""'), ("'''", "'''")],
}


def arid_lines(lines, language: str) -> set:
    """Return the 1-based line numbers that are unproductive to mutate.

    Combines the per-language regex set with structural block-comment tracking.
    `lines` is a list of source lines without trailing newlines.
    """
    patterns = _patterns_for(language)
    delims = _BLOCK_COMMENTS.get(language, [])
    arid, open_close = set(), None

    for lineno, line in enumerate(lines, start=1):
        if open_close is not None:
            arid.add(lineno)
            if open_close[1] in line:
                open_close = None
            continue

        if any(p.search(line) for p in patterns):
            arid.add(lineno)

        stripped = line.strip()
        for opener, closer in delims:
            if opener not in stripped:
                continue
            after = stripped.split(opener, 1)[1]
            if closer not in after:
                # unterminated on this line -- everything until the closer is comment
                arid.add(lineno)
                open_close = (opener, closer)
            break

    return arid


def _patterns_for(language: str):
    key = (language or "").lower()
    src = _ARID.get(key) or _ARID["java"]
    compiled = _COMPILED_CACHE.get(key)
    if compiled is None:
        compiled = [re.compile(p) for p in src]
        _COMPILED_CACHE[key] = compiled
    return compiled


_COMPILED_CACHE = {}


# --- test / target markers ---------------------------------------------------
# Matching is anchored on path segments and file-name suffixes rather than bare
# substrings. A bare `"test" in path` check silently discards real source files
# whose names merely CONTAIN the word -- latest-news.ts, Contest.java,
# specialist.service.ts -- and a dropped file is a mutation score that looks
# clean because nothing was measured.

_TEST_DIR_SEGMENTS = frozenset({
    "test", "tests", "__tests__", "__mocks__", "spec", "specs", "testing",
    "e2e", "integration-test", "integrationtest", "fixtures", "mocks",
})

# Deliberately NOT case-insensitive as a whole. The lowercase alternatives must
# stay lowercase-only and the PascalCase ones capitalised, because that is the
# only thing separating `PolicyUtilsTest.java` (a test) from `Contest.java` (a
# source file that merely ends in the letters t-e-s-t). A case-insensitive
# `Test$` swallows Contest, Greatest, Latest and Protest.
_TEST_NAME_RE = re.compile(
    r"""(?x)
      ^test_                                            # test_foo      (python)
    | [._\-](test|tests|spec|specs)$                     # foo.spec / foo_test / foo-spec
    | (?<![A-Z])(Test|Tests|TestCase|TestCases|Spec|Specs|Fixture|Fixtures)$
                                                         # FooTest       (java / c#)
    | [a-z0-9](IT|ITCase)$                               # FooIT         (maven failsafe)
    """
)


def _is_test_path(path: str) -> bool:
    p = Path(path)
    posix = "/" + p.as_posix()
    segments = {s.lower() for s in p.parts[:-1]}
    if segments & _TEST_DIR_SEGMENTS:
        return True
    if "/src/test/" in posix or "/src/it/" in posix:
        return True
    # Strip only the final extension, then test the remaining stem, so
    # `foo.service.spec.ts` reduces to `foo.service.spec` and still matches.
    stem = p.name
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return bool(_TEST_NAME_RE.search(stem))


# Non-source files that share a source extension: build config, type
# declarations, generated code, module/barrel files with no logic to mutate.
_NON_SOURCE_NAME_RE = re.compile(
    r"""(?ix)
    \.d\.ts$
    | \.(config|conf)\.(ts|js|mjs|cjs)$
    | ^(karma|jest|webpack|rollup|vite|babel|eslint|stryker|playwright|
        protractor|cypress|tailwind|postcss|next|nuxt|metro|gulpfile|
        gruntfile|commitlint|lint-staged)\b[.\w-]*\.(ts|js|mjs|cjs)$
    | \.(generated|designer|g)\.(cs|ts|js|java)$
    | (^|[.\-_])(index|barrel|public[-_]api)\.(ts|js)$
    | \.module\.ts$
    | ^(setup|conftest)\.py$
    | ^__init__\.py$
    | ^(migrations?|AssemblyInfo)\.(cs|py)$
    """
)


def _is_non_source(path: str) -> bool:
    return bool(_NON_SOURCE_NAME_RE.search(Path(path).name))


def is_excluded_dir(path: str) -> bool:
    """True when any directory segment of `path` is one we never mutate."""
    return any(part in EXCLUDE_DIRS for part in Path(path).parts[:-1])


# ---- Java --------------------------------------------------------------------
_JAVA_SRC_MARKERS = ("src/main/java/", "src/main/kotlin/")


def _java_target(project_root: str, source_file: str) -> Optional[str]:
    path = Path(source_file).as_posix()
    for marker in _JAVA_SRC_MARKERS:
        if marker in path:
            rel = path.split(marker, 1)[1]
            return rel.rsplit(".", 1)[0].replace("/", ".")
    return None


_JAVA_TEST_SUFFIXES = ("Test", "Tests", "IT", "TestCase", "Spec")


def _nearest_build_dir(project_root: str, source_files, markers: tuple):
    """The directory (posix, relative to the project root; "" for the root itself) holding the
    nearest build file in `markers` above most of `source_files`. None when none is found."""
    root = Path(project_root).resolve()
    counts = {}
    for f in source_files:
        d = (root / f).resolve().parent
        while True:
            if any((d / m).exists() for m in markers):
                try:
                    rel = d.relative_to(root).as_posix()
                except ValueError:
                    rel = ""
                rel = "" if rel == "." else rel
                counts[rel] = counts.get(rel, 0) + 1
                break
            if d == root or d.parent == d:
                break
            d = d.parent
    if not counts:
        return None
    return sorted(counts, key=lambda m: (-counts[m], m))[0]


def maven_module_for(project_root: str, source_files) -> str:
    """The Maven module (path relative to the project root, as `-pl` takes it) holding most of
    `source_files`: the nearest pom.xml above each file. "" when that is the project root itself
    (a single-module build, where `-pl` is an error) or no pom.xml is found."""
    return _nearest_build_dir(project_root, source_files, ("pom.xml",)) or ""


# ---- Gradle -------------------------------------------------------------------
_GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
_GRADLE_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")


def java_build_tool(project_root: str) -> str:
    """"maven" or "gradle" for the Java/Kotlin project at `project_root`. A root pom.xml means
    Maven (also when Gradle files exist next to it, as before Gradle was supported); otherwise
    a Gradle build or settings file means Gradle. Defaults to "maven"."""
    root = Path(project_root)
    if (root / "pom.xml").exists():
        return "maven"
    if any((root / n).exists() for n in _GRADLE_BUILD_FILES + _GRADLE_SETTINGS_FILES):
        return "gradle"
    return "maven"


def gradle_executable(project_root: str) -> Optional[str]:
    """The project's Gradle wrapper (preferred: it pins the Gradle version the build needs),
    else `gradle` on PATH, else None."""
    wrapper = Path(project_root) / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if wrapper.is_file():
        return str(wrapper)
    for candidate in ("gradle", "gradle.bat"):
        exe = shutil.which(candidate)
        if exe:
            return exe
    return None


def gradle_project_for(project_root: str, source_files) -> str:
    """The Gradle project path (":" for the root, ":core:api" for core/api) holding most of
    `source_files`: the nearest build.gradle(.kts) above each file. Assumes the default layout,
    where a project's path follows its directory; settings that remap projectDir are not read
    (pass --module :name for those)."""
    rel = _nearest_build_dir(project_root, source_files, _GRADLE_BUILD_FILES)
    return ":" + rel.replace("/", ":") if rel else ":"


def gradle_project_path(module: str) -> str:
    """A --module value as a Gradle project path: "core/api", "core:api" and ":core:api" all
    give ":core:api"; "" gives the root ":"."""
    m = (module or "").strip().replace("/", ":").strip(":")
    return ":" + m if m else ":"


def build_module_for(project_root: str, source_files) -> str:
    """The Java build module for the engine: a Gradle project path for a Gradle build, else
    the Maven module (see maven_module_for)."""
    if java_build_tool(project_root) == "gradle":
        return gradle_project_for(project_root, source_files)
    return maven_module_for(project_root, source_files)


_JUNIT_PLATFORM_HINTS = ("useJUnitPlatform", "junit-jupiter", "org.junit.jupiter", "io.kotest", "spock-core:2")


def uses_junit_platform(project_root: str, limit: int = 2000) -> bool:
    """True when the tests run on the JUnit Platform (JUnit 5, Kotest, Spock 2), which PIT
    needs pitest-junit5-plugin for. Read from the Gradle build files first (including
    convention plugins in buildSrc), then from the test sources' imports."""
    root = Path(project_root)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in filenames:
            is_build = name.endswith((".gradle", ".gradle.kts")) or name == "libs.versions.toml"
            is_test = name.endswith((".java", ".kt", ".groovy")) and "test" in Path(dirpath).parts
            if not (is_build or is_test):
                continue
            seen += 1
            if seen > limit:
                return False
            try:
                text = (Path(dirpath) / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(h in text for h in _JUNIT_PLATFORM_HINTS):
                return True
    return False


def _java_test(project_root: str, source_file: str) -> Optional[str]:
    parts = list(Path(source_file).parts)
    src = Path(source_file)
    candidates = []
    if "main" in parts:
        parts[parts.index("main")] = "test"
        test_path = Path(*parts)
        candidates = [
            os.path.join(project_root, str(test_path.with_name(test_path.stem + suffix + test_path.suffix)))
            for suffix in _JAVA_TEST_SUFFIXES
        ]
    found = _first_existing(candidates, None)
    if found is None:  # same name in another package, or a layout without src/main
        found = _find_test_by_name(project_root, source_file,
                                   tuple(src.stem + suf + src.suffix for suf in _JAVA_TEST_SUFFIXES))
    if found is not None:
        return found
    return candidates[0] if candidates else None


# ---- path/target helpers shared by the file-path tools -----------------------

def _rel_target(project_root: str, source_file: str) -> Optional[str]:
    """python / typescript / c#: the tool mutates the file by path."""
    if _is_test_path(source_file) or _is_non_source(source_file):
        return None
    return Path(source_file).as_posix()


def _first_existing(candidates, fallback):
    for c in candidates:
        if os.path.exists(c):
            return c
    return fallback


# Directory names that mark a path as test code, in every supported ecosystem.
_TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "unittest", "unittests"}


def _find_test_by_name(project_root: str, source_file: str, names) -> Optional[str]:
    """Fallback after the conventional layouts: an existing test file called one of `names`
    somewhere under a test-like directory (tests/, test/, spec/, __tests__/ ...), at any depth.

    Covers layouts nobody listed (tests/unit-test/<mirrored dirs>/, tests/<pkg>/, a test in a
    different package than its class). Conservative by design: it only answers when exactly one
    candidate is the best by (module prefix shared with the source, then mirrored directory
    names, then living under a unit/ directory); a tie answers None rather than guess, so a
    same-named spec in two places never gets a test appended to the wrong one."""
    names = set(names)
    exts = tuple(sorted({"." + n.rsplit(".", 1)[-1] for n in names}))
    src_parts = Path(source_file).parent.parts
    best, best_key, tie = None, None, False
    for rel in _walk_source(project_root, exts):
        parts = Path(rel).parts
        if parts[-1] not in names or not any(c.lower() in _TEST_DIRS for c in parts[:-1]):
            continue
        prefix = 0
        for a, b in zip(src_parts, parts[:-1]):
            if a != b:
                break
            prefix += 1
        mirrored = 0
        for a, b in zip(reversed(src_parts), reversed(parts[:-1])):
            if a != b:
                break
            mirrored += 1
        unit = int(any("unit" in c.lower() for c in parts[:-1]))
        key = (prefix, mirrored, unit)
        if best_key is None or key > best_key:
            best, best_key, tie = rel, key, False
        elif key == best_key:
            tie = True
    return None if tie or best is None else os.path.join(project_root, best)


def _python_test(project_root: str, source_file: str) -> Optional[str]:
    src = Path(source_file)
    stem, d = src.stem, src.parent
    candidates = [
        os.path.join(project_root, str(d / f"test_{stem}.py")),
        os.path.join(project_root, str(d / f"{stem}_test.py")),
        os.path.join(project_root, str(d / "tests" / f"test_{stem}.py")),
        os.path.join(project_root, str(d.parent / "tests" / f"test_{stem}.py")),
        os.path.join(project_root, "tests", f"test_{stem}.py"),
        os.path.join(project_root, "test", f"test_{stem}.py"),
    ]
    found = _first_existing(candidates, None)
    if found is None:  # tests/unit/test_x.py, tests/<pkg>/test_x.py, ...
        found = _find_test_by_name(project_root, source_file, (f"test_{stem}.py", f"{stem}_test.py"))
    return found or candidates[0]


def _ts_test(project_root: str, source_file: str) -> Optional[str]:
    """Best-guess test file for a TS/JS source: co-located spec / __tests__, else a top-level tests/ or test/ mirroring src/."""
    src = Path(source_file)
    stem, d, ext = src.stem, src.parent, src.suffix
    parts = d.parts
    mirrored = Path(*parts[1:]) if parts and parts[0] == "src" else d
    candidates = [
        os.path.join(project_root, str(d / f"{stem}.spec{ext}")),
        os.path.join(project_root, str(d / f"{stem}.test{ext}")),
        os.path.join(project_root, str(d / "__tests__" / f"{stem}.spec{ext}")),
        os.path.join(project_root, str(d / "__tests__" / f"{stem}.test{ext}")),
        os.path.join(project_root, str(d / "__tests__" / f"{stem}{ext}")),
        os.path.join(project_root, "tests", str(mirrored / f"{stem}.test{ext}")),
        os.path.join(project_root, "tests", str(mirrored / f"{stem}.spec{ext}")),
        os.path.join(project_root, "test", str(mirrored / f"{stem}.test{ext}")),
        os.path.join(project_root, "test", str(mirrored / f"{stem}.spec{ext}")),
    ]
    # Flat layouts: tests/unit-test/<stem>.test.ts (and unit/, unit-tests/).
    for sub in ("unit-test", "unit-tests", "unit"):
        for tdir in ("tests", "test"):
            candidates.append(os.path.join(project_root, tdir, sub, f"{stem}.test{ext}"))
            candidates.append(os.path.join(project_root, tdir, sub, f"{stem}.spec{ext}"))
    found = _first_existing(candidates, None)
    if found is None:  # tests/unit-test/<mirrored dirs>/x.test.ts, spec/x.spec.ts, tests/<app dir>/..., ...
        found = _find_test_by_name(project_root, source_file, (f"{stem}.test{ext}", f"{stem}.spec{ext}"))
    return found or candidates[0]


def _go_test(project_root: str, source_file: str) -> Optional[str]:
    src = Path(source_file)
    return os.path.join(project_root, str(src.with_name(src.stem + "_test.go")))


def _go_target(project_root: str, source_file: str) -> Optional[str]:
    """gremlins mutates packages, not files: return './<dir>'."""
    if _is_test_path(source_file):
        return None
    d = Path(source_file).parent.as_posix()
    return "./" + d if d and d != "." else "./..."


def _csharp_test(project_root: str, source_file: str) -> Optional[str]:
    """C# test projects live apart from source. Match <Name>{Tests|Test|Spec}.cs
    but also the common versioned convention where a token sits between the
    class name and the suffix -- AppConfiguration.cs -> AppConfigurationV1Tests.cs.

    The token is only accepted when it is empty or version-like (``V1``/``v2``),
    so AppConfiguration.cs never mis-binds to AppConfigurationBinderTests.cs.
    When several candidates match (a V1 and a V2 test carrying the same stem),
    the one whose directory best mirrors the source's is chosen, so the V1
    source maps to the V1 test rather than the V2 one."""
    stem_l = Path(source_file).stem.lower()
    suffixes = ("tests", "test", "spec")
    src_dir_parts = [p.lower() for p in Path(source_file).parent.parts]

    def _token_ok(token: str) -> bool:
        return token == "" or re.fullmatch(r"v\d+", token) is not None

    def _dir_affinity(cs: str) -> int:
        test_parts = [p.lower() for p in Path(cs).parent.parts]
        score = 0
        for a, b in zip(reversed(src_dir_parts), reversed(test_parts)):
            if a != b:
                break
            score += 1
        return score

    best, best_score = None, -1
    for cs in _walk_source(project_root, (".cs",)):
        base = Path(cs).name.lower()
        if not base.endswith(".cs"):
            continue
        base = base[:-3]
        if not base.startswith(stem_l):
            continue
        rest = base[len(stem_l):]
        if not any(rest.endswith(suf) and _token_ok(rest[: -len(suf)]) for suf in suffixes):
            continue
        score = _dir_affinity(cs)
        if score > best_score:
            best, best_score = cs, score
    if best is not None:
        return os.path.join(project_root, best)
    return os.path.join(project_root, str(Path(source_file).with_name(Path(source_file).stem + "Tests.cs")))


# --- filesystem walking -------------------------------------------------------

def _walk_source(project_root: str, exts: tuple, limit: int = 20000):
    """Yield repo-relative paths with any of `exts`, skipping EXCLUDE_DIRS.

    Bounded so detection on a huge monorepo cannot stall the pipeline; the
    bound is only ever hit by repos far larger than a mutation run would target.
    """
    root = Path(project_root)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".stryker")]
        for name in filenames:
            if name.endswith(exts):
                yield Path(dirpath, name).relative_to(root).as_posix()
                seen += 1
                if seen >= limit:
                    return


# --- engine resolution --------------------------------------------------------
# Detection (Step 1) and execution (Step 4) resolve the engine binary through
# the SAME function, so "installed: True" can never disagree with what actually
# runs. Local project installs are preferred over global ones and over `npx`,
# which may otherwise reach for the network mid-run.

# Which filename forms are actually executable by subprocess on this platform.
# On Windows `node_modules/.bin/stryker` (no extension) is a POSIX SHELL SCRIPT:
# handing it to CreateProcess fails with "WinError 193: %1 is not a valid Win32
# application". The runnable sibling is `stryker.cmd`. On POSIX the reverse holds
# -- the extensionless file is the real one. `.ps1` is never directly runnable.
_EXE_SUFFIXES = (".cmd", ".bat", ".exe") if os.name == "nt" else ("", ".sh")


def _exe_candidates(project_root: str, *names):
    """Local node_modules/.bin and venv script dirs first, then PATH."""
    root = Path(project_root)
    # node_modules/.bin in the project and every ancestor (Node's lookup order); a git worktree relies on the main checkout's.
    local_dirs = [d / "node_modules" / ".bin" for d in (root, *root.parents)] + [
        root / ".venv" / "Scripts", root / ".venv" / "bin",
        root / "venv" / "Scripts", root / "venv" / "bin",
    ]
    for name in names:
        for d in local_dirs:
            for suffix in _EXE_SUFFIXES:
                cand = d / (name + suffix)
                if cand.is_file():
                    yield str(cand)
        found = shutil.which(name)
        if found:
            yield found


def _probe(cmd, cwd=None, timeout=60) -> bool:
    """True if the command runs and exits 0. Never raises."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stryker_cmd(project_root: str):
    for exe in _exe_candidates(project_root, "stryker"):
        return [exe]
    # Fall back to the package's own entry script -- present whenever
    # @stryker-mutator/core is a dependency even if .bin symlinks are missing.
    for base in (Path(project_root), *Path(project_root).parents):
        entry = base / "node_modules" / "@stryker-mutator" / "core" / "bin" / "stryker.js"
        if entry.is_file():
            node = shutil.which("node")
            if node:
                return [node, str(entry)]
    npx = shutil.which("npx")
    return [npx, "stryker"] if npx else None


def _dotnet_tools_dirs():
    """Well-known locations `dotnet tool install -g` drops shims into, so the
    engine resolves even when the current process PATH hasn't been refreshed to
    include the global tools dir (a fresh shell after install, or a wizard child
    process that inherited a stale PATH)."""
    dirs = []
    for env in ("DOTNET_TOOLS_PATH",):
        val = os.environ.get(env)
        if val:
            dirs.append(Path(val))
    home = os.environ.get("DOTNET_CLI_HOME") or os.path.expanduser("~")
    if home:
        dirs.append(Path(home) / ".dotnet" / "tools")
    return dirs


def _dotnet_global_tool(name: str):
    """A dotnet global tool shim by name, resolved via PATH first, then the
    known global-tools directories. Returns an absolute path or None."""
    exe = shutil.which(name)
    if exe:
        return exe
    for d in _dotnet_tools_dirs():
        for suffix in _EXE_SUFFIXES:
            cand = d / (name + suffix)
            if cand.is_file():
                return str(cand)
    return None


def _stryker_dotnet_cmd(project_root: str):
    exe = _dotnet_global_tool("dotnet-stryker")
    if exe:
        return [exe]
    dotnet = shutil.which("dotnet")
    return [dotnet, "stryker"] if dotnet else None


def _mutmut_cmd(project_root: str):
    for exe in _exe_candidates(project_root, "mutmut"):
        return [exe]
    python = shutil.which("python") or shutil.which("py")
    if python and _probe([python, "-c", "import mutmut"], timeout=30):
        return [python, "-m", "mutmut"]
    return None


def _gremlins_cmd(project_root: str):
    for exe in _exe_candidates(project_root, "gremlins"):
        return [exe]
    return None


def _pitest_cmd(project_root: str):
    """PIT runs through the build tool. Maven: mvn plus a pom that declares the plugin (both
    halves are required -- a Maven on PATH with no pitest declaration cannot mutate anything).
    Gradle: the wrapper or gradle; the agent's init script applies the plugin itself."""
    if java_build_tool(project_root) == "gradle":
        exe = gradle_executable(project_root)
        return [exe] if exe else None
    for candidate in ("mvn", "mvn.cmd", "mvn.bat"):
        exe = shutil.which(candidate)
        if exe:
            return [exe]
    return None


def _pitest_installed(project_root: str) -> bool:
    if _pitest_cmd(project_root) is None:
        return False
    if java_build_tool(project_root) == "gradle":
        return True  # pitest-init.gradle brings the plugin; nothing to declare
    root = Path(project_root)
    for pom in [root / "pom.xml", *root.glob("*/pom.xml")]:
        try:
            if "pitest" in pom.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _stryker_installed(project_root: str) -> bool:
    cmd = _stryker_cmd(project_root)
    if cmd is None:
        return False
    # A local install is proof enough; only shell out for the ambiguous npx case.
    if "npx" not in Path(cmd[0]).name.lower():
        return True
    return _probe(cmd + ["--version"], cwd=project_root, timeout=120)


def _simple_installed(cmd_fn):
    def check(project_root: str) -> bool:
        return cmd_fn(project_root) is not None
    return check


# --- the profile -------------------------------------------------------------

@dataclass
class LangProfile:
    language: str
    tool: str
    markers: tuple                    # marker files ("pom.xml") or globs ("*.sln")
    source_exts: tuple                # extensions that count as mutatable source
    default_module: Optional[str]     # Maven module etc.; None when not applicable
    test_patterns: tuple = ()         # human-readable, surfaced in Step 1 output
    install_hint: str = ""
    _arid_src: list = field(repr=False, default_factory=list)
    _target: Callable = field(repr=False, default=None)
    _test: Callable = field(repr=False, default=None)
    _engine_cmd: Callable = field(repr=False, default=None)
    _installed: Callable = field(repr=False, default=None)

    _arid_compiled: list = field(default=None, repr=False)

    @property
    def marker(self) -> str:
        """Back-compat: the primary marker, when callers want just one."""
        return self.markers[0]

    @property
    def arid_patterns(self):
        if self._arid_compiled is None:
            self._arid_compiled = [re.compile(p) for p in self._arid_src]
        return self._arid_compiled

    def is_source(self, path: str) -> bool:
        return (
            path.endswith(self.source_exts)
            and not _is_test_path(path)
            and not _is_non_source(path)
            and not is_excluded_dir(path)
        )

    def target_id(self, project_root: str, source_file: str) -> Optional[str]:
        """The identifier the mutation tool wants (FQCN, file path, or package).
        None when the file is not mutatable source for this language."""
        return self._target(project_root, source_file)

    def test_file(self, project_root: str, source_file: str) -> Optional[str]:
        """Best-guess absolute path to the test covering this source file.
        Callers check existence; a guess that does not exist means 'untested'."""
        return self._test(project_root, source_file)

    def engine_command(self, project_root: str) -> Optional[list]:
        """The argv prefix that invokes this language's mutation engine, or None
        when it is not installed. Step 1 and Step 4 both use this, so a
        reported 'installed' can never disagree with what actually runs."""
        return self._engine_cmd(project_root)

    def is_installed(self, project_root: str) -> bool:
        return bool(self._installed(project_root))

    def marker_paths(self, project_root: str) -> list:
        """Marker files that actually exist for this language."""
        root = Path(project_root)
        hits = []
        for m in self.markers:
            if m.startswith("*"):
                hits.extend(sorted(p.name for p in root.glob(m)))
            elif (root / m).exists():
                hits.append(m)
        return hits

    def source_files(self, project_root: str, limit: int = 20000) -> list:
        """Non-test source files of this language, repo-relative."""
        return [f for f in _walk_source(project_root, self.source_exts, limit)
                if self.is_source(f)]


_PROFILES = {
    "java": LangProfile(
        language="java", tool="pitest",
        markers=("pom.xml", "build.gradle", "build.gradle.kts"),
        source_exts=(".java", ".kt"), default_module="impl",
        test_patterns=("*Test.java", "*Tests.java", "*IT.java"),
        install_hint=("Maven: declare the pitest-maven plugin (mutation-testing profile) in the module pom; "
                      "Gradle: commit the Gradle wrapper (gradlew) or put gradle on PATH, the agent applies "
                      "the PIT plugin itself"),
        _arid_src=_ARID["java"], _target=_java_target, _test=_java_test,
        _engine_cmd=_pitest_cmd, _installed=_pitest_installed,
    ),
    "python": LangProfile(
        language="python", tool="mutmut",
        markers=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
        source_exts=(".py",), default_module=None,
        test_patterns=("test_*.py", "*_test.py"),
        install_hint="pip install mutmut",
        _arid_src=_ARID["python"], _target=_rel_target, _test=_python_test,
        _engine_cmd=_mutmut_cmd, _installed=_simple_installed(_mutmut_cmd),
    ),
    "typescript": LangProfile(
        language="typescript", tool="stryker",
        markers=("tsconfig.json", "package.json"),
        source_exts=(".ts", ".tsx"), default_module=None,
        test_patterns=("*.spec.ts", "*.test.ts"),
        install_hint="npm i -D @stryker-mutator/core",
        _arid_src=_ARID["typescript"], _target=_rel_target, _test=_ts_test,
        _engine_cmd=_stryker_cmd, _installed=_stryker_installed,
    ),
    "javascript": LangProfile(
        language="javascript", tool="stryker",
        markers=("package.json",),
        source_exts=(".js", ".jsx", ".mjs", ".cjs"), default_module=None,
        test_patterns=("*.spec.js", "*.test.js"),
        install_hint="npm i -D @stryker-mutator/core",
        _arid_src=_ARID["javascript"], _target=_rel_target, _test=_ts_test,
        _engine_cmd=_stryker_cmd, _installed=_stryker_installed,
    ),
    "csharp": LangProfile(
        language="csharp", tool="stryker-dotnet",
        markers=("*.sln", "*.csproj", "Directory.Build.props"),
        source_exts=(".cs",), default_module=None,
        test_patterns=("*Tests.cs", "*Test.cs", "*Spec.cs"),
        install_hint="dotnet tool install -g dotnet-stryker",
        _arid_src=_ARID["csharp"], _target=_rel_target, _test=_csharp_test,
        _engine_cmd=_stryker_dotnet_cmd, _installed=_simple_installed(_stryker_dotnet_cmd),
    ),
    "go": LangProfile(
        language="go", tool="gremlins",
        markers=("go.mod",),
        source_exts=(".go",), default_module=None,
        test_patterns=("*_test.go",),
        install_hint="go install github.com/go-gremlins/gremlins/cmd/gremlins@latest",
        _arid_src=_ARID["go"], _target=_go_target, _test=_go_test,
        _engine_cmd=_gremlins_cmd, _installed=_simple_installed(_gremlins_cmd),
    ),
}

# Reporting order only. Detection no longer picks a single winner, so this is
# presentation, not precedence.
_DETECT_ORDER = ["csharp", "java", "typescript", "javascript", "python", "go"]

# Every extension the pipeline may treat as source, across all languages --
# used by the changed-files scanner when it has no profile yet.
ALL_SOURCE_EXTS = tuple(sorted({e for p in _PROFILES.values() for e in p.source_exts}))

# .ts and .js overlap conceptually (a repo with both is one Stryker run), so a
# language whose sources are a strict subset of another's is folded away when
# both are detected.
_SUBSUMES = {"typescript": "javascript"}


def get_profile(language: str) -> LangProfile:
    key = (language or "").lower()
    if key not in _PROFILES:
        raise KeyError(f"No language profile for '{language}'. "
                       f"Known: {', '.join(sorted(_PROFILES))}")
    return _PROFILES[key]


def all_profiles() -> list:
    return [_PROFILES[l] for l in _DETECT_ORDER]


def profile_for_file(source_file: str) -> Optional[LangProfile]:
    """Return the language profile that treats `source_file` as mutatable
    source, or None when no profile does (a test file, a config file, a
    generated file, or an unsupported extension).

    This is extension-driven, so a polyglot repo maps *each* changed file to
    its own engine independently of whatever markers sit at the repo root. It
    is what lets one run mutate Java, TypeScript and Python files together.
    Source extensions are disjoint across profiles, so first match is exact."""
    for lang in _DETECT_ORDER:
        prof = _PROFILES[lang]
        if prof.is_source(source_file):
            return prof
    return None


def detect_languages(project_root: str, probe_install: bool = True,
                     source_scan_limit: int = 20000) -> list:
    """Every language actually present in `project_root`, most source-heavy first.

    A marker file on its own is NOT treated as proof. This repo ships a
    `pom.xml` only so `frontend-maven-plugin` can build an Angular app and
    holds zero `.java` files; a marker-only rule reports it as a Java/PIT
    project and never mutates the TypeScript that makes up the entire
    codebase. So a language is reported only when a marker AND at least one
    non-test source file both exist.

    Returns a list of dicts:
        {language, tool, markers, sourceFileCount, sampleFiles, testPatterns,
         installed, installHint}
    Empty list when nothing supported is found -- callers decide whether that
    is fatal.
    """
    found = []
    for lang in _DETECT_ORDER:
        prof = _PROFILES[lang]
        markers = prof.marker_paths(project_root)
        if not markers:
            continue
        sources = prof.source_files(project_root, limit=source_scan_limit)
        if not sources:
            continue
        found.append({
            "language": prof.language,
            "tool": prof.tool,
            "markers": markers,
            "sourceFileCount": len(sources),
            "sampleFiles": sources[:5],
            "testPatterns": list(prof.test_patterns),
            "installed": prof.is_installed(project_root) if probe_install else None,
            "installHint": prof.install_hint,
        })

    present = {e["language"] for e in found}
    found = [e for e in found
             if _SUBSUMES.get(e["language"]) not in present or e["sourceFileCount"] > 0]
    # Fold javascript into typescript when both are present: one Stryker run
    # covers both, and reporting them separately double-counts the engine.
    if "typescript" in present and "javascript" in present:
        found = [e for e in found if e["language"] != "javascript"]

    found.sort(key=lambda e: -e["sourceFileCount"])
    return found


def profile_for_root(project_root: str) -> LangProfile:
    """The single most source-heavy language in the repo.

    Retained for callers that genuinely need one profile (module defaults,
    prompt fencing). It is ADVISORY: per-file resolution via
    `profile_for_file()` decides what each engine actually mutates, so a wrong
    guess here no longer discards other languages' files."""
    detected = detect_languages(project_root, probe_install=False)
    if detected:
        return _PROFILES[detected[0]["language"]]
    # Nothing corroborated: fall back to a bare marker so single-file runs
    # against an otherwise-empty tree still resolve a profile.
    for lang in _DETECT_ORDER:
        if _PROFILES[lang].marker_paths(project_root):
            return _PROFILES[lang]
    raise RuntimeError(
        f"No supported project marker found in {project_root}. Looked for: "
        + ", ".join(sorted({m for p in _PROFILES.values() for m in p.markers}))
    )


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    entries = detect_languages(root)
    if not entries:
        print(f"No supported language detected in {root}")
        raise SystemExit(1)
    for e in entries:
        flag = {True: "yes", False: "NO", None: "?"}[e["installed"]]
        print(f"{e['language']:<11} {e['tool']:<15} "
              f"{e['sourceFileCount']:>5} files  markers={','.join(e['markers'])}  "
              f"engine installed={flag}")

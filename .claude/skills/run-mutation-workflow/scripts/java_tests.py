# -*- coding: utf-8 -*-
"""
Which tests PIT runs for a Java/Kotlin change.

PIT needs a green suite: one test that fails without any mutation stops the whole run. So the
agent must not hand PIT every test of a package. It picks:

  * the tests that cover the mutated classes: the class's own test, plus every test class in the
    module's `src/test` that names the class (same package, or an import of it);
  * minus the tests that need Docker (Testcontainers), when Docker is not reachable;
  * minus the test classes the caller excluded (--exclude-tests / MUTATION_EXCLUDED_TESTS).

When no test names the class, the selection falls back to the tests of the class's package, as
before, so a class covered only indirectly still runs against something.

A class that only an excluded test covers is reported NO_COVERAGE: that is a true statement about
the tests that ran.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

TEST_SOURCE_DIRS = ("src/test/java", "src/test/kotlin", "src/test/groovy")
TEST_SOURCE_EXTS = (".java", ".kt", ".groovy")
EXCLUDE_ENV = "MUTATION_EXCLUDED_TESTS"

_MAX_FILE_BYTES = 1_000_000
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
_IDENT_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
# A test class that starts containers: the Testcontainers library, its JUnit extension, or @Container.
_CONTAINER_RE = re.compile(r"org\.testcontainers|@Testcontainers\b|@Container\b")


def excluded_from_env(env=None) -> list:
    """The comma separated class globs of MUTATION_EXCLUDED_TESTS."""
    return split_globs((env if env is not None else os.environ).get(EXCLUDE_ENV, ""))


def split_globs(*values) -> list:
    """Class globs from any mix of comma separated strings and lists, without duplicates."""
    out = []
    for value in values:
        parts = value if isinstance(value, (list, tuple)) else [value]
        for part in parts:
            for glob in str(part or "").split(","):
                glob = glob.strip()
                if glob and glob not in out:
                    out.append(glob)
    return out


_DOCKER_STATE = {}


def docker_available() -> bool:
    """True when `docker info` reaches a Docker daemon (also Colima, Podman with a docker shim).
    Checked once per process."""
    if "ok" not in _DOCKER_STATE:
        ok = False
        exe = shutil.which("docker")
        if exe:
            try:
                ok = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                                    capture_output=True, timeout=20, check=False).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        _DOCKER_STATE["ok"] = ok
    return _DOCKER_STATE["ok"]


def _glob_matches(name: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def _class_globs(names) -> list:
    """A test class and its nested classes (JUnit 5 @Nested: Outer$Inner)."""
    out = []
    for n in names:
        out += [n, n + "$*"]
    return out


def package_globs(target_classes) -> list:
    """Every test in the packages of the target classes (com.acme.Calc -> com.acme.*)."""
    globs = []
    for cls in target_classes or []:
        pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
        glob = f"{pkg}.*" if pkg else "*"
        if glob not in globs:
            globs.append(glob)
    return globs


def _read_test_sources(test_roots):
    """[(path, package, class name, text)] for every file under the test source roots."""
    files = []
    for root in test_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in sorted(filenames):
                if not name.endswith(TEST_SOURCE_EXTS):
                    continue
                path = Path(dirpath) / name
                try:
                    if path.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = _PACKAGE_RE.search(text)
                files.append((path, m.group(1) if m else "", path.stem, text))
    return files


def container_bound(files) -> set:
    """Class names (package-qualified) of the test classes that need Docker: those that use
    Testcontainers themselves, and those that use such a class (a shared base class, a support
    class), followed to the end."""
    fq = {(pkg + "." if pkg else "") + stem: (stem, text) for _, pkg, stem, text in files}
    bound = {name for name, (_, text) in fq.items() if _CONTAINER_RE.search(text)}
    idents = {name: set(_IDENT_RE.findall(text)) for name, (_, text) in fq.items()}
    changed = True
    while changed:
        changed = False
        bound_stems = {fq[b][0] for b in bound}
        for name in fq:
            if name not in bound and idents[name] & bound_stems:
                bound.add(name)
                changed = True
    return bound


def select_tests(project_root, module_dir, target_classes, own_tests=(), exclude=(),
                 full_suite=False, docker_ok=None) -> dict:
    """Tests PIT runs for `target_classes` in the module at `module_dir` (relative to the project
    root, "" for the root project).

    `own_tests` are the covering test files found by name (project-relative paths).
    Returns {"tests": [...] PIT targetTests globs, "excluded": [...] PIT excludedTestClasses globs,
    "mode": "referencing" | "package" | "full", "selected": [class names], "container": [class
    names left out for lack of Docker], "user_excluded": [class names left out by --exclude-tests],
    "files": [the selected test files, project-relative; empty unless mode is "referencing"]}.
    """
    root = Path(project_root)
    mod = root / module_dir if module_dir else root
    exclude = split_globs(exclude)
    test_roots = [mod / d for d in TEST_SOURCE_DIRS if (mod / d).is_dir()]
    files = _read_test_sources(test_roots)

    needs_docker = container_bound(files)
    skip_container = sorted(needs_docker) if needs_docker and not (
        docker_available() if docker_ok is None else docker_ok) else []

    def left_out(name):
        return name in skip_container or _glob_matches(name, exclude)

    excluded = _class_globs(skip_container) + exclude

    if full_suite:
        return {"tests": ["*"], "excluded": excluded, "mode": "full", "selected": [],
                "container": skip_container, "user_excluded": [], "files": []}

    own = {(root / p).resolve() for p in own_tests}
    wanted = []
    for cls in target_classes or []:
        pkg, _, simple = cls.rpartition(".")
        wanted.append((pkg, simple, re.compile(r"\b" + re.escape(simple) + r"\b")))

    selected, paths = [], {}
    for path, pkg, stem, text in files:
        name = (pkg + "." if pkg else "") + stem
        covers = path.resolve() in own
        if not covers:
            for tpkg, simple, word in wanted:
                if not word.search(text):
                    continue
                if (pkg == tpkg or f"{tpkg}.{simple}" in text or f"import {tpkg}.*" in text):
                    covers = True
                    break
        if covers and name not in selected:
            selected.append(name)
            paths[name] = path

    kept = [n for n in selected if not left_out(n)]
    user_excluded = [n for n in selected if n not in skip_container and _glob_matches(n, exclude)]
    if kept:
        return {"tests": _class_globs(kept), "excluded": excluded, "mode": "referencing",
                "selected": kept, "container": [n for n in selected if n in skip_container],
                "user_excluded": user_excluded,
                "files": [Path(os.path.relpath(paths[n], root)).as_posix() for n in kept]}
    # No test names the class (or every one that does is left out): the package's tests, minus
    # what is left out.
    return {"tests": package_globs(target_classes), "excluded": excluded, "mode": "package",
            "selected": [], "container": [n for n in selected if n in skip_container],
            "user_excluded": user_excluded, "files": [], "all_covering_left_out": bool(selected)}

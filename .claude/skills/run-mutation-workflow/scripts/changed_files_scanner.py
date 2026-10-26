# -*- coding: utf-8 -*-
"""
Step 2: Find source files changed vs a base branch (or locally). Replaces the
changed-files-scanner LLM skill -- pure git commands plus the shared language
registry.

Polyglot: qualification is by extension across every supported language
(java/kotlin, python, typescript/javascript, c#, go) via `lang_profiles`, not a
single JVM `src/main` layout, and the result is grouped by language so the
orchestrator can drive several engines from one changeset.

Three correctness rules this scanner enforces, each of which was previously
getting silently violated:

1. NEVER exclude by bare substring. `"test" in path` also discards
   `latest-news.ts`, `Contest.java` and `specialist.service.ts`. A dropped file
   is worse than a slow run: the mutation score looks clean because that file
   was never measured. Exclusion is anchored on path SEGMENTS and file-name
   suffixes (see `lang_profiles._is_test_path` / `_is_non_source`).
2. NEVER return a path that is not on disk. `git diff` reports deletions, and a
   deleted file crashes Step 3 with FileNotFoundError.
3. ALWAYS include untracked files in local scope. `git diff HEAD` omits them, so
   a brand-new service -- the file most likely to need mutation testing -- was
   being skipped by exactly the mode meant to catch "what I just changed".
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from .lang_profiles import (  # package import -- plugin installed anywhere
        ALL_SOURCE_EXTS, profile_for_file, is_excluded_dir,
        _is_test_path, _is_non_source,
    )
except ImportError:  # direct invocation: py .../changed_files_scanner.py
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from lang_profiles import (  # noqa: E402
            ALL_SOURCE_EXTS, profile_for_file, is_excluded_dir,
            _is_test_path, _is_non_source,
        )
    except Exception:  # pragma: no cover - standalone fallback
        _FALLBACK = True
if "_FALLBACK" in dir():  # pragma: no cover - standalone fallback
    ALL_SOURCE_EXTS = (
        ".cjs", ".cs", ".go", ".java", ".js", ".jsx", ".kt", ".mjs", ".py",
        ".ts", ".tsx",
    )
    profile_for_file = None

    def is_excluded_dir(path):
        parts = set(Path(path).parts[:-1])
        return bool(parts & {".git", "node_modules", "target", "dist", "build",
                             ".claude", ".github", ".venv"})

    def _is_test_path(path):
        name = Path(path).name.lower()
        return ".spec." in name or ".test." in name or name.startswith("test_")

    def _is_non_source(path):
        return Path(path).name.endswith(".d.ts")


EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's magic empty-tree hash

# Volume above which a run is worth warning about rather than silently starting.
LARGE_CHANGESET = 20


# --- public API ---------------------------------------------------------------

# Files that make a directory a build/package root for one of the supported engines.
# Deliberately package-level only: tsconfig.json and similar also appear inside a package.
_PROJECT_MARKERS = ("package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                    "settings.gradle", "settings.gradle.kts", "pyproject.toml", "setup.py",
                    "setup.cfg", "Pipfile", "requirements.txt", "go.mod")


def _has_project_marker(d: Path) -> bool:
    if any((d / m).exists() for m in _PROJECT_MARKERS):
        return True
    return any(any(d.glob(pat)) for pat in ("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "*.slnx"))


def _outermost_marker_dir(path: Path, toplevel: Path):
    """The marker directory closest to `toplevel` on the way down to `path`, else None.

    Outermost, not nearest: a Maven/Gradle module or a C# project sits under the
    aggregator pom / solution that must be the working directory, and a
    JS workspace package under the root that owns node_modules."""
    try:
        rel = path.resolve().relative_to(toplevel)
    except (ValueError, OSError):
        return None
    d = toplevel
    for part in rel.parts:
        d = d / part
        if d.is_dir() and _has_project_marker(d):
            return d
    return None


def _git_lines(cwd, *args) -> list:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def _argv_values(flag: str) -> list:
    """Values of `flag` on this process's command line (`--flag v` or `--flag=v`)."""
    argv, out = sys.argv[1:], []
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif tok.startswith(flag + "="):
            out.append(tok.split("=", 1)[1])
    return out


def _hint_files(start: Path, toplevel: Path) -> list:
    """Toplevel-relative paths of the files this run is about to mutate, best effort.

    Same precedence as scope resolution: explicit --file, else --base diff plus local
    work, else local uncommitted/untracked work, else the last commit."""
    explicit = _argv_values("--file")
    if explicit:
        out = []
        for f in explicit:
            for base in (start, toplevel):
                cand = (base / f)
                if cand.exists():
                    try:
                        out.append(cand.resolve().relative_to(toplevel).as_posix())
                    except ValueError:
                        pass
                    break
        return out
    base = (_argv_values("--base") or [None])[-1]
    names = []
    if base:
        names += _git_lines(toplevel, "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD")
    names += _git_lines(toplevel, "diff", "--name-only", "--diff-filter=d", "HEAD")
    names += _git_lines(toplevel, "ls-files", "--others", "--exclude-standard")
    if not names and not base:
        names = _git_lines(toplevel, "diff", "--name-only", "--diff-filter=d", "HEAD~1", "HEAD") \
            or _git_lines(toplevel, "show", "--name-only", "--format=", "--diff-filter=d", "HEAD")
    return names


def _nested_project_dir(start: Path, toplevel: Path, hint_files=None) -> Path:
    """The project dir to use when the git toplevel is not itself a project.

    Monorepos often keep the package (package.json + node_modules + the mutation
    tool's config) in a subdirectory. A toplevel that IS a project is always kept, so
    multi-module Maven/Gradle repos still resolve to the root. Otherwise, in order:
    the outermost project directory between the toplevel and the working directory;
    then the project directory holding most of the files being mutated (so a session
    started at the repo root still finds the package); else the toplevel."""
    if _has_project_marker(toplevel):
        return toplevel
    here = _outermost_marker_dir(start, toplevel)
    if here is not None:
        return here
    hints = _hint_files(start, toplevel) if hint_files is None else hint_files
    counts = {}
    for f in hints:
        d = _outermost_marker_dir(toplevel / f, toplevel)
        if d is not None:
            counts[d] = counts.get(d, 0) + 1
    if counts:
        return sorted(counts, key=lambda d: (-counts[d], str(d)))[0]
    return toplevel


def resolve_project_root(start=None, hint_files=None):
    """Root of the repo under test, resolved from the invocation context.

    Never derived from this file's own location: the skill may be installed at
    a project's .claude/skills/, under ~/.claude/plugins/, or a marketplace
    checkout, none of which say anything about where the code being mutated
    lives. Resolution order: MUTATION_PROJECT_ROOT env var, `git rev-parse
    --show-toplevel` from the working directory (narrowed to the package that
    holds the code being mutated when the toplevel itself has no build marker --
    see `_nested_project_dir`), the working directory itself.
    """
    env = os.environ.get("MUTATION_PROJECT_ROOT")
    if env:
        return str(Path(env).resolve())
    start = start or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True,
            text=True, cwd=start, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            top = Path(result.stdout.strip()).resolve()
            return str(_nested_project_dir(Path(start).resolve(), top, hint_files))
    except (OSError, subprocess.SubprocessError):  # git missing or hung
        pass
    return str(Path(start).resolve())


def changes_outside_root(project_root: str) -> list:
    """Changed source files in the repo that live outside `project_root` (other packages of a
    monorepo). They are not mutated by this run; callers surface them."""
    root = Path(project_root).resolve()
    top_lines = _git_lines(root, "rev-parse", "--show-toplevel")
    if not top_lines:
        return []
    top = Path(top_lines[0]).resolve()
    if top == root:
        return []
    names = (_git_lines(top, "diff", "--name-only", "--diff-filter=d", "HEAD")
             + _git_lines(top, "ls-files", "--others", "--exclude-standard"))
    outside = []
    for n in names:
        try:
            (top / n).resolve().relative_to(root)
        except ValueError:
            # Only what a run could have mutated: not READMEs, config, tests or build output.
            if (n.endswith(tuple(ALL_SOURCE_EXTS)) and not is_excluded_dir(n)
                    and not _is_test_path(n) and not _is_non_source(n)):
                outside.append(n)
    return sorted(set(outside))


def scan(project_root: str, base: str = None) -> dict:
    """Resolve scope and return a full, language-grouped result.

    `base` given      -> merge-base diff against that ref, plus local work.
    `base` omitted    -> local uncommitted+untracked changes, else the last commit.

    Returns:
        {files, byLanguage, skipped, scope, detail, baseRef, fileCount, lineRanges}
    """
    if base:
        merge_base = _merge_base(project_root, base)
        raw = _diff_files(project_root, merge_base) + _untracked(project_root)
        scope, detail, base_ref = "branch", f"merge-base with {base}", merge_base
        diff_ref = merge_base
    else:
        resolved = _local_or_last_commit(project_root)
        raw, scope, detail = resolved["raw"], resolved["scope"], resolved["detail"]
        base_ref = None
        diff_ref = resolved["ref"]

    files, skipped = _filter_source_files(raw, project_root)
    return {
        "files": files,
        "byLanguage": group_by_language(files),
        "skipped": skipped,
        "scope": scope,
        "detail": detail,
        "baseRef": base_ref,
        "fileCount": len(files),
        # {file: set(changed line numbers) | None}. None means "whole file" --
        # untracked/new files, or no parsable diff -- so a mutant is never
        # silently dropped just because its ranges couldn't be determined.
        "lineRanges": diff_line_ranges(project_root, files, diff_ref),
    }


def resolve_line_ranges_for_files(project_root: str, files: list, base: str = None) -> dict:
    """Line ranges for a caller-supplied file list (e.g. `--file`) that never
    called `scan()` itself. Uses the same ref-resolution rules as `scan()` so
    an explicit file list still gets diff-scoped, not whole-file, mutation
    scoring.

    `--file` mode is meant to carry zero git dependency (a synthetic
    workspace, a fresh checkout with no commits, a plain directory that is
    not a git repository at all) -- so a failure resolving a ref must not
    crash the run. Fail open to whole-file scoring for every file instead.
    """
    try:
        ref = _merge_base(project_root, base) if base else _local_or_last_commit(project_root)["ref"]
        return diff_line_ranges(project_root, files, ref)
    except RuntimeError:
        return {f: None for f in files}


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _hunk_added_lines(diff_output: str) -> set:
    """Parse a `git diff -U0` hunk header's new-file side into the exact line
    numbers the diff changed (added or modified) -- this is what makes scoring
    diff-scoped rather than whole-file: none of the five mutation engines
    support surgical line-level mutation, so the tool still mutates the whole
    file and this set is used afterward to filter its raw output down to only
    the lines actually touched. A pure-deletion hunk (count 0) has no new-file
    line of its own, so the line right after the deletion point is flagged as
    the closest anchor instead of nothing."""
    lines = set()
    for line in diff_output.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            lines.add(max(start, 1))
        else:
            lines.update(range(start, start + count))
    return lines


def diff_line_ranges(project_root: str, files: list, ref: str) -> dict:
    """{file: set(int) | None} against `ref`. None ("whole file") covers an
    untracked/new file (nothing to diff against -- everything in it is new)
    and the fail-open case where no diff could be parsed, so a file is never
    silently excluded from scoring entirely because its ranges are unknown."""
    ranges = {}
    for f in files:
        is_tracked = _git(project_root, "ls-files", "--error-unmatch", "--", f).returncode == 0
        if not is_tracked:
            ranges[f] = None
            continue
        result = _git(project_root, "diff", "-U0", "--diff-filter=d", ref, "--", f)
        if result.returncode != 0 or not result.stdout.strip():
            ranges[f] = None
            continue
        parsed = _hunk_added_lines(result.stdout)
        ranges[f] = parsed if parsed else None
    return ranges


def get_local_or_last_commit_files(project_root: str) -> dict:
    """
    Used when no PR/base-branch context was given at all (no --base, no --file):
    scope to the smallest thing that could plausibly be "what the user just
    changed" instead of diffing the whole branch since it left its base.

    Priority:
      1. Uncommitted local changes -- tracked modifications (`git diff HEAD`)
         AND untracked new files (`git ls-files --others`). Someone actively
         mid-edit almost certainly means these, and a new file is the most
         likely thing to need mutation testing.
      2. Falls back to the single most recent commit (HEAD~1..HEAD) when the
         working tree is clean.
      3. Falls back to the initial commit's own tree (diff against git's
         empty-tree hash) when there is no HEAD~1 at all (single-commit repo).

    Returns {"files", "byLanguage", "skipped", "scope", "detail"} where scope is
    "local" | "last-commit" | "initial-commit".
    """
    result = scan(project_root, base=None)
    return result


def group_by_language(files: list) -> dict:
    """{language: [files]} using each file's OWN extension.

    This is the polyglot pivot: it is what lets one changeset fan out to
    several mutation engines instead of everything but the primary language
    being discarded."""
    grouped = {}
    for f in files:
        prof = profile_for_file(f) if profile_for_file else None
        grouped.setdefault(prof.language if prof else "unknown", []).append(f)
    return {k: sorted(v) for k, v in sorted(grouped.items())}


# --- git plumbing -------------------------------------------------------------

def _git(project_root: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=project_root,
        encoding="utf-8", errors="replace",
    )


def _merge_base(project_root: str, base_branch: str) -> str:
    result = _git(project_root, "merge-base", "HEAD", base_branch)
    if result.returncode != 0:
        # fallback: diff against the base branch directly
        return base_branch
    return result.stdout.strip()


def _diff_files(project_root: str, base: str) -> list:
    """Changed paths vs `base`, excluding deletions.

    `--diff-filter=d` drops deleted paths at the source. Returning a path that
    no longer exists crashes Step 3, and there is nothing to mutate in a file
    that is gone."""
    # --relative: paths relative to project_root, which may be below the git toplevel.
    result = _git(project_root, "diff", "--name-only", "--relative", "--diff-filter=d", base)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _untracked(project_root: str) -> list:
    """New files git does not know about yet.

    `git diff` cannot see these, so without this a brand-new source file is
    invisible to the very scope mode meant to catch recent work."""
    result = _git(project_root, "ls-files", "--others", "--exclude-standard")
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _local_or_last_commit(project_root: str) -> dict:
    tracked = _diff_files(project_root, "HEAD")
    untracked = _untracked(project_root)
    if tracked or untracked:
        bits = []
        if tracked:
            bits.append(f"{len(tracked)} modified")
        if untracked:
            bits.append(f"{len(untracked)} untracked")
        return {
            "raw": tracked + untracked,
            "scope": "local",
            "detail": ", ".join(bits) + " file(s)",
            "ref": "HEAD",
        }

    has_parent = _git(project_root, "rev-parse", "--verify", "HEAD~1").returncode == 0
    if has_parent:
        subject = _git(project_root, "log", "-1", "--format=%h %s").stdout.strip()
        return {
            "raw": _diff_files(project_root, "HEAD~1"),
            "scope": "last-commit",
            "detail": subject or "HEAD~1..HEAD",
            "ref": "HEAD~1",
        }

    return {
        "raw": _diff_files(project_root, EMPTY_TREE_SHA),
        "scope": "initial-commit",
        "detail": "single-commit repo, no HEAD~1 -- diffed against the empty tree",
        "ref": EMPTY_TREE_SHA,
    }


# --- filtering ---------------------------------------------------------------

def _filter_source_files(files: list, project_root: str = ".") -> tuple:
    """Return (kept, skipped) where skipped items carry a reason.

    Every rejection is reported with its reason rather than dropped silently --
    a file that vanishes between `git diff` and the mutation run is the single
    easiest way to get a falsely clean score."""
    kept, skipped, seen = [], [], set()
    for f in files:
        f = Path(f).as_posix()
        if f in seen:
            continue
        seen.add(f)

        if not f.endswith(tuple(ALL_SOURCE_EXTS)):
            skipped.append({"file": f, "reason": "not a supported source extension"})
        elif is_excluded_dir(f):
            skipped.append({"file": f, "reason": "build output, dependency or tooling directory"})
        elif _is_test_path(f):
            skipped.append({"file": f, "reason": "test file"})
        elif _is_non_source(f):
            skipped.append({"file": f, "reason": "config, declaration, barrel or generated file"})
        elif not os.path.exists(os.path.join(project_root, f)):
            skipped.append({"file": f, "reason": "no longer on disk (deleted or moved)"})
        else:
            kept.append(f)
    return sorted(kept), skipped


# --- CLI ---------------------------------------------------------------------

def _main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    argv = [a for a in argv if not a.startswith("--")]
    root = argv[0] if argv else os.getcwd()
    base = argv[1] if len(argv) > 1 else None

    try:
        result = scan(root, base=base)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    if as_json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Scope  : {result['scope']}  ({result['detail']})")
    if result["baseRef"]:
        print(f"Base   : {result['baseRef']}")

    if not result["files"]:
        print("\nNo changed source files found. Nothing to mutate.")
    else:
        print(f"\nChanged source files ({result['fileCount']}):")
        for lang, group in result["byLanguage"].items():
            print(f"  [{lang}]")
            for f in group:
                print(f"    {f}")

    if result["skipped"]:
        print(f"\nSkipped ({len(result['skipped'])}):")
        for s in result["skipped"]:
            print(f"  {s['file']}  --  {s['reason']}")

    if result["fileCount"] > LARGE_CHANGESET:
        print(f"\nWARNING: {result['fileCount']} source files in scope. A full "
              f"mutation run over this many files can take 30-60+ minutes. "
              f"Consider narrowing with --file.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

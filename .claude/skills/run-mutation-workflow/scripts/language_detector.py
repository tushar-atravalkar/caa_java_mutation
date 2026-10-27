# -*- coding: utf-8 -*-
"""
Step 1: Detect every programming language present in the project, and the
mutation engine for each. Replaces the language-detector LLM skill -- pure
file-existence checks plus one `--version` probe per engine.

Polyglot: this returns a LIST of languages, not a winner. The marker -> language
-> tool table lives in `lang_profiles.py` so every step shares one source of
truth.

Why a marker file is not enough
-------------------------------
A `pom.xml` can exist purely to drive `frontend-maven-plugin` for an Angular
app, with zero `.java` files anywhere. Priority-ordered marker matching reports
that repo as Java/PIT and the whole TypeScript codebase goes unmutated -- with
a clean-looking score, because nothing was measured. Detection therefore
requires a marker AND at least one non-test source file of that language.

Back-compat: `detect()` still returns the flat {language, tool, installed,
project_root} keys older callers read, populated from the most source-heavy
language, and adds a `languages` list plus `installedByLanguage` for
polyglot callers.
"""

import json
import os
import sys
from pathlib import Path

try:  # package-relative import -- resolves at any install location
    from .lang_profiles import detect_languages, get_profile, profile_for_file
except ImportError:  # direct invocation: py .../language_detector.py
    sys.path.insert(0, str(Path(__file__).parent))
    from lang_profiles import (  # noqa: E402
        detect_languages, get_profile, profile_for_file,
    )


def detect(project_root: str, probe_install: bool = True) -> dict:
    """Return the full polyglot detection result for `project_root`.

    Raises RuntimeError only when NO supported language is present at all.
    """
    languages = detect_languages(project_root, probe_install=probe_install)
    if not languages:
        raise RuntimeError(
            f"No supported language detected in {project_root}. Looked for source "
            "files alongside a project marker for: csharp, java, typescript, "
            "javascript, python, go."
        )

    primary = languages[0]
    return {
        # --- flat keys, unchanged contract for single-language callers -------
        "language": primary["language"],
        "tool": primary["tool"],
        "installed": primary["installed"],
        "project_root": project_root,
        # --- polyglot view --------------------------------------------------
        "languages": languages,
        "tools": [e["tool"] for e in languages],
        "installedByLanguage": {e["language"]: e["installed"] for e in languages},
        "polyglot": len(languages) > 1,
    }


def detect_for_files(project_root: str, files: list) -> dict:
    """Detection narrowed to a specific file list.

    When the caller already knows the scope (a PR diff, an explicit --file),
    what matters is the engines those files need -- not every language in the
    repo. Returns the same shape as `detect()` but only for languages the
    given files actually belong to.
    """
    wanted = {}
    for f in files:
        prof = profile_for_file(f)
        if prof is None:
            continue
        wanted.setdefault(prof.language, []).append(f)

    languages = []
    for lang, owned in wanted.items():
        prof = get_profile(lang)
        languages.append({
            "language": lang,
            "tool": prof.tool,
            "markers": prof.marker_paths(project_root),
            "sourceFileCount": len(owned),
            "sampleFiles": owned[:5],
            "testPatterns": list(prof.test_patterns),
            "installed": prof.is_installed(project_root),
            "installHint": prof.install_hint,
        })
    languages.sort(key=lambda e: -e["sourceFileCount"])

    if not languages:
        raise RuntimeError(
            "None of the given files are mutatable source for a supported "
            "language (they may all be tests, configs or generated files)."
        )
    primary = languages[0]
    return {
        "language": primary["language"],
        "tool": primary["tool"],
        "installed": primary["installed"],
        "project_root": project_root,
        "languages": languages,
        "tools": [e["tool"] for e in languages],
        "installedByLanguage": {e["language"]: e["installed"] for e in languages},
        "polyglot": len(languages) > 1,
    }


def missing_engines(info: dict) -> list:
    """Languages in scope whose mutation engine is not installed.

    The orchestrator STOPs on these: a missing engine is an environment
    problem, and running the other languages anyway would report a score that
    silently omits one language.
    """
    return [e for e in info.get("languages", []) if e["installed"] is False]


def _print_human(info: dict) -> None:
    langs = info["languages"]
    label = "languages" if len(langs) > 1 else "language"
    print(f"Detected {len(langs)} {label} in {info['project_root']}")
    print()
    print(f"  {'LANGUAGE':<11} {'ENGINE':<15} {'FILES':>6}  {'ENGINE?':<8} MARKERS")
    for e in langs:
        flag = {True: "yes", False: "MISSING", None: "unknown"}[e["installed"]]
        print(f"  {e['language']:<11} {e['tool']:<15} {e['sourceFileCount']:>6}  "
              f"{flag:<8} {','.join(e['markers'])}")
    print()
    for e in missing_engines(info):
        print(f"  ERROR: {e['tool']} for {e['language']} is not installed "
              f"-- {e['installHint']}")


def _main() -> int:
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    args = [a for a in args if not a.startswith("--")]
    root = args[0] if args else os.getcwd()
    files = args[1:]

    try:
        info = detect_for_files(root, files) if files else detect(root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    if as_json:
        print(json.dumps(info, indent=2))
    else:
        _print_human(info)

    # exit 2 == detected fine, but at least one engine is missing. Distinct from
    # exit 1 (nothing detected) so the orchestrator can tell the two apart.
    return 2 if missing_engines(info) else 0


if __name__ == "__main__":
    sys.exit(_main())

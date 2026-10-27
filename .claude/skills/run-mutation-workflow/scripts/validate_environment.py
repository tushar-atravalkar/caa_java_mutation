# -*- coding: utf-8 -*-
"""
Pre-flight environment validation -- check mutation engines before a run.

Detects which languages are actually present in the project (using the same
corroborated detection as Step 1), then checks only the engines those languages
need. A Java project only checks pitest/mvn; a TypeScript project only checks
stryker. Engines for absent languages are not evaluated -- a missing mutmut is
not an error in a Java-only repo.

Exit codes:
    0   all engines required by the detected languages are available
    1   one or more required engines missing

Usage:
    python validate_environment.py                 # check engines for this project
    python validate_environment.py --json          # JSON output
    python validate_environment.py /path/to/repo   # explicit project root
"""

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent

try:  # package-relative import -- resolves at any install location
    from .lang_profiles import (
        detect_languages,
        _stryker_cmd, _pitest_cmd, _mutmut_cmd, _gremlins_cmd, _stryker_dotnet_cmd,
    )
except ImportError:  # direct invocation: py .../validate_environment.py
    sys.path.insert(0, str(_HERE))
    from lang_profiles import (  # noqa: E402
        detect_languages,
        _stryker_cmd, _pitest_cmd, _mutmut_cmd, _gremlins_cmd, _stryker_dotnet_cmd,
    )

# Maps lang_profiles language names to their engine resolver and install hint.
# Resolvers are the same functions the pipeline calls at runtime, so
# "available here" == "found at runtime".
_LANG_ENGINE = {
    "csharp":     ("dotnet-stryker", _stryker_dotnet_cmd,
                   "dotnet tool install -g dotnet-stryker"),
    "java":       ("pitest",         _pitest_cmd,
                   "Maven: install Maven and declare pitest-maven in pom.xml; Gradle: commit the wrapper (gradlew) or install gradle"),
    "kotlin":     ("pitest",         _pitest_cmd,
                   "Maven: install Maven and declare pitest-maven in pom.xml; Gradle: commit the wrapper (gradlew) or install gradle"),
    "typescript": ("stryker",        _stryker_cmd,
                   "npm i -D @stryker-mutator/core (or install npx globally)"),
    "javascript": ("stryker",        _stryker_cmd,
                   "npm i -D @stryker-mutator/core (or install npx globally)"),
    "python":     ("mutmut",         _mutmut_cmd,
                   "pip install mutmut"),
    "go":         ("gremlins",       _gremlins_cmd,
                   "go install github.com/go-gremlins/gremlins/cmd/gremlins@latest"),
}


def check_engines(project_root: str) -> dict:
    """Detect languages present in project_root, then probe only their engines.

    Returns:
        {
          "projectRoot": str,
          "detectedLanguages": [str, ...],
          "engines": [
            {
              "name": str,       engine tool name
              "languages": [str], languages that use this engine
              "available": bool,
              "install_hint": str,
            },
            ...
          ],
          "allAvailable": bool,
          "missingEngines": [str, ...],
        }
    """
    detected = detect_languages(project_root, probe_install=False)
    lang_names = [e["language"] for e in detected]

    # Deduplicate engines: java and kotlin both use pitest; check it once.
    seen_engines = {}
    for lang in lang_names:
        if lang not in _LANG_ENGINE:
            continue
        engine_name, resolver, hint = _LANG_ENGINE[lang]
        if engine_name not in seen_engines:
            seen_engines[engine_name] = {
                "name": engine_name,
                "languages": [],
                "resolver": resolver,
                "install_hint": hint,
            }
        seen_engines[engine_name]["languages"].append(lang)

    engines = []
    for entry in seen_engines.values():
        cmd = entry["resolver"](project_root)
        engines.append({
            "name": entry["name"],
            "languages": entry["languages"],
            "available": cmd is not None,
            "install_hint": entry["install_hint"],
        })

    missing = [e["name"] for e in engines if not e["available"]]
    return {
        "projectRoot": project_root,
        "detectedLanguages": lang_names,
        "engines": engines,
        "allAvailable": len(missing) == 0,
        "missingEngines": missing,
    }


def _print_human(result: dict) -> None:
    langs = result["detectedLanguages"]
    engines = result["engines"]
    missing = result["missingEngines"]

    print("Mutation engine pre-flight check")
    print(f"  project root      : {result['projectRoot']}")
    print(f"  detected languages: {', '.join(langs) if langs else '(none)'}")
    print()

    if not engines:
        print("  No supported languages detected -- nothing to check.")
        return

    print(f"  {'ENGINE':<18} {'LANGUAGES':<28} STATUS")
    print(f"  {'-'*18} {'-'*28} {'-'*10}")
    for e in engines:
        status = "available" if e["available"] else "MISSING"
        lang_str = ", ".join(e["languages"])
        print(f"  {e['name']:<18} {lang_str:<28} {status}")

    print()
    if missing:
        for e in [e for e in engines if not e["available"]]:
            print(f"  MISSING {e['name']:<14} install: {e['install_hint']}")
        print()
        print(f"  {len(missing)}/{len(engines)} required engine(s) missing -- "
              f"pipeline will exit 1 for affected languages")
    else:
        print(f"  All {len(engines)} required engine(s) available -- pre-flight OK")


def main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    positional = [a for a in argv if not a.startswith("--")]
    project_root = positional[0] if positional else os.getcwd()

    result = check_engines(project_root)

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    return 0 if result["allAvailable"] else 1


if __name__ == "__main__":
    sys.exit(main())

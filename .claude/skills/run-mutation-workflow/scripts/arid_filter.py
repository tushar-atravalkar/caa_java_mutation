# -*- coding: utf-8 -*-
"""
Step 3: Mark lines that produce unproductive mutants.
Replaces the arid-filter LLM skill -- regex + structural, no LLM.
Based on Google's 2022 study on equivalent/unproductive mutants (16.9M mutants).

Polyglot: the pattern set is per-language and lives in `lang_profiles.py`, which
also drives block-comment/docstring tracking so every line of a multi-line
comment is marked, not only the lines that happen to begin with a comment token.
Supported: java/kotlin, python, typescript, javascript, csharp, go. When the
language is not passed it is inferred from the file extension.

Two behaviours worth knowing:

* Arid lines are used to SUPPRESS mutants after the engine runs, not to stop the
  engine generating them -- no mutation tool in the set takes a per-line
  exclusion list. Suppression removes them from BOTH the score's numerator and
  its denominator (see `run.py`), because counting a mutant the pipeline then
  hides from the test-writer produces a gate nobody can pass.
* A missing file is reported, not raised. Step 2 can hand over a path that has
  since been deleted, and one stale path must not abort the whole run.
"""

import json
import re
import sys
from pathlib import Path

try:  # package-relative import -- resolves at any install location
    from .lang_profiles import (
        arid_lines as _arid_lines, get_profile, profile_for_file,
    )
except ImportError:  # direct invocation: py .../arid_filter.py
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from lang_profiles import (  # noqa: E402
            arid_lines as _arid_lines, get_profile, profile_for_file,
        )
    except Exception:  # pragma: no cover - standalone fallback
        _arid_lines, get_profile, profile_for_file = None, None, None


# Extension -> language, used when the caller does not pass one explicitly.
_EXT_LANG = {
    ".java": "java", ".kt": "java",
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".cs": "csharp",
    ".go": "go",
}

# Kept for backwards compatibility: the Java pattern set as compiled regexes.
# New code relies on the per-language patterns resolved via lang_profiles.
ARID_PATTERNS = [
    re.compile(r'\b(log|logger)\.(trace|debug|info|warn|error|fatal)\s*\('),
    re.compile(r'Thread\.sleep\s*\('),
    re.compile(r'^\s*(//|/\*|\*)'),
    re.compile(r'^\s*$'),
    re.compile(r'^\s*@\w+'),
]


# --- equivalent-mutant patterns, language-independent -------------------------
# These produce mutants that are semantically identical to the original, so no
# test can ever kill them.
#
# Each pattern declares WHICH operator families it neutralises, and suppression
# is applied per (line, operator) rather than per line. Blanket line
# suppression is wrong here: `return a.length == 0 ? 0 : n;` contains an
# equivalent relational mutant AND perfectly killable ternary/return mutants,
# so silencing the whole line hides real survivors and inflates the score.
#
# Family -> engine operator names is resolved by `operator_family()` below,
# which covers PIT, Stryker, Stryker.NET, mutmut and gremlins naming.

_EQUIVALENT = [
    (re.compile(r'\.(Count|Length|length|size\(\))\s*[=!]=\s*0'), ("relational",),
     "count/size/length is never negative -- <= 0 is equivalent to == 0"),
    (re.compile(r'\.(isEmpty|IsNullOrEmpty|IsNullOrWhiteSpace)\s*\('), ("relational",),
     "isEmpty is equivalent to size()==0 -- relational mutants behave identically"),
    (re.compile(r'^\s*\w+(\+\+|--)\s*;?\s*$'), ("arithmetic", "update"),
     "pre/post increment are identical as a standalone statement"),
    (re.compile(r'[*/]\s*1\b(?!\d)|[+\-]\s*0\b(?!\d)'), ("arithmetic",),
     "arithmetic identity (x*1, x/1, x+0, x-0) -- mutants are equivalent"),
    (re.compile(r'\|\|\s*false\b|&&\s*true\b|\bor\s+False\b|\band\s+True\b'), ("logical",),
     "boolean identity (x||false, x&&true) -- mutants are equivalent"),
    (re.compile(r'catch\s*[({][^)]*\)?\s*\{?\s*return\s+(null|None|nil)\b'), ("return",),
     "returning null from a catch-only block equals returning the type default"),
]

# Substrings that identify an operator family across every supported engine.
# Matched case-insensitively against the engine's own operator/mutator name.
_OPERATOR_FAMILIES = {
    "relational": ("equality", "relational", "conditionalsboundary", "ror",
                   "negateconditional", "conditional", "comparison"),
    "arithmetic": ("arithmetic", "math", "aor", "increment", "decrement",
                   "operator_replacement_arithmetic", "number"),
    # Deliberately no bare "and"/"or" needles: every engine's names end in
    # "Operator" or "Mutator", both of which contain the letters "or", so a
    # two-letter needle tags MathMutator and EqualityOperator as logical.
    "logical":    ("logical", "lcr", "booleansubstitution", "logicalconnector"),
    "update":     ("update", "increment", "decrement"),
    "return":     ("return", "returnvals", "empty"),
}


def operator_family(operator: str) -> set:
    """The equivalence families an engine operator name belongs to.

    Engines name the same idea differently -- PIT `ConditionalsBoundaryMutator`,
    Stryker `EqualityOperator`, gremlins `CONDITIONALS_BOUNDARY`, mutmut's
    generic label -- so matching is on substrings of the lowercased name."""
    name = (operator or "").lower().replace("_", "")
    families = set()
    for family, needles in _OPERATOR_FAMILIES.items():
        if any(n.replace("_", "") in name for n in needles):
            families.add(family)
    return families


def is_suppressed(line: int, operator: str, arid: set, equivalents: dict) -> bool:
    """True when a mutant at `line` with `operator` should not count.

    `arid` suppresses unconditionally (the line is a comment, a log call, a
    brace). `equivalents` suppresses only mutants whose operator family matches
    the equivalence found on that line; an unrecognised operator is never
    suppressed, so an unknown mutant stays visible rather than being silently
    dropped."""
    if line in arid:
        return True
    families = equivalents.get(line)
    if not families:
        return False
    return bool(operator_family(operator) & set(families))


def _language_for(path: Path, language: str = None) -> str:
    if language:
        return language
    if profile_for_file is not None:
        prof = profile_for_file(path.as_posix())
        if prof is not None:
            return prof.language
    return _EXT_LANG.get(path.suffix.lower(), "java")


def _patterns_for(language: str):
    """Back-compat shim for callers that want the raw compiled pattern list."""
    if get_profile is not None:
        try:
            return get_profile(language).arid_patterns
        except KeyError:
            pass
    return ARID_PATTERNS


def filter_file(file_path: str, language: str = None, strict: bool = False) -> dict:
    """Classify each line of `file_path` as arid or productive.

    Returns a dict with `arid_lines`, `productive_lines`, `arid_pct`,
    `equivalent_lines` and `arid_reasons`. On a missing or unreadable file it
    returns the same shape with `error` set and empty line lists, so one stale
    path from Step 2 degrades to "nothing suppressed here" instead of aborting
    the run. Pass `strict=True` to get the old raise-on-missing behaviour.
    """
    path = Path(file_path)
    lang = _language_for(path, language)

    if not path.exists():
        if strict:
            raise FileNotFoundError(f"File not found: {file_path}")
        return _empty(file_path, lang, "file not found (deleted or moved since Step 2)")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if strict:
            raise
        return _empty(file_path, lang, f"unreadable: {exc}")

    lines = text.splitlines()

    if _arid_lines is not None:
        arid = set(_arid_lines(lines, lang))
    else:  # pragma: no cover - standalone fallback
        patterns = _patterns_for(lang)
        arid = {i for i, line in enumerate(lines, start=1)
                if any(p.search(line) for p in patterns)}

    # Two tiers, deliberately kept apart:
    #   arid          -- nothing on this line is worth mutating, at all
    #   equivalents   -- this line has SOME equivalent mutants, by operator
    reasons = {}
    equivalents = {}
    for i, line in enumerate(lines, start=1):
        if i in arid:
            continue
        for pattern, families, why in _EQUIVALENT:
            if pattern.search(line):
                equivalents[i] = list(families)
                reasons[i] = f"{why} [applies to: {', '.join(families)} mutants only]"
                break
    for i in sorted(arid):
        reasons.setdefault(i, "unproductive (comment, logging, config, boilerplate)")

    productive = [i for i in range(1, len(lines) + 1) if i not in arid]

    return {
        "file": file_path,
        "language": lang,
        "total_lines": len(lines),
        "arid_lines": sorted(arid),
        "equivalent_lines": sorted(equivalents),
        "equivalent_operators": {str(k): v for k, v in sorted(equivalents.items())},
        "productive_lines": productive,
        "arid_reasons": {str(k): v for k, v in sorted(reasons.items())},
        "arid_pct": round(len(arid) / len(lines) * 100, 1) if lines else 0.0,
        "error": None,
    }


def _empty(file_path: str, language: str, error: str) -> dict:
    return {
        "file": file_path,
        "language": language,
        "total_lines": 0,
        "arid_lines": [],
        "equivalent_lines": [],
        "equivalent_operators": {},
        "productive_lines": [],
        "arid_reasons": {},
        "arid_pct": 0.0,
        "error": error,
    }


def filter_files(file_paths, language_by_file: dict = None) -> dict:
    """Run `filter_file` over many paths. Returns {path: result}.

    `language_by_file` lets the orchestrator pass each file's own language in a
    polyglot run instead of relying on extension inference."""
    language_by_file = language_by_file or {}
    return {f: filter_file(f, language_by_file.get(f)) for f in file_paths}


def _main() -> int:
    argv = [a for a in sys.argv[1:]]
    as_json = "--json" in argv
    show_lines = "--lines" in argv
    argv = [a for a in argv if not a.startswith("--")]

    if not argv:
        print("Usage: python arid_filter.py <source_file> [language] [--json] [--lines]")
        return 1

    result = filter_file(argv[0], argv[1] if len(argv) > 1 else None)

    if as_json:
        print(json.dumps(result, indent=2))
        return 1 if result["error"] else 0

    print(f"File          : {result['file']}")
    print(f"Language      : {result['language']}")
    if result["error"]:
        print(f"ERROR         : {result['error']}")
        return 1
    print(f"Total lines   : {result['total_lines']}")
    print(f"Arid lines    : {len(result['arid_lines'])} ({result['arid_pct']}%)  "
          f"-- suppressed entirely")
    print(f"Equivalent    : {len(result['equivalent_lines'])} line(s)  "
          f"-- suppressed only for the matching operator family")
    print(f"Productive    : {len(result['productive_lines'])}")
    if show_lines:
        print("\nLine detail:")
        for lineno in sorted(int(k) for k in result["arid_reasons"]):
            tag = "ARID" if lineno in result["arid_lines"] else "EQUV"
            print(f"  {tag} {lineno:>4}  {result['arid_reasons'][str(lineno)]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

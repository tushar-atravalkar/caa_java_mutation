# Path Audit — CXQA-908460

Fix hardcoded paths: align to skill-relative convention (official Claude plugin
pattern). The skill must work identically when installed at a project's
`.claude/`, under `~/.claude/plugins/<plugin>/`, or from a marketplace checkout.

Verified: `scripts/selftest.py` — 159/159 checks passed, executed from a
directory **outside** the repo to prove location-independence. Both import
modes exercised (package-relative and direct `py <script>` invocation).

## Rules applied

1. **Invocations** use `${CLAUDE_PLUGIN_ROOT:-.claude}/skills/run-mutation-workflow/...`
   — plugin root when installed as a plugin, project-local `.claude/` fallback.
2. **The project root is never derived from a script's own location.**
   Resolution order: `MUTATION_PROJECT_ROOT` env var → `git rev-parse
   --show-toplevel` from the working directory → the working directory.
3. **Python imports are package-relative** (`from .lang_profiles import ...`),
   with a `sys.path` fallback so every script stays directly runnable.
4. **No hardcoded `plugin/` path segments** anywhere.

## Changes by file

### `scripts/run.py`
- **Before:** `PROJECT_ROOT = str(_SKILLS.parent.parent)` — walked up from the
  script's own location, assuming the `.claude/skills/` layout. Installed under
  `~/.claude/plugins/`, this resolved to a directory in the user's home, not
  the repo under test.
- **After:** `PROJECT_ROOT = resolve_project_root()` (shared helper, rule 2).
- Imports converted to package-relative with direct-invocation fallback.

### `scripts/mutation_wizard.py`
- Same `PROJECT_ROOT` fix as `run.py` (was duplicated there).
- Imports (`observability`, `token_report`, `changed_files_scanner`) converted
  to package-relative with fallback.
- `RUN_PY = _HERE / "run.py"` is **unchanged on purpose**: locating a sibling
  script by the current file's directory is skill-relative and correct.

### `scripts/changed_files_scanner.py`
- New shared `resolve_project_root(start=None)` — single implementation of
  rule 2, used by both `run.py` and `mutation_wizard.py`.
- Imports converted to package-relative with fallback (the standalone stub
  fallback for missing `lang_profiles` is preserved).

### `scripts/observability.py`
- **Before:** walked parent directories looking for a hardcoded
  `plugin/agent-orchestrator/observability` sibling; env override was checked
  last.
- **After:** `AGENT_ORCHESTRATOR_OBSERVABILITY_DIR` env var is checked
  **first**; the co-installed discovery walk now looks for
  `agent-orchestrator/observability` under every parent — no hardcoded
  `plugin/` root-folder name (strictly more general: still finds the old
  layout).
- Docstring and the not-found message no longer reference a `plugin/` path.

### `scripts/language_detector.py`, `scripts/arid_filter.py`, `scripts/mutation_runners.py`, `scripts/selftest.py`
- Imports converted to package-relative with direct-invocation fallback.
- `selftest.py`'s `_SKILLS` is **unchanged on purpose**: it locates the skill's
  own assets to copy into test sandboxes — that is genuinely script-relative
  data, not the project root.

### `scripts/__init__.py` (new)
- Makes the directory a package so `from .` imports resolve.

### `agents/unified-mutation-testing.md`
- All 4 command invocations: `py .claude/skills/run-mutation-workflow/scripts/<x>.py`
  → `py "${CLAUDE_PLUGIN_ROOT:-.claude}/skills/run-mutation-workflow/scripts/<x>.py"`.
- Scripts reference table: paths now skill-relative (`scripts/<x>.py`) with a
  note stating the prefix to prepend.
- The relative doc link to the Observability Contract
  (`../../agent-orchestrator/observability/CONTRACT.md`) is a documentation
  pointer, not an invocation; left as-is.

### `commands/run-mutation-workflow.md`
- Invocation `py .claude/skills/run-mutation-workflow/scripts/mutation_wizard.py`
  → `py "${CLAUDE_PLUGIN_ROOT:-.claude}/skills/run-mutation-workflow/scripts/mutation_wizard.py"`
  (the marketplace-breaking `.claude/skills` prefix called out in the story).

## Acceptance criteria mapping

| Criterion | Status |
|---|---|
| No hardcoded `/plugin/` paths | ✅ `observability.py` walk generalized |
| No `.claude/skills/` prefix in invocations | ✅ `${CLAUDE_PLUGIN_ROOT:-.claude}` everywhere |
| All Python imports relative (`from .` imports) | ✅ all 7 cross-importing modules, `__init__.py` added; `sys.path` fallback keeps scripts directly runnable |
| Command invocations use skill-relative paths | ✅ agent definition and command updated |
| Paths work when plugin installed at different locations | ✅ selftest run from outside the repo; package-mode import smoke test; `PROJECT_ROOT` proven cwd-derived |
| audit-paths.md documents all changes | ✅ this file |

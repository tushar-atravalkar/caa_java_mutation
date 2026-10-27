# -*- coding: utf-8 -*-
"""
Best-effort adapter onto the platform's Observability Contract
(the agent-orchestrator plugin's observability/CONTRACT.md, emit.py).

unified-mutation-testing is not dispatched by agent-orchestrator, so every
invocation here is a STANDALONE trace (its own trace_id, its own
`standalone-<trace_id>.jsonl` file) -- Round 1 and a later `--verify` round
are two independent trace files, never stitched into one.

Why "best effort": this plugin can be installed into a target repo on its
own. Whether agent-orchestrator's observability module physically ships
alongside it there is not something this script can verify from inside a
single plugin's own file tree. So every function here degrades to a silent
no-op (after one printed note) instead of raising, if `emit.py` can't be
found. The mutation pipeline's own correctness never depends on this module
succeeding.
"""

import importlib.util
import os
import sys
from pathlib import Path

_EMIT = None
_EMIT_CHECKED = False
_NOTED = False


def _candidate_dirs():
    # Explicit override first: the one signal that works at ANY install
    # location (marketplace plugin, ~/.claude/plugins/, project .claude/).
    override = os.environ.get("AGENT_ORCHESTRATOR_OBSERVABILITY_DIR")
    if override:
        yield Path(override)
    # Co-installed layout: walk up and look for an agent-orchestrator sibling
    # at any level. No hardcoded root-folder name -- checking every parent for
    # `agent-orchestrator/observability` covers plugin/, plugins/, and bare
    # monorepo checkouts alike.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agent-orchestrator" / "observability"
        if candidate.is_dir():
            yield candidate


def _load_emit():
    global _EMIT, _EMIT_CHECKED, _NOTED
    if _EMIT_CHECKED:
        return _EMIT
    _EMIT_CHECKED = True

    for directory in _candidate_dirs():
        emit_path = directory / "emit.py"
        if not emit_path.exists():
            continue
        try:
            # emit.py does `from sanitize import sanitize` -- a same-directory
            # relative import that only resolves if its own folder is on
            # sys.path, same trick run.py already uses for sibling skills.
            sys.path.insert(0, str(directory))
            spec = importlib.util.spec_from_file_location("_agent_orchestrator_emit", emit_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _EMIT = module
            return _EMIT
        except Exception as exc:  # pragma: no cover - defensive only
            if not _NOTED:
                print(f"  (observability contract found but failed to load: {exc})")
                _NOTED = True
            return None

    if not _NOTED:
        print("  (observability contract not found -- skipping event emission; "
              "install the agent-orchestrator plugin or set "
              "AGENT_ORCHESTRATOR_OBSERVABILITY_DIR)")
        _NOTED = True
    return None


def try_start_run(agent, **kwargs):
    """Returns an emit.Span, or None if the contract isn't available."""
    emit = _load_emit()
    if emit is None:
        return None
    try:
        return emit.start_run(agent=agent, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive only
        print(f"  (observability start_run failed: {exc})")
        return None


def try_step(span, name, index):
    if span is None:
        return
    emit = _load_emit()
    if emit is None:
        return
    try:
        emit.step_event(span.agent, span.trace_id, span.span_id, name, index)
    except Exception as exc:  # pragma: no cover - defensive only
        print(f"  (observability step event failed: {exc})")


def try_error(span, err_type, message):
    if span is None:
        return
    emit = _load_emit()
    if emit is None:
        return
    try:
        emit.error_event(span.agent, span.trace_id, span.span_id, err_type, message)
    except Exception as exc:  # pragma: no cover - defensive only
        print(f"  (observability error event failed: {exc})")


def try_end_run(span, **kwargs):
    """Returns the JSONL path written, or None if the contract isn't available."""
    if span is None:
        return None
    emit = _load_emit()
    if emit is None:
        return None
    try:
        return emit.end_run(span, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive only
        print(f"  (observability end_run failed: {exc})")
        return None

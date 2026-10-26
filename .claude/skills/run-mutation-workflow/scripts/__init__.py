# -*- coding: utf-8 -*-
"""Mutation workflow pipeline scripts.

This file makes the directory a package so the sibling modules can use
package-relative imports (``from .lang_profiles import ...``), which resolve
no matter where the skill is installed -- a project's .claude/skills/,
~/.claude/plugins/<plugin>/skills/, or a marketplace checkout.

Every script also remains directly runnable (``py .../run.py``): each module
falls back to a ``sys.path`` shim on its own directory when it is executed
outside package context.
"""

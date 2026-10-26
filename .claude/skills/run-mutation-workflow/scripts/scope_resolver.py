# -*- coding: utf-8 -*-
"""
Scope resolution for the mutation workflow: what does the user's prompt want mutated, and
is the local checkout actually that code?

Two jobs, both deterministic (no LLM, stdlib + git only):

1. `extract`   -- read the user's own words and name the scope: a file, a PR, a commit id, a
                  branch, untracked files, the last commit, or nothing at all. The user never
                  types `--pr` / `--commit`; the caller derives those flags from the prompt.
2. `preflight` -- for a PR, a commit or a branch, check the checkout BEFORE any engine runs:
                  (1) is the local branch the expected one, (2) on the right branch, are the
                  changes the same as the PR/commit, (3) if so run. Cases 1 and 2 return
                  options with one marked recommended; the user decides, never this script.

A third subcommand, `worktree`, carries out the usual recommended option (the PR/commit in a
separate git worktree, leaving the current branch and uncommitted work alone).

    py scope_resolver.py extract   --prompt "mutation test PR 482"          [--root .]
    py scope_resolver.py preflight --kind pr --value 482                    [--root .]
    py scope_resolver.py worktree  --kind commit --value 3fa9c1e            [--root .]

Every subcommand prints one JSON object. Exit codes: 0 ok / scope found, 7 preflight found a
mismatch (the JSON carries the options), 1 unresolvable (bad PR/commit/branch), 2 no scope found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
try:  # package-relative imports -- resolve at any install location
    from .changed_files_scanner import (
        ALL_SOURCE_EXTS, EMPTY_TREE_SHA, _diff_files, _filter_source_files, _git,
    )
except ImportError:  # direct invocation: py .../scope_resolver.py
    sys.path.insert(0, str(_HERE))
    from changed_files_scanner import (  # noqa: E402
        ALL_SOURCE_EXTS, EMPTY_TREE_SHA, _diff_files, _filter_source_files, _git,
    )

EXIT_OK, EXIT_UNRESOLVABLE, EXIT_NO_SCOPE, EXIT_MISMATCH = 0, 1, 2, 7

# First match wins when a prompt names several things ("PR 12 and commit abc1234").
PRECEDENCE = ("file", "pr", "commit", "branch", "untracked", "last-commit", "local")

DEFAULT_BASES = ("origin/main", "origin/master", "main", "master", "origin/develop", "develop")
_BRANCH_WORDS = {"main", "master", "develop", "dev", "trunk", "release"}

NO_SCOPE_MESSAGE = (
    "No scope found in your prompt. Tell me what to mutate: a file path, a PR number or link, a "
    "commit id, a branch, 'untracked files' or 'last commit'. With no answer I will use your "
    "uncommitted + untracked changes (else the last commit)."
)


# --- git helpers ----------------------------------------------------------------

def _out(root, *args) -> str:
    r = _git(str(root), *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def _ok(root, *args) -> bool:
    return _git(str(root), *args).returncode == 0


def _lines(root, *args) -> list:
    return [l.strip() for l in _out(root, *args).splitlines() if l.strip()]


def _refs(root) -> set:
    """Local and remote branch names, with and without the `origin/` prefix."""
    names = set(_lines(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"))
    return names | {n.split("/", 1)[1] for n in names if n.startswith("origin/")}


def _resolve_commit(root, rev):
    """Full SHA for `rev` if it names a commit in this repo, else None."""
    return _out(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}") or None


def _current_branch(root) -> str:
    return _out(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"


def default_base(root):
    """The branch a PR/branch is measured against when nothing names one."""
    sym = _out(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    for cand in ([sym] if sym else []) + list(DEFAULT_BASES):
        if _resolve_commit(root, cand):
            return cand
    return None


# --- 1. extract -----------------------------------------------------------------

_PR_URL = re.compile(r"/(?:pull|pulls|pullrequest|merge_requests)/(\d+)", re.I)
_PR_WORD = re.compile(r"(?<![\w/.-])(?:pull\s*request|pr|mr|merge\s*request)\b[\s:#-]*(?:no\.?|number|id)?[\s:#-]*(\d+)\b", re.I)
_COMMIT_WORD = re.compile(r"\b(?:commit|sha|hash|revision)\b(?:\s+(?:id|hash|sha))?[\s:#=]*([0-9a-f]{4,40})\b", re.I)
_HEX_TOKEN = re.compile(r"(?<![\w./-])([0-9a-f]{7,40})(?![\w./-])", re.I)
_BRANCH_PHRASE = re.compile(r"\bbranch(?:es)?\s+[`'\"]?([\w./-]+)", re.I)
_BASE_PHRASE = re.compile(r"(?:\bagainst|\bvs\.?|\bversus|\bcompared\s+(?:to|with)|\bdiff(?:ed)?\s+(?:to|with|from)|\bbase(?:d)?\s+(?:branch\s+|on\s+)?)\s+[`'\"]?([\w./-]+)", re.I)
_THIS_BRANCH = re.compile(r"\b(?:this|current|my)\s+branch\b", re.I)
_BASE_FLAG = re.compile(r"--base[\s=]+(\S+)")
_LAST_COMMIT = re.compile(r"(?i:\b(?:last|latest|previous|most\s+recent|recent)\s+commit\b)|\bHEAD\b(?!~)")
_UNTRACKED = re.compile(r"\buntracked\b|\bnew\s+files?\b|\bnot\s+(?:yet\s+)?tracked\b", re.I)
_LOCAL = re.compile(r"\b(?:uncommitted|unstaged|working\s+(?:tree|copy)|local\s+changes)\b", re.I)


def _file_candidates(prompt: str) -> list:
    exts = tuple(e.lower() for e in ALL_SOURCE_EXTS)
    out = []
    for tok in re.split(r"[\s,;]+", prompt):
        tok = tok.strip("`'\"()[]<>").rstrip(".:!?")
        if not tok or "://" in tok or tok.startswith("--"):
            continue
        if tok.lower().endswith(exts) and tok not in out:
            out.append(tok)
    return out


def extract(prompt: str, root=".") -> dict:
    """Name the scope the prompt asks for. Result:

    {kind, value, files, flags, resolved, ignored, needsInput, message}

    `flags` are what the caller passes to the mutation wizard for this scope (before
    preflight); `ignored` lists other scopes also named in the prompt (lower precedence)."""
    root = str(root)
    found = {}   # kind -> {"value":..., "source":..., "resolved": bool}

    files = _file_candidates(prompt)
    if files:
        found["file"] = {"value": files, "source": ", ".join(files), "resolved": True}

    pr = _PR_URL.search(prompt) or _PR_WORD.search(prompt)
    if not pr and re.search(r"\b(?:pr|pull)\b", prompt, re.I):
        pr = re.search(r"(?<![\w/])#(\d+)\b", prompt)   # a bare #123 only counts next to the word PR
    if pr:
        found["pr"] = {"value": pr.group(1), "source": pr.group(0).strip(), "resolved": True}

    pr_digits = found["pr"]["value"] if "pr" in found else None
    cm = _COMMIT_WORD.search(prompt)
    if cm and cm.group(1) != pr_digits:
        tok = cm.group(1)
        sha = _resolve_commit(root, tok)
        # "commit added" is prose, not an id: an unknown token only counts if it looks like a sha.
        if sha or (len(tok) >= 7 and re.search(r"\d", tok)):
            found["commit"] = {"value": sha or tok, "source": cm.group(0).strip(), "resolved": bool(sha)}
    if "commit" not in found:
        for m in _HEX_TOKEN.finditer(prompt):
            tok = m.group(1)
            # Bare hex is only a commit if it has a digit AND a letter and git knows it.
            if tok != pr_digits and re.search(r"\d", tok) and re.search(r"[a-f]", tok, re.I):
                sha = _resolve_commit(root, tok)
                if sha:
                    found["commit"] = {"value": sha, "source": tok, "resolved": True}
                    break

    refs = _refs(root)
    flag = _BASE_FLAG.search(prompt)
    against = _BASE_PHRASE.search(prompt)
    named = _BRANCH_PHRASE.search(prompt)
    if flag or (against and against.group(1) in refs):
        m = flag or against
        found["branch"] = {"value": m.group(1), "role": "base", "source": m.group(0).strip(),
                           "resolved": m.group(1) in refs}
    elif named and (named.group(1) in refs or re.search(r"[/_\d-]", named.group(1))):
        # "branch changes" is prose; "branch feature/x" names one.
        found["branch"] = {"value": named.group(1), "role": "target", "source": named.group(0).strip(),
                           "resolved": named.group(1) in refs}
    elif _THIS_BRANCH.search(prompt):
        found["branch"] = {"value": _current_branch(root), "role": "target",
                           "source": _THIS_BRANCH.search(prompt).group(0), "resolved": True}
    else:
        for tok in re.split(r"[\s,;]+", prompt):
            tok = tok.strip("`'\"()[]<>").rstrip(".:!?")
            if tok in refs and (tok in _BRANCH_WORDS or re.search(r"[/_\d-]", tok)):
                found["branch"] = {"value": tok, "role": "target", "source": tok, "resolved": True}
                break

    if _UNTRACKED.search(prompt):
        found["untracked"] = {"value": None, "source": _UNTRACKED.search(prompt).group(0), "resolved": True}
    if _LAST_COMMIT.search(prompt):
        found["last-commit"] = {"value": "HEAD", "source": _LAST_COMMIT.search(prompt).group(0), "resolved": True}
    if _LOCAL.search(prompt):
        found["local"] = {"value": None, "source": _LOCAL.search(prompt).group(0), "resolved": True}

    kind = next((k for k in PRECEDENCE if k in found), None)
    if kind is None:
        return {"kind": "none", "value": None, "files": [], "flags": [], "resolved": True,
                "ignored": [], "needsInput": True, "message": NO_SCOPE_MESSAGE}

    hit = found[kind]
    ignored = [{"kind": k, "source": found[k]["source"]} for k in PRECEDENCE if k in found and k != kind]
    res = {"kind": kind, "value": hit["value"], "source": hit["source"], "files": [],
           "flags": [], "resolved": hit["resolved"], "ignored": ignored,
           "needsInput": False, "message": ""}

    if kind == "file":
        res["files"] = hit["value"]
        res["value"] = hit["value"]
        res["flags"] = [x for f in hit["value"] for x in ("--file", f)]
        res["resolved"] = all((Path(root) / f).exists() or Path(f).exists() for f in hit["value"])
    elif kind == "pr":
        res["flags"] = ["--pr", hit["value"]]
    elif kind == "commit":
        res["flags"] = ["--commit", hit["value"]]
    elif kind == "last-commit":
        res["flags"] = ["--commit", "HEAD"]
    elif kind == "branch":
        res["role"] = hit["role"]
        if hit["role"] == "base":
            res["flags"] = ["--base", hit["value"]]
        # a branch named as the target has no flags until preflight: that decides whether this
        # checkout is it, and what it is measured against
    elif kind == "untracked":
        res["files"] = _lines(root, "ls-files", "--others", "--exclude-standard")
        src, _ = _filter_source_files(res["files"], root)
        res["files"] = src
        res["flags"] = [x for f in src for x in ("--file", f)]
        if not src:
            res["message"] = "There are no untracked source files."
    if not res["resolved"] and kind in ("commit", "branch", "file") and not res["message"]:
        res["message"] = f"{kind} '{res['value']}' was named in the prompt but is not in this repository."
    return res


# --- 2. preflight ---------------------------------------------------------------

def _pr_from_gh(root, number):
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        r = subprocess.run(
            [gh, "pr", "view", str(number), "--json", "headRefName,headRefOid,baseRefName,state"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout)
        return {"headSha": d["headRefOid"], "headBranch": d.get("headRefName"),
                "baseName": d.get("baseRefName"), "state": d.get("state"), "source": "gh"}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _net(root, *args):
    """A git command that talks to the remote: never prompts for credentials, never hangs."""
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 1, "", "git remote call failed or timed out")


def _pr_from_git(root, number):
    """`refs/pull/N/head` -- GitHub's (and Gitea's) read-only ref for a PR's head. No branch name."""
    r = _net(root, "ls-remote", "origin", f"refs/pull/{number}/head")
    sha = r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None
    return {"headSha": sha, "headBranch": None, "baseName": None, "state": None, "source": "git"} if sha else None


def resolve_pr(root, number) -> dict:
    """{headSha, headBranch, baseRef, mergeBase, source} for PR `number`, or {"error": ...}.
    Fetches the PR head into the object store (FETCH_HEAD only: no branch, no checkout)."""
    info = _pr_from_gh(root, number) or _pr_from_git(root, number)
    if not info:
        return {"error": f"PR {number} could not be found: `gh` is unavailable or not logged in, and "
                         f"the 'origin' remote has no refs/pull/{number}/head."}
    sha = info["headSha"]
    if not _resolve_commit(root, sha) and _net(root, "fetch", "--quiet", "origin", f"refs/pull/{number}/head").returncode != 0:
        return {"error": f"PR {number} head {sha[:9]} is not in the local repository and could not be fetched."}
    base = None
    if info.get("baseName"):
        base = next((c for c in (f"origin/{info['baseName']}", info["baseName"]) if _resolve_commit(root, c)), None)
    base = base or default_base(root)
    if not base:
        return {"error": f"Cannot tell which branch PR {number} targets, and no main/master/develop exists locally."}
    mb = _out(root, "merge-base", base, sha)
    if not mb:
        return {"error": f"No merge base between {base} and PR {number} head {sha[:9]}."}
    return {**info, "baseRef": base, "mergeBase": mb}


def _range_for(root, kind, value):
    """The change set of a PR or commit: {from, to, expectedBranch, baseRef, label, detail, scope}, or {"error"}."""
    if kind == "pr":
        pr = resolve_pr(root, value)
        if "error" in pr:
            return pr
        return {"from": pr["mergeBase"], "to": pr["headSha"], "expectedBranch": pr.get("headBranch"),
                "baseRef": pr["baseRef"], "label": f"PR #{value}",
                "detail": f"PR #{value} {pr['mergeBase'][:9]}..{pr['headSha'][:9]} (base {pr['baseRef']})",
                "scope": "pr"}
    sha = _resolve_commit(root, value)
    if not sha:
        return {"error": f"'{value}' is not a commit in this repository."}
    parent = _resolve_commit(root, f"{sha}^") or EMPTY_TREE_SHA
    subject = _out(root, "log", "-1", "--format=%h %s", sha)
    return {"from": parent, "to": sha, "expectedBranch": None, "baseRef": parent,
            "label": f"commit {sha[:9]}", "detail": subject or sha[:9], "scope": "commit"}


def _opt(oid, label, why, recommended=False, commands=None, flags=None):
    return {"id": oid, "label": label, "why": why, "recommended": recommended,
            "commands": commands or [], "flags": flags or []}


def preflight(root, kind, value) -> dict:
    """Check the checkout against a PR / commit / branch. `status` is one of:

    ok               HEAD is on the expected branch and holds the same changes -> run.
    wrong-branch     the local branch is not the expected one.
    changes-differ   right branch, but the local changes are not the PR/commit's.
    unresolvable     the PR / commit / branch cannot be found (`problems` says why).
    """
    root = str(root)
    if kind == "last-commit":
        kind, value = "commit", "HEAD"
    out = {"kind": kind, "value": value, "status": "ok", "problems": [], "options": [],
           "recommended": None, "files": [], "flags": [], "currentBranch": _current_branch(root)}
    if kind == "branch":
        return _preflight_branch(root, value, out)

    rng = _range_for(root, kind, value)
    if "error" in rng:
        return {**out, "status": "unresolvable", "problems": [rng["error"]]}
    sha, frm = rng["to"], rng["from"]
    head = _out(root, "rev-parse", "HEAD")
    current = out["currentBranch"]
    contains = _ok(root, "merge-base", "--is-ancestor", sha, "HEAD")
    expected = rng["expectedBranch"]
    raw = _diff_files(root, frm, sha)
    files, _skipped = _filter_source_files(raw, root)
    out.update(headSha=sha, expectedBranch=expected, baseRef=rng["baseRef"], fromRef=frm,
               label=rng["label"], detail=rng["detail"], scope=rng["scope"], files=files)

    if expected is not None:
        wrong = current != expected and head != sha
    else:
        # No branch name to compare (git-only PR lookup, or a commit): HEAD is on the right line of
        # history if it contains the target, or is an ancestor of it that is not just the base.
        behind_same_line = (_ok(root, "merge-base", "--is-ancestor", "HEAD", sha)
                            and not _ok(root, "merge-base", "--is-ancestor", "HEAD", frm))
        wrong = not (contains or behind_same_line)
    ahead = behind = 0
    counts = _out(root, "rev-list", "--left-right", "--count", f"HEAD...{sha}").split()
    if len(counts) == 2:
        ahead, behind = int(counts[0]), int(counts[1])
    dirty = _lines(root, "diff", "--name-only", sha, "--", *raw) if raw else []
    out.update(ahead=ahead, behind=behind, modifiedFiles=dirty)

    wt = _opt("worktree", f"Run {rng['label']} in a separate git worktree",
              "Checks out exactly that code beside this repo; your branch and uncommitted work are untouched.",
              commands=[f"py scope_resolver.py worktree --kind {kind} --value {value}"], flags=["--" + kind, str(value)])
    abort = _opt("abort", "Stop; I will fix the checkout myself", "Nothing is run or changed.")

    if wrong:
        why = (f"you are on '{current}' but {rng['label']} lives on '{expected}'" if expected
               else f"{rng['label']} is not part of the branch you are on ('{current}')")
        out["status"] = "wrong-branch"
        out["problems"].append(why)
        switch = (f"gh pr checkout {value}" if kind == "pr" and rng.get("expectedBranch") else
                  f"git switch --detach {sha}")
        dirty_now = bool(_lines(root, "status", "--porcelain", "--untracked-files=no"))
        wt["recommended"] = True
        out["options"] = [
            wt,
            _opt("checkout", f"Switch this working copy to {rng['label']}",
                 "Runs where you work, but replaces your current checkout"
                 + (" - you have uncommitted changes, commit or stash them first." if dirty_now else "."),
                 commands=[switch], flags=["--" + kind, str(value)]),
            abort]
    else:
        notes = []
        if not contains:
            notes.append(f"local '{current}' does not contain {rng['label']} head {sha[:9]} "
                         f"({behind} commit(s) behind, {ahead} ahead)")
        if dirty:
            notes.append(f"{len(dirty)} file(s) of the change differ from {sha[:9]} on disk: " + ", ".join(dirty[:5]))
        if notes:
            out["status"] = "changes-differ"
            out["problems"] = notes
            clean = not _lines(root, "status", "--porcelain", "--untracked-files=no")
            opts = []
            if not contains and ahead == 0 and clean:
                opts.append(_opt("sync", f"Fast-forward '{current}' to {rng['label']}",
                                 "Your branch is only behind and has no local edits, so this loses nothing.",
                                 recommended=True, commands=["git pull --ff-only"], flags=["--" + kind, str(value)]))
            wt["recommended"] = not opts
            opts.append(wt)
            opts.append(_opt("local-as-is", "Mutate my local code as it is now",
                             f"Scope becomes everything your checkout changed since {rng['baseRef'][:12]}; the score "
                             f"describes your local code, not {rng['label']} exactly.",
                             flags=["--base", rng["from"]]))
            opts.append(abort)
            out["options"] = opts
    out["recommended"] = next((o["id"] for o in out["options"] if o["recommended"]), None)
    out["flags"] = ["--" + kind, str(value)] if out["status"] == "ok" else []
    return out


def _preflight_branch(root, name, out) -> dict:
    refs = _refs(root)
    if name not in refs:
        return {**out, "status": "unresolvable", "problems": [f"branch '{name}' does not exist locally or on origin."]}
    base = default_base(root)
    current = out["currentBranch"]
    out.update(expectedBranch=name, baseRef=base)
    if current == name or name == base or name.replace("origin/", "") == current:
        out["flags"] = ["--base", base] if (base and current == name) else []
        return out
    out["status"] = "wrong-branch"
    out["problems"].append(f"you are on '{current}' but asked for branch '{name}'")
    out["options"] = [
        _opt("worktree", f"Run branch '{name}' in a separate git worktree",
             "Checks out that branch beside this repo; your branch and uncommitted work are untouched.",
             recommended=True, commands=[f"py scope_resolver.py worktree --kind branch --value {name}"],
             flags=["--base", base or ""]),
        _opt("checkout", f"Switch this working copy to '{name}'", "Replaces your current checkout.",
             commands=[f"git switch {name}"], flags=["--base", base or ""]),
        _opt("abort", "Stop; I will fix the checkout myself", "Nothing is run or changed."),
    ]
    out["recommended"] = "worktree"
    return out


def describe(pre: dict) -> str:
    """The user-facing text for a failed preflight."""
    if pre["status"] == "ok":
        return ""
    head = {"wrong-branch": "WRONG BRANCH", "changes-differ": "LOCAL CHANGES DIFFER",
            "unresolvable": "SCOPE NOT FOUND"}[pre["status"]]
    lines = [f"{head}: " + "; ".join(pre["problems"])]
    for o in pre["options"]:
        lines.append(f"  [{o['id']}]{' (recommended)' if o['recommended'] else ''} {o['label']} - {o['why']}")
    return "\n".join(lines)


# --- worktree ---------------------------------------------------------------------

def make_worktree(root, kind, value) -> dict:
    """Detached worktree of the PR head / commit / branch, next to the repo. Returns the directory
    to run in (the project root inside it when `root` is below the git toplevel)."""
    root = Path(root).resolve()
    top = Path(_out(root, "rev-parse", "--show-toplevel") or str(root)).resolve()
    if kind == "branch":
        rev = next((c for c in (value, f"origin/{value}") if _resolve_commit(str(top), c)), None)
        sha = _resolve_commit(str(top), rev) if rev else None
    else:
        rng = _range_for(str(root), kind, value)
        if "error" in rng:
            return {"error": rng["error"]}
        sha = rng["to"]
    if not sha:
        return {"error": f"cannot resolve {kind} '{value}'"}
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{kind}-{value}").strip("-")[:40]
    path = top.parent / f"{top.name}-mutation-{slug}"
    if path.exists():
        return {"error": f"{path} already exists; remove it with `git worktree remove {path}` or reuse it."}
    r = _git(str(top), "worktree", "add", "--detach", str(path), sha)
    if r.returncode != 0:
        return {"error": f"git worktree add failed: {r.stderr.strip()}"}
    try:
        rel = root.relative_to(top)
    except ValueError:
        rel = Path(".")
    run_dir = path / rel
    return {"path": str(path), "runDir": str(run_dir), "headSha": sha,
            "next": f"cd {run_dir}  then run the workflow with the same scope flags",
            "cleanup": f"git worktree remove {path}"}


# --- CLI ------------------------------------------------------------------------

def _emit(obj) -> None:
    print(json.dumps(obj, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="name the scope in a prompt")
    e.add_argument("--prompt", required=True)
    e.add_argument("--root", default=".")
    p = sub.add_parser("preflight", help="check the checkout against a PR/commit/branch")
    p.add_argument("--kind", choices=("pr", "commit", "branch", "last-commit"), required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--root", default=".")
    w = sub.add_parser("worktree", help="check out a PR/commit/branch in a separate worktree")
    w.add_argument("--kind", choices=("pr", "commit", "branch"), required=True)
    w.add_argument("--value", required=True)
    w.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    if args.cmd == "extract":
        res = extract(args.prompt, args.root)
        _emit(res)
        return EXIT_NO_SCOPE if res["kind"] == "none" else (EXIT_OK if res["resolved"] else EXIT_UNRESOLVABLE)
    if args.cmd == "preflight":
        res = preflight(args.root, args.kind, args.value)
        res["message"] = describe(res)
        _emit(res)
        return {"ok": EXIT_OK, "unresolvable": EXIT_UNRESOLVABLE}.get(res["status"], EXIT_MISMATCH)
    res = make_worktree(args.root, args.kind, args.value)
    _emit(res)
    return EXIT_UNRESOLVABLE if "error" in res else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

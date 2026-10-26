# -*- coding: utf-8 -*-
"""
Shared, plugin-agnostic Jira updater. Any agent/skill in this repo that wants
to record adoption on a Jira issue shells out to this script instead of
reinventing REST calls, auth or retry logic.

Stable CLI contract (do not break -- internals may change freely):

    python jira_updater.py --issue KEY --label LABEL [--comment-file PATH]
                            [--field k=v ...] [--validate-only]

Auth (env vars, read once, shared by every caller):
    JIRA_BASE_URL   e.g. https://yourteam.atlassian.net       (required)
    JIRA_EMAIL      Atlassian account email                    \
    JIRA_API_TOKEN  API token for that account                 } basic auth
    ATLASSIAN_AUTH_TOKEN  bearer token, used if the pair above is absent

Two very different failure policies, on purpose:
  --validate-only  is a pre-flight gate. It is allowed to fail loudly
                   (non-zero exit) -- that is the whole point of validating
                   a supplied ID before the caller's real work starts.
  applying a label/comment/field (the default mode) is a best-effort side
                   effect on completion. It NEVER raises past this script and
                   ALWAYS exits 0 -- a Jira outage, a permissions error or a
                   rate limit must never fail the caller's real job. Every
                   failure is logged (stderr, and --log-file if given) and
                   swallowed.

No third-party dependencies -- stdlib only (urllib), matching the rest of
this repo's tooling.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class JiraAuthError(Exception):
    pass


def _auth_header() -> tuple[str, str]:
    """Returns (header_name, header_value). Raises JiraAuthError if unconfigured."""
    base_url = os.environ.get("JIRA_BASE_URL")
    if not base_url:
        raise JiraAuthError("JIRA_BASE_URL is not set")

    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if email and token:
        basic = b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        return "Authorization", f"Basic {basic}"

    bearer = os.environ.get("ATLASSIAN_AUTH_TOKEN")
    if bearer:
        return "Authorization", f"Bearer {bearer}"

    raise JiraAuthError(
        "no Jira credentials found -- set JIRA_EMAIL + JIRA_API_TOKEN, "
        "or ATLASSIAN_AUTH_TOKEN"
    )


def _base_url() -> str:
    return os.environ["JIRA_BASE_URL"].rstrip("/")


def _log(message: str, log_file: str = None) -> None:
    line = f"[jira-updater] {message}"
    print(line, file=sys.stderr)
    if log_file:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "message": message}
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # logging must never itself be fatal


def _request(method: str, path: str, body: dict = None) -> tuple[int, str]:
    """One HTTP call with retry/backoff on 429/5xx. Returns (status, text).
    Raises JiraAuthError or urllib.error.URLError for non-retryable failures."""
    header_name, header_value = _auth_header()
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None

    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header(header_name, header_value)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
                last_exc = exc
                continue
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            # DNS/connection-level outage -- retry the same way as a 5xx.
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
                last_exc = exc
                continue
            raise
    raise last_exc


def validate_issue(issue_key: str) -> tuple[bool, str]:
    """Pre-flight check: does this issue exist and is it readable?
    Returns (ok, reason)."""
    try:
        status, _ = _request("GET", f"/rest/api/2/issue/{issue_key}?fields=id")
    except JiraAuthError as exc:
        return False, f"auth not configured: {exc}"
    except urllib.error.URLError as exc:
        return False, f"could not reach Jira: {exc}"

    if status == 200:
        return True, "ok"
    if status == 404:
        return False, f"{issue_key} does not exist"
    if status in (401, 403):
        return False, f"not permitted to read {issue_key} (HTTP {status})"
    return False, f"unexpected response (HTTP {status})"


def apply_update(issue_key: str, label: str, fields: dict, log_file: str = None) -> None:
    """Best-effort: add a label and any extra fields. Never raises."""
    update = {"labels": [{"add": label}]} if label else {}
    payload = {}
    if update:
        payload["update"] = update
    if fields:
        payload["fields"] = fields
    if not payload:
        return

    try:
        status, text = _request("PUT", f"/rest/api/2/issue/{issue_key}", payload)
        if status not in (200, 204):
            _log(f"label/field update on {issue_key} returned HTTP {status}: {text}",
                 log_file)
        else:
            _log(f"applied label '{label}' to {issue_key}", log_file)
    except JiraAuthError as exc:
        _log(f"skipping label update on {issue_key} -- {exc}", log_file)
    except urllib.error.URLError as exc:
        _log(f"skipping label update on {issue_key} -- Jira unreachable: {exc}", log_file)
    except Exception as exc:  # belt-and-suspenders: this call must never propagate
        _log(f"skipping label update on {issue_key} -- unexpected error: {exc}", log_file)


def apply_comment(issue_key: str, comment_text: str, log_file: str = None) -> None:
    """Best-effort: add a comment. Never raises."""
    if not comment_text:
        return
    try:
        status, text = _request(
            "POST", f"/rest/api/2/issue/{issue_key}/comment", {"body": comment_text}
        )
        if status not in (200, 201):
            _log(f"comment on {issue_key} returned HTTP {status}: {text}", log_file)
        else:
            _log(f"added comment to {issue_key}", log_file)
    except JiraAuthError as exc:
        _log(f"skipping comment on {issue_key} -- {exc}", log_file)
    except urllib.error.URLError as exc:
        _log(f"skipping comment on {issue_key} -- Jira unreachable: {exc}", log_file)
    except Exception as exc:
        _log(f"skipping comment on {issue_key} -- unexpected error: {exc}", log_file)


def _label_from_config(config_path: str) -> str:
    """Reads {"label": "..."} from a caller's own jira.config.json so a
    caller never has to parse JSON itself -- it just passes --config."""
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("label")
    except (OSError, ValueError) as exc:
        _log(f"could not read --config {config_path}: {exc}")
        return None


def _parse_fields(pairs: list) -> dict:
    fields = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--field must be k=v, got: {pair!r}")
        key, value = pair.split("=", 1)
        fields[key] = value
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared Jira adoption/update CLI")
    parser.add_argument("--issue", required=True, help="Jira issue key, e.g. PROJ-1234")
    parser.add_argument("--label", default=None, help="Label to add on completion")
    parser.add_argument("--config", default=None,
                        help="Path to a jira.config.json ({\"label\": \"...\"}). "
                             "Used only when --label is not given directly -- "
                             "lets every caller keep its own label as config, "
                             "not code, without parsing JSON itself.")
    parser.add_argument("--comment-file", default=None,
                        help="Path to a text file whose contents become a comment")
    parser.add_argument("--field", action="append", default=[],
                        help="Extra field update as k=v. Repeatable.")
    parser.add_argument("--validate-only", action="store_true",
                        help="Only check the issue exists/is readable, then exit. "
                             "This is the one mode allowed to exit non-zero.")
    parser.add_argument("--log-file", default=None,
                        help="Optional path to append structured log lines to")
    args = parser.parse_args()

    if args.validate_only:
        ok, reason = validate_issue(args.issue)
        if ok:
            print(f"{args.issue}: valid")
            sys.exit(0)
        print(f"{args.issue}: invalid -- {reason}", file=sys.stderr)
        sys.exit(1)

    try:
        fields = _parse_fields(args.field)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(0)  # even a bad --field must not block the caller

    label = args.label or (_label_from_config(args.config) if args.config else None)

    comment_text = None
    if args.comment_file:
        try:
            with open(args.comment_file, "r", encoding="utf-8") as fh:
                comment_text = fh.read()
        except OSError as exc:
            _log(f"could not read --comment-file {args.comment_file}: {exc}", args.log_file)

    apply_update(args.issue, label, fields, args.log_file)
    apply_comment(args.issue, comment_text, args.log_file)
    sys.exit(0)  # always -- see module docstring


if __name__ == "__main__":
    main()

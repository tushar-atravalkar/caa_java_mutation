# jira-updater

Shared, plugin-agnostic script for recording adoption on a Jira issue.
Any agent/skill in this repo that wants "capture an issue ID, label it on
completion" behavior shells out to this one script instead of writing its
own Jira REST client.

## CLI contract (stable)

```
python jira_updater.py --issue KEY --config PATH [--comment-file PATH] [--field k=v ...]
python jira_updater.py --issue KEY --label LABEL [--comment-file PATH] [--field k=v ...]
python jira_updater.py --issue KEY --validate-only
```

Internals (retry policy, which Jira endpoints are called, HTTP client) can
change at any time. This flag set and these exit-code semantics cannot,
without updating every caller.

| Flag | Required | Notes |
|---|---|---|
| `--issue` | yes | Jira issue key, e.g. `PROJ-1234` |
| `--label` | yes, unless `--config` or `--validate-only` | Label added via `update.labels.add` |
| `--config` | yes, unless `--label` or `--validate-only` | Path to caller's own `jira.config.json`; this script reads `{"label": "..."}` from it so the caller never parses JSON itself |
| `--comment-file` | no | Path to a text file; its contents become a comment |
| `--field k=v` | no, repeatable | Extra field updates, e.g. `--field customfield_1001=foo` |
| `--validate-only` | no | Pre-flight existence/permission check only; no label/comment applied |
| `--log-file` | no | Append structured JSON log lines here in addition to stderr |

## Exit codes

- **Default mode (applying a label/comment):** always exits `0`. A Jira
  outage, missing permissions, or a rate limit is logged and swallowed —
  labeling is a side effect on completion, never a reason to fail the
  caller's real job.
- **`--validate-only`:** exits `0` if the issue exists and is readable,
  `1` otherwise. This is the one mode allowed to fail loudly, since its
  entire purpose is gating the caller's execution on a valid ID.

## Auth (shared across every caller)

Environment variables, read once:

```
JIRA_BASE_URL   https://yourteam.atlassian.net       (required)
JIRA_EMAIL      Atlassian account email               \
JIRA_API_TOKEN  API token for that account             } basic auth, preferred
ATLASSIAN_AUTH_TOKEN   bearer token, used if the pair above is absent
```

No per-plugin auth reinvention — every caller in the repo points at the
same env vars.

## Label-per-plugin convention

The label a caller applies is **config, not code**. Each caller keeps its
own `jira.config.json` next to its own agent/skill definition, e.g.:

```json
{ "label": "mutation-agent-adopted" }
```

The caller passes `--config <path to its own jira.config.json>` and this
script reads the label out of it — no JSON parsing, no label-lookup
function, on the caller's side. Adding Jira-adoption tracking to a new
agent costs: one `jira.config.json` file next to it + one plain
`subprocess.run([...jira_updater.py..., "--issue", id, "--config", cfg])`
call at its completion point, and one `--validate-only` call before real
work starts if the ID should gate execution. No new auth, no new retry
logic, no new tests, no per-agent helper functions — those all already
live here.

## Non-blocking by design

Everything except `--validate-only` is best-effort. A caller invokes this
script for its side effect and moves on regardless of what it printed —
never gate real work on this script's success.

# AresClaw dashboard CLI

This directory contains the dependency-free Python 3 command used by AresClaw
agents and controlled integration jobs. It implements the dashboard service's
`/api/v1` HTTP contract. It is a client, not an authentication or authorization
boundary.

## Modes

Human mode is the default. The existing runtime environment pre-writes the
current user's W3 token to `/root/.config/auth_token`; every CLI invocation
loads that file fresh and calls the fixed dashboard service directly with
`Authorization: Bearer` and `X-Dashboard-Auth-Mode: human`. The CLI never
writes, refreshes, or echoes the token. The service URL and workdir come
from `ARESCLAW_DASHBOARD_SERVICE_URL` / `ARESCLAW_DASHBOARD_WORKDIR`
(or an optional `--config` JSON). Writes first verify the signed-in
principal via `/me`; frozen request snapshots are bound to the service
origin, the verified `principal_id`, and the request id, so a renewed token
of the same user can resume an operation while a different user cannot.

Integration mode directly calls a fixed service origin with a service-account
JWT (requires `--config`; `token_file` is mandatory):

```json
{
  "service_url": "https://dashboards.intranet.example",
  "workdir": "/srv/dashboard-job",
  "token_file": "/run/secrets/dashboard.jwt",
  "timeout_seconds": 30
}
```

```sh
dashboard --auth-mode integration --config /etc/aresclaw/dashboard.json list
```

`service_url` must be an HTTP(S) origin with no path, credentials, query, or
fragment. `workdir` and `token_file` are the only file roots/credential source
used by this mode. Environment proxies and redirects are disabled. Publish,
access-change, and source paths must be relative to `workdir`; path traversal,
symbolic links/reparse points, and source overwrite are rejected.

Integration publish and `access-apply` store request-scoped frozen data under
`<workdir>/.aresclaw-dashboard/requests`. Keep this directory across retries
for as long as the service retains idempotency records. It contains dashboard
content or permission-change data, but never the JWT.

## Workflow

There are two independent references: the live published version and the current
draft. `save` never changes what existing viewers see. `publish --file` uploads and
publishes atomically; without a file, `publish` requires the exact dashboard ID,
draft version ID and expected revision. Dashboard IDs and viewing links do not
change across updates. `restore` returns to draft, not directly to published.

```sh
dashboard create --title "Weekly report" --request-id <create-uuid>
dashboard save --file report.html --dashboard-id <id> --expected-revision 1 --request-id <save-uuid>
dashboard publish --dashboard-id <id> --version-id <draft-id> --expected-revision 2 --request-id <publish-uuid>
dashboard publish --file report.html --dashboard-id <id> --expected-revision 3 --request-id <update-uuid>
```

The placeholders above must be replaced by actual IDs from results. Use a new
UUID for a new action; retry the same action with its original UUID and arguments.
Updating existing HTML does not require resending title/description: omit to
preserve them, or supply an empty description explicitly to clear it. New
dashboard creation with a file requires a title. `list --status draft` lists
drafts the current caller can access.

```sh
request_id="$(dashboard new-request-id | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')"
dashboard publish --file report.html --title "Weekly report" --request-id "$request_id"
dashboard operation --request-id "$request_id"
```

Every write requires `--request-id`. Existing-dashboard changes also require
`--expected-revision`. `publish --dashboard-id ...` must include that
revision. Grant and public-access dates accept absolute RFC3339 timestamps only.

The command families are:

- `list`, `show`, `source`, `create`, `save`, `publish`, and `operation`
- `rename`, `versions`, `rollback`, `archive`, and `restore`
- `principals`, `grants`, `share`, `revoke`, `public`, `access`, and
  `access-apply`
- `group list`, `group create`, and `group member add|remove`

Run `dashboard <command> --help` for flags. Group member changes read the
current member set once, verify the supplied group revision, freeze the final
replacement request under the request ID, and update it with CAS. A retry of
the same command replays the frozen member list and revision — it never
re-reads the group, so a lost response recovers the recorded outcome instead
of failing on the revision the first success bumped.

## Output and exit codes

Business results and errors are one JSON object on stdout. Diagnostics use
stderr and never contain the loaded credential. Exit codes follow
[the canonical contracts, section 8](C:/Users/ouyan/Documents/code/acpdev/codeg/docs/aresclaw-dashboard/contracts.md):

| Code | Meaning |
| ---: | --- |
| 0 | Succeeded (including a replayed recorded success) |
| 2 | Invalid input |
| 3 | Authentication required, expired, or rejected |
| 4 | Forbidden or not visible |
| 5 | Conflict (revision, quota, idempotency key) |
| 6 | Accepted/pending — query again with the same request ID |
| 7 | Write outcome unknown; query the original request ID |
| 8 | Network failure on a read |
| 9 | Other service/protocol failure, including an operation that ended in
  `state=failed` (mapped by its recorded error code when recognizable) |

An `operation` query answering HTTP 200 with `state=failed` is NOT success:
the CLI exits 3/4/5/9 by the recorded error and keeps the `request_id` in the
output for recovery.

The CLI never follows redirects and never accepts `--token`, an actor identity,
or an arbitrary service URL. A timeout or transport break during a write returns
`outcome_unknown` and the original `idempotency_key`.

## Frozen requests and recovery

File uploads, access-change files and computed group replacements are frozen locally under
`.aresclaw-dashboard/requests/<origin-hash>/<principal_id>/` before the first
attempt, keyed by request ID and bound to the verified principal:

- Retries of a save, publish or access-apply replay the frozen bytes even after the
  source file is edited, moved, or deleted; the original file is read only
  when a snapshot is first created.
- The same user with a renewed token resumes their own snapshots; a different
  user gets a separate namespace and never continues another user's request.
- Snapshots contain no credentials.
- Request UUIDs are normalized before credential loading or snapshot selection;
  malformed IDs are rejected without sending a request. All snapshot path
  components reject symbolic links and Windows directory junctions.
- A save and a publish cannot reuse an upload request ID: disposition is frozen.
  Plain JSON commands use the supplied immutable arguments; keep them unchanged
  when retrying. Unknown operation states are protocol failures, never success.

## Tests

```sh
python3 -m unittest discover -s integrations/aresclaw-dashboard/tests -v
```

Tests use a real temporary HTTP server and subprocess CLI invocations. No
third-party Python packages are required.

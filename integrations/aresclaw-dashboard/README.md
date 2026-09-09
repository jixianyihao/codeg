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

## Read, compare, and update source

`list` and `show` return metadata only: the dashboard ID and revision, plus
separate `current_version_id` / `draft_version_id`, `*_version_sha256`, and
`*_version_byte_size` fields. Listing does not fetch HTML or read each source
object. Missing versions have null metadata; an inaccessible draft stays hidden.

```sh
dashboard list --scope mine
dashboard show <dashboard-id>
dashboard source <dashboard-id> --output downloads/current.html
dashboard source <dashboard-id> --version draft --output downloads/draft.html
dashboard source <dashboard-id> --version-id <version-id-from-list> --expected-sha256 <sha256-from-list> --output downloads/baseline.html
```

`source --version current` is the default when `--version-id` is absent.
`--version current|draft` and `--version-id` are mutually exclusive. A selector
reads `show` once, pins its version ID, SHA-256, byte size and revision, then
downloads that exact version even if another user publishes meanwhile. A missing
or hidden selected pointer exits 4 without requesting source; draft selection
never falls back to the live version. For a snapshot already selected from
`list`, use its explicit version ID and digest as in the last command: this avoids
a second metadata read and preserves the original comparison target.

`--expected-sha256` must be exactly 64 lowercase hexadecimal characters; uppercase,
whitespace and malformed values are rejected before credential loading or any
network request. The CLI hashes the downloaded bytes and compares every available
expectation: the supplied digest, selected detail digest and size, and the
`X-Content-SHA256` / `X-Dashboard-Version-Id` response headers. Header names are
case-insensitive; equivalent UUID version spellings match the same version,
while legacy opaque IDs require an exact match. A mismatch exits 9 with `source_integrity_mismatch` and creates
neither the file nor its parent directory. Older services without these headers
remain usable with an explicit version ID; `--expected-sha256` can still check a
digest from an external snapshot. Output must be relative to the configured
workdir, and existing files, symbolic links, junctions and escaping paths remain
rejected.

Successful source JSON includes `state`, `dashboard_id`, `version_id`, `sha256`
(computed from actual bytes), `byte_size` and `output`. Current/draft selection
also returns the original detail `revision`; explicit-ID downloads do not invent
a revision. Source is written byte for byte, including encoding, whitespace,
CRLF/LF and the final newline. Hash local HTML in binary mode too, for example:

```sh
python3 -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("report.html").read_bytes()).hexdigest())'
```

Compare local HTML with the selected source, review the diff, then `save` or
`publish --file` with the same dashboard ID, the revision captured by `list`/`show`
and a new request UUID. Equal hashes allow the caller to skip only an action that
would change content alone; they do not replace publishing a saved draft,
restoring or archiving, or changing metadata or permissions. Neither CLI nor
service automatically skips an upload or a business action based on a hash.
A revision conflict requires a new review of the changes; do not silently rebase
onto the latest revision. Updates create immutable versions at the same stable
dashboard link.

## Write results and recovery

Published HTML can contain ordinary absolute HTTP(S) links to articles or other
dashboards. The viewer opens real anchor clicks in a new tab with no opener or
referrer, retaining the current board; `#section` anchors stay inside the HTML.
Use real `a[href]` elements rather than `data-url` rows or custom window/parent
navigation. Do not add a framed-mode `preventDefault()` handler or a copy-only
notice: the viewer owns external link handling. If the browser blocks opening,
its trusted fallback link lets the user open the validated destination. This
render-time behavior does not rewrite stored HTML, its source or SHA-256.

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
[the canonical contracts, section 8](../../docs/aresclaw-dashboard/contracts.md):

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
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s integrations/aresclaw-dashboard/tests -v
```

Tests use a real temporary HTTP server and subprocess CLI invocations. No
third-party Python packages are required.

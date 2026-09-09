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

```sh
request_id="$(dashboard new-request-id | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')"
dashboard publish --file report.html --title "Weekly report" --request-id "$request_id"
dashboard operation --request-id "$request_id"
```

Every write requires `--request-id`. Existing-dashboard changes also require
`--expected-revision`. `publish --dashboard-id ...` must include that
revision. Grant and public-access dates accept absolute RFC3339 timestamps only.

The command families are:

- `list`, `show`, `source`, `publish`, and `operation`
- `rename`, `versions`, `rollback`, `archive`, and `restore`
- `principals`, `grants`, `share`, `revoke`, `public`, `access`, and
  `access-apply`
- `group list`, `group create`, and `group member add|remove`

Run `dashboard <command> --help` for flags. Group member changes read the
current member set, verify the supplied group revision, and update it with CAS.

## Output and exit codes

Business results and errors are one JSON object on stdout. Diagnostics use
stderr and never contain the loaded credential.

| Code | Meaning |
| ---: | --- |
| 0 | Succeeded |
| 10 | Accepted or pending |
| 20 | Authentication required or expired |
| 21 | Forbidden or not visible |
| 30 | Revision, quota, or idempotency conflict |
| 40 | Invalid input |
| 50 | Write outcome unknown; query the original request ID |
| 60 | Network failure on a read |
| 70 | Other service/protocol failure |

The CLI never follows redirects and never accepts `--token`, an actor identity,
or an arbitrary service URL. A timeout or transport break during a write returns
`outcome_unknown` and the original `idempotency_key`.

## Tests

```sh
python3 -m unittest discover -s integrations/aresclaw-dashboard/tests -v
```

Tests use a real temporary HTTP server and subprocess CLI invocations. No
third-party Python packages are required.

# CLI reference

`bf-agent-viewer` is the real, scriptable path underneath both services (the gateway and the console) and everything that feeds them — organizations, humans, agents, tokens, console logins. [`docker-compose.yml`](../docker-compose.yml) is a thin wrapper around these same commands, not a separate deployment mechanism; anything you can do with `docker compose run`, you can do with a direct `pip install -e ".[console]"` and these commands run locally. See [`status.md`](status.md) for what's built right now, and the root [`README.md`](../README.md) for the fastest way to get a stack running with Docker Compose.

Every command below reads `--db` and, where relevant, `--org` from either a flag or an environment variable (`BF_DB`, `BF_ORG`) — a flag always wins if both are given. That's what lets `docker-compose.yml` configure each service with one `environment:` block instead of hard-coding flags into `command:`.

## Bootstrapping a fresh database

Nothing else works until an organization and at least one human exist — `register` and `console-user create` both assume those rows are already there.

```
bf-agent-viewer org create --db bf.db --id org-1 --name "Acme"
bf-agent-viewer human create --db bf.db --org org-1 --id human-1 --name "Ada" --email ada@example.com
```

`human create --email` is only required if that human will log into the console; an agent owner with no console access needs no email.

## `org create`

Creates an organization. This project is single-tenant per install (see [`security.md`](security.md)) — in practice most installs create exactly one.

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | Path to the SQLite database file; created if it doesn't exist |
| `--id` | — | yes | Organization id, referenced by every human and agent |
| `--name` | — | yes | Display name |

## `human create`

Creates a human — an agent owner, and/or a future console user.

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | |
| `--org` | `BF_ORG` | yes | Must already exist (`org create` first) |
| `--id` | — | yes | Human id, referenced by `register --owner` and `console-user create --human` |
| `--name` | — | yes | |
| `--email` | — | only for console access | Needed before `console-user create` |
| `--role` | — | no | Free text |

## `register`

Registers an agent identity and issues its bearer token in one step. Run again with `--parent-identity` to register a sub-agent — its `--scope` must be a subset of the parent's granted scope, enforced at registration time, not just hoped for (see [`how-it-works.md`](how-it-works.md) on delegation).

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | |
| `--org` | `BF_ORG` | yes | |
| `--agent-id` | — | yes | |
| `--name` | — | yes | Display name |
| `--owner` | — | yes | `human create`'s `--id` |
| `--scope` | — | yes | One or more tool names, space-separated |
| `--parent-identity` | — | no | The parent's `identity-<agent-id>` id, for delegation |

Prints the issued token once. There's no separate command to retrieve it later — set it as the agent's `X-BF-Agent-Token` header when you get it, or re-run `register` to issue a fresh one (the old token isn't revoked automatically; see [`versions.md`](versions.md) on the v0.2.0 credential broker for real revocation).

## `claim`

The other half of dual-path agent registration (F-027): claims an agent that the gateway discovered passively (an unrecognized caller — no valid bearer token — that showed up in traffic and got a real, visible-but-unowned row instead of being logged invisibly). Check the dashboard or `agents` table for the id it landed under, then claim it here to assign a real owner and issue it a real, scoped credential.

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | |
| `--org` | `BF_ORG` | yes | |
| `--agent-id` | — | yes | The id of the discovered agent row — see the dashboard, or `agents` table `status = 'unclaimed'` |
| `--owner` | — | yes | `human create`'s `--id` |
| `--scope` | — | yes | One or more tool names, space-separated |
| `--name` | — | no | Rename the agent at claim time; defaults to keeping the name it was discovered under |

Like `register`, prints the issued token once — set it as the agent's `X-BF-Agent-Token` header. Claiming refuses to run against anything not currently in the discovered/unclaimed state (already claimed, or explicitly registered) rather than silently re-owning it — register a fresh identity with `register` instead if that's what you actually want.

An agent that's discovered but never claimed stays visible on the dashboard with no owner — it's never blocked from calling tools, since v0.1.0 is visibility-first, not enforcement-first (see [`security.md`](security.md)).

## Reporting events from the OTel SDK (fallback path, F-024)

Not a `bf-agent-viewer` CLI command — this is a small Python client (`bf_agent_viewer.sdk`) an agent process imports directly, for the fallback instrumentation path: agents that never connect through the MCP gateway above at all, e.g. one calling third-party REST APIs directly with no MCP layer in between (see [`how-it-works.md`](how-it-works.md) section 3). It uses the exact same `X-BF-Agent-Token` a `register`/`claim` call already printed you — no separate credential to manage.

```python
from bf_agent_viewer.sdk import BFAgentViewerClient

client = BFAgentViewerClient(
    gateway_url="http://localhost:8941",  # the running `gateway`'s own host:port
    token="bfav_...",                     # from `register` or `claim`, above
    agent_name="billing-agent",           # optional; used only for passive discovery if the token isn't recognized
)

# Times the block and reports success/error automatically:
with client.trace_tool_call("send_invoice", arguments={"customer": "acme"}):
    send_invoice(customer="acme")

# Or report a call that already finished, on your own terms:
client.record_tool_call(
    "send_invoice", arguments={"customer": "acme"},
    result="success", duration_ms=142.3,
)
```

Dependency-free (stdlib `urllib` only) so importing it never pulls in an HTTP library an agent doesn't already have. Reporting failures (gateway unreachable, event rejected) never raise by default — pass `raise_on_error=True` to `BFAgentViewerClient` if you want them to.

What this path is and isn't: it's lower-fidelity than the MCP gateway on purpose. The gateway sees every call because it sits in the request path; the SDK only sees what an agent chooses to report, after the fact. It also can't block anything — by the time an event reaches the gateway's ingestion endpoint, the real tool call already happened somewhere this platform was never in the path for. A call reported outside the identity's `granted_scope`, or a burst of reports exceeding this endpoint's own rate limit, is still logged (or, for the rate limit, rejected purely to protect the event pipeline itself) and — for a scope violation — raises an alert, but neither one prevents the call itself. See [`security.md`](security.md).

Wire format is a small, honest subset of the OpenTelemetry GenAI semantic convention's `gen_ai.*` attribute names (`POST /v1/otel/events` on the gateway) rather than a proprietary shape — see [`standards.md`](standards.md).

## `gateway`

Runs the gateway: a persistent process that proxies to one backend MCP server, resolving identity per request from the presented bearer token, enforcing delegation scope, rate-limiting per agent, and logging every call as a tamper-evident event. See [`security.md`](security.md) for what it does and doesn't protect against.

| Flag | Env var | Default | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | — (required) | |
| `--org` | `BF_ORG` | — (required) | |
| `--backend-script` | `BF_BACKEND_SCRIPT` | — (required) | Path to the backend MCP server script this gateway proxies to |
| `--write-tools` | `BF_WRITE_TOOLS` | none | Comma-separated tool names treated as write actions |
| `--host` | `BF_GATEWAY_HOST` | `127.0.0.1` | |
| `--port` | `BF_GATEWAY_PORT` | `8941` | |
| `--rate-limit` | `BF_RATE_LIMIT` | `5.0` | Steady tokens/sec per agent identity |
| `--rate-burst` | `BF_RATE_BURST` | `20.0` | Burst allowance above the steady rate |
| `--no-container` | `BF_NO_CONTAINER` | off | Forces the rlimit-only backend sandbox even if Docker is available |
| `--alert-webhook` | `BF_ALERT_WEBHOOK` | none | URL to POST a JSON alert to (F-036) — e.g. on a rate-limit rejection |
| `--alert-email-to` | `BF_ALERT_EMAIL_TO` | none | Comma-separated recipient addresses |
| `--alert-email-from` | `BF_ALERT_EMAIL_FROM` | — | Required if `--alert-email-to` is set |
| `--alert-email-smtp-host` | `BF_ALERT_EMAIL_SMTP_HOST` | — | Required if `--alert-email-to` is set |
| `--alert-email-smtp-port` | `BF_ALERT_EMAIL_SMTP_PORT` | `587` | |
| `--alert-email-user` | `BF_ALERT_EMAIL_USER` | none | Omit for an unauthenticated/allowlisted relay |
| `--alert-email-password` | `BF_ALERT_EMAIL_PASSWORD` | none | Prefer the env var over the flag — avoids the password landing in shell history |
| `--alert-email-no-tls` | `BF_ALERT_EMAIL_NO_TLS` | off | Skips STARTTLS |
| `--gap-threshold` | `BF_GAP_THRESHOLD_SECONDS` | `60.0` | Seconds since the last logged event before a startup gap is marked and alerted (F-042) — see [`security.md`](security.md) |

By default the gateway spawns the backend script inside a locked-down Docker container when Docker is reachable (real filesystem/network isolation), falling back to rlimit-only sandboxing with a logged warning when it isn't — never silently. `--no-container`/`BF_NO_CONTAINER=1` forces the fallback path deliberately; the Docker Compose deployment sets this (see the comment at the top of [`docker-compose.yml`](../docker-compose.yml) for why mounting the host's Docker socket into the gateway's own container isn't the answer).

Neither `--alert-webhook` nor `--alert-email-to` is required — with neither set, alerts still fire, just to the gateway's own log (WARNING, or ERROR for `critical` severity) rather than a channel. Set both to fan an alert out to each independently; one failing to deliver doesn't stop the other from being tried. Every alert is persisted to the database regardless of delivery, so a webhook outage or SMTP failure doesn't mean the alert never happened.

**Agent heartbeat (F-047).** Every gateway registers a reserved tool, `bf_heartbeat`, that an agent can call through its normal MCP connection to signal it's still alive — useful for an agent that goes quiet for a stretch with no real tool calls to make, so it doesn't start reading as offline (see the online/offline indicator under `console` below) just for being idle. It's not proxied to the backend, doesn't need to be in the caller's granted scope, and doesn't count against its rate limit. `bf_heartbeat` is a reserved name — avoid giving a backend tool of your own the same name.

## `console`

Runs the read-only web console. Requires a valid session on every route — see `console-user create` below to provision the first login.

| Flag | Env var | Default | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | — (required) | |
| `--org` | `BF_ORG` | none | Restrict the dashboard to one organization; omit to show all |
| `--host` | `BF_CONSOLE_HOST` | `127.0.0.1` | |
| `--port` | `BF_CONSOLE_PORT` | `8942` | |
| `--secure-cookies` | `BF_SECURE_COOKIES` | off | Marks session cookies `Secure` — only correct once served behind TLS |
| `--stale-threshold` | `BF_STALE_THRESHOLD_SECONDS` | `300.0` | Seconds since an agent's last logged event before the dashboard shows it as offline rather than online (F-046) |

Each agent's dashboard row and detail page show a computed online/offline badge — "online" if it's logged activity within `--stale-threshold`, "offline" if it has activity but it's older than that, or "never" if it's never logged anything at all. This is derived at read time from existing event data, not a separate liveness check — see [`features.md`](features.md) for the F-047 ping/health-check feature that's a live probe instead.

## `console-user create`

Provisions (or resets) a console login for an existing human, and starts TOTP enrollment. Prints a secret and an `otpauth://` URI — add it to an authenticator app, then finish setup by logging in at the console and entering the current code. There is no path that grants console access with a password alone; MFA enrollment is mandatory, not a toggle.

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | |
| `--human` | — | yes | Must already exist and have an email on file |
| `--password` | — | no | Omit to be prompted (recommended — avoids the password landing in shell history) |

## `export events`

The scriptable half of the customer-facing compliance export (F-034): a filtered CSV export of event history, for satisfying your own regulatory record-keeping obligations (e.g. EU AI Act Art. 12) without contacting us. The console's own `/export` page (behind login, point-and-click, with agent/owner dropdowns) calls the same underlying export — use whichever fits: the CLI for automation, the console for a one-off pull.

| Flag | Env var | Required | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | yes | |
| `--org` | `BF_ORG` | no | Restrict the export to one organization; omit to export across all organizations in this database |
| `--agent-id` | — | no | Restrict to one agent |
| `--owner` | — | no | Restrict to agents owned by this `human create`'s `--id` |
| `--start` | — | no | Inclusive lower bound — `YYYY-MM-DD` or the full `YYYY-MM-DDTHH:MM:SSZ` timestamp shape |
| `--end` | — | no | Inclusive upper bound — a bare date covers through the end of that day |
| `--out` | — | no | Write CSV to this file; omit to print to stdout (pipe it, or redirect it, as you like) |

Every filter is optional and additive — with none set, this exports the organization's (or, with `--org` also omitted, the whole database's) complete event history. Each row includes the event's `content_hash`, so an auditor can independently re-verify the tamper-evident chain (see [`security.md`](security.md)) rather than taking the export on faith. Output format/columns are CSV only for v0.1.0 — a formatted/PDF report is a possible later Paid enhancement, not built now.

```
bf-agent-viewer export events --db bf.db --org org-1 --start 2026-09-01 --end 2026-09-30 --out september.csv
```

## `retention prune`

The prune job half of configurable log retention (F-019): deletes event rows older than the retention window, for satisfying your own regulatory record-keeping obligations (e.g. EU AI Act Art. 19/26 minimum retention) without the event store growing forever. Not run automatically — run it yourself on whatever schedule fits (a daily cron entry is the simplest option for a self-hosted install).

| Flag | Env var | Default | Notes |
| --- | --- | --- | --- |
| `--db` | `BF_DB` | — (required) | |
| `--retention-days` | `BF_RETENTION_DAYS` | `180` | Delete events older than this many days — the default is a compliance-safe ~6 months |

Safe to run repeatedly (a run with nothing yet past the window is a no-op) and safe to run against a database that's still receiving live traffic from a gateway process. Pruning only ever removes a contiguous prefix of the event history — the oldest rows first — never an arbitrary filtered set: the event store's tamper-evident hash chain (see [`security.md`](security.md)) chains each row to the one before it, so a prune run records a checkpoint of the last row it removes before deleting anything, and chain verification (and the next event logged) both pick up from that checkpoint afterward rather than assuming an untouched history. This is why there's no `--agent-id`/`--org` filter here the way `export events` has one above — retention prunes the whole chain's oldest rows, not a filtered subset of them.

```
bf-agent-viewer retention prune --db bf.db --retention-days 180
```

## A complete first run

```
bf-agent-viewer org create --db bf.db --id org-1 --name "Acme"
bf-agent-viewer human create --db bf.db --org org-1 --id human-1 --name "Ada" --email ada@example.com
bf-agent-viewer register --db bf.db --org org-1 --agent-id agent-1 --name "First Agent" --owner human-1 --scope get_weather
bf-agent-viewer console-user create --db bf.db --human human-1

# separate terminals / processes:
bf-agent-viewer gateway --db bf.db --org org-1 --backend-script path/to/your_backend.py
bf-agent-viewer console --db bf.db
```

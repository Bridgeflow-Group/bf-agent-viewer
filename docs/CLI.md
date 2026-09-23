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

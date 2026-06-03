# mcp-github

Tank-bound GitHub App MCP server for Tank sessions.

Incoming MCP requests must carry a Tank-signed session attestation with
audience `mcp-github-tank`. The agent-facing MCP server name remains
`github`; the implementation is intentionally Tank-specific.

## Layout

- `src/` - Python MCP server package.
- `Dockerfile` - image build for `romainecr.azurecr.io/mcp-github`.
- `chart/` - Helm chart synced by ArgoCD.

Images are SHA-tagged from `main`; `.github/workflows/build.yml` pushes the image and commits the matching chart tag.

## GitHub App Boundaries

`mcp-github` is Tank-specific. It uses two Tank-owned app identities:

- `tank-operator-host-*`: an org-owned private GitHub App installed only on
  `romaine-life`, used for Tank host automation.
- `tank-operator-app-*`: the public org-owned user-facing GitHub App that
  standard users install on their own accounts.

The chart expects these host Key Vault secrets:

- `tank-operator-host-app-id`
- `tank-operator-host-app-installation-id`
- `tank-operator-host-app-private-key`

It also expects the existing user-facing secrets:

- `tank-operator-app-id`
- `tank-operator-app-private-key`

Do not point `GITHUB_APP_*` at a generic org-wide App such as
`romaine-life-host`, or at the user-facing Tank App. Either shortcut crosses
subsystem identities and makes a migration look healthy for the wrong reason.

The user-facing App should likewise remain a dedicated `romaine-life` org-owned
App, not a personal-account App and not a generic shared App. It is public only
because GitHub requires public Apps for installation on accounts outside the
owning org. Current production slug: `romaine-life-tank-operator`.

## Control Action Audit

PR lifecycle tools that can put code onto a protected branch are audited through
Tank before they mutate GitHub:

- `mark_pull_request_ready_for_review`
- `merge_pull_request`

For each invocation, `mcp-github` appends a `started` row to Tank's
`control_action_events` ledger using the caller's service JWT and session id.
If that write fails, the GitHub mutation is not attempted. After GitHub accepts
or rejects the mutation, `mcp-github` appends a terminal `succeeded` or
`failed` row with the PR target and result/error evidence.

Production callers write to the configured production Tank URL. Test-slot
callers write to their slot orchestrator (`http://tank-operator.<scope>.svc`)
based on the caller's service-token scope, so the trace appears in the same
Tank UI where the session is running.

Humans inspect the durable trace in Tank's session Background -> Control tab or
through `GET /api/sessions/<id>/control-actions`. Loki can still show raw
GitHub HTTP calls, but it is not the attribution source of truth.

Prometheus scrapes `/metrics` through the chart's ServiceMonitor. Relevant
counters:

- `mcp_github_control_action_total{tool,action,status,result}`
- `mcp_github_control_action_audit_append_total{status,result}`

Create the org-owned Tank host App with the GitHub App manifest flow, not
query parameters on the settings page. The settings page can ignore event
parameters silently.

```json
{
  "name": "tank-operator-host",
  "url": "https://tank.romaine.life",
  "hook_attributes": {
    "url": "https://tank.romaine.life/api/github/webhook"
  },
  "redirect_url": "http://localhost:9/github-app-manifest-callback",
  "public": false,
  "default_permissions": {
    "organization_administration": "write",
    "actions": "write",
    "actions_variables": "write",
    "administration": "write",
    "checks": "read",
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "read",
    "workflows": "write"
  },
  "default_events": []
}
```

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

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

## MCP Contracts

`list_installation_repos` returns the GitHub-facing repository discovery shape:

```json
{
  "repositories": [
    {
      "full_name": "owner/name",
      "private": false,
      "default_branch": "main"
    }
  ],
  "count": 1,
  "total_count": 1,
  "truncated": false,
  "has_more": false,
  "limit": null
}
```

Product APIs that prefer shorter vocabulary, such as Tank's `/api/github/repos`,
translate this response at their boundary. The MCP tool contract remains
`repositories` to match GitHub's resource name and the adjacent installation
fan-out tools.

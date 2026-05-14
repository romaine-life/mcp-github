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

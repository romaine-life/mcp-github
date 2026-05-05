from typing import Any

import httpx

from .auth import GitHubAppTokenMinter

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, minter: GitHubAppTokenMinter) -> None:
        self._minter = minter

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._minter.installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = httpx.get(f"{GITHUB_API}{path}", headers=self._headers(), params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()

    def get_text(self, path: str) -> str:
        """Like get(), but returns the response body as text after following
        redirects. Used for endpoints that hand out non-JSON, e.g.
        /actions/jobs/{id}/logs which 302s to a presigned blob URL.
        httpx strips Authorization on cross-origin redirects, so the App
        token doesn't leak to the presigned host."""
        r = httpx.get(
            f"{GITHUB_API}{path}",
            headers=self._headers(),
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.text

    def get_bytes(self, path: str) -> bytes:
        """Like get_text(), but returns raw response bytes. For endpoints that
        hand out binary blobs, e.g. /actions/artifacts/{id}/zip which 302s to
        a presigned URL containing a zip archive. Same cross-origin
        Authorization-stripping guarantee as get_text."""
        r = httpx.get(
            f"{GITHUB_API}{path}",
            headers=self._headers(),
            timeout=120.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.content

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.post(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        r.raise_for_status()
        return r.json() if r.content else None

    def patch(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.patch(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        r.raise_for_status()
        return r.json() if r.content else None

    def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.put(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        r.raise_for_status()
        return r.json() if r.content else None

    def delete(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.request("DELETE", f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        r.raise_for_status()
        return r.json() if r.content else None

    def mint_scoped_token(
        self,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Pass-through to the underlying minter. Surfaces a one-shot scoped
        token to callers (the `mint_clone_token` MCP tool); the cached
        process token used for outgoing API calls is unaffected."""
        return self._minter.mint_scoped_token(
            repositories=repositories, permissions=permissions,
        )

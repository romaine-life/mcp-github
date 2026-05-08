from typing import Any

import httpx

from .caller import current_caller
from .minter_pool import MinterPool

GITHUB_API = "https://api.github.com"

# Cap the response-body excerpt we splice into error messages. GitHub's error
# bodies for normal failures (404 / 422 / 405) are tiny JSON; this cap is
# only a guard against pathological upstream responses (HTML error pages,
# enormous validation lists) blowing up an MCP frame.
_ERROR_BODY_CAP = 1200


def _check(r: httpx.Response) -> None:
    """Raise on non-2xx with the response body included in the error.

    ``httpx.Response.raise_for_status`` raises an ``HTTPStatusError``
    whose ``__str__`` is just the status line — when this exception
    bubbles through the MCP transport, the body that GitHub returned
    (e.g. "Required status check has not been verified" on a 405 merge
    attempt, or the 422 list of which fields failed validation) is
    silently dropped. Re-raise with the body inlined so MCP clients see
    GitHub's actual explanation, not just a status code.

    Preserves the ``HTTPStatusError`` class and ``response`` attribute
    so existing callers that pattern-match on ``exc.response.status_code``
    (see ``_is_404`` in tools.py) keep working.
    """
    if r.is_success:
        return
    body = r.text or ""
    if len(body) > _ERROR_BODY_CAP:
        body = body[:_ERROR_BODY_CAP] + "...(truncated)"
    detail = f": {body}" if body else ""
    raise httpx.HTTPStatusError(
        f"{r.status_code} {r.reason_phrase} for "
        f"{r.request.method} {r.request.url}{detail}",
        request=r.request,
        response=r,
    )


class GitHubClient:
    """Wraps GitHub API calls with the right per-caller App minter.

    Resolves the minter on every request via the contextvar set by the
    HTTP middleware (see ``caller.py``). When the contextvar is unset
    — stdio mode, an unrecognised caller, or a failed orchestrator
    lookup — the pool returns the host minter, which preserves
    pre-stage-3 behavior for every code path that doesn't have a
    routable caller.
    """

    def __init__(self, pool: MinterPool) -> None:
        self._pool = pool

    def _minter(self):
        return self._pool.for_caller(current_caller())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._minter().installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = httpx.get(f"{GITHUB_API}{path}", headers=self._headers(), params=params, timeout=15.0)
        _check(r)
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
        _check(r)
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
        _check(r)
        return r.content

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.post(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        _check(r)
        return r.json() if r.content else None

    def patch(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.patch(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        _check(r)
        return r.json() if r.content else None

    def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.put(f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        _check(r)
        return r.json() if r.content else None

    def delete(self, path: str, json: dict[str, Any] | None = None) -> Any:
        r = httpx.request("DELETE", f"{GITHUB_API}{path}", headers=self._headers(), json=json, timeout=15.0)
        _check(r)
        return r.json() if r.content else None

    def mint_scoped_token(
        self,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Pass-through to the underlying minter for the in-flight caller.

        Surfaces a one-shot scoped token to callers (the
        ``mint_clone_token`` MCP tool); the cached process token used for
        outgoing API calls is unaffected. Resolved per call so a
        non-host caller's ``git clone`` token comes from *their*
        installation, which is the central point of stage 3.
        """
        return self._minter().mint_scoped_token(
            repositories=repositories, permissions=permissions,
        )

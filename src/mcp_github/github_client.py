from typing import Any

import httpx

from .caller import CallerIdentity, current_caller
from .minter_pool import MinterPool

GITHUB_API = "https://api.github.com"

_ERROR_BODY_CAP = 1200


def _check(r: httpx.Response) -> None:
    """Raise on non-2xx with the response body included in the error.

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


def _is_cross_install_failure(r: httpx.Response) -> bool:
    """True when GitHub signals the installation can't reach this repo.

    403 + "Resource not accessible by integration" is unambiguous.
    404 is also included because GH hides inaccessible repos as 404 to
    prevent enumeration; the caller only primes the cache if the host
    retry *succeeds*, so a false-positive costs one extra round-trip.
    """
    if r.is_success:
        return False
    if r.status_code == 403:
        return "Resource not accessible by integration" in (r.text or "")
    return r.status_code == 404


class GitHubClient:
    """Wraps GitHub API calls with the right per-caller App minter.

    Resolves the minter on every request via the contextvar set by the
    HTTP middleware (see ``caller.py``). When the contextvar is unset
    — stdio mode, an unrecognised caller, or a failed orchestrator
    lookup — the pool returns the host minter, which preserves
    pre-stage-3 behavior for every code path that doesn't have a
    routable caller.

    Cross-installation fallback. Tools that target a specific repo pass
    ``repo=(owner, name)`` so the client can retry on behalf of a
    non-host caller whose user installation can't see that repo. On
    failure (404 / 403 "not accessible by integration"), the client
    retries once with the host installation token. On retry success it
    records the repo as inaccessible in the pool so subsequent calls skip
    the user-install round-trip for the rest of the 30-min cache window.
    """

    def __init__(self, pool: MinterPool) -> None:
        self._pool = pool

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _with_fallback(
        self,
        make_request,  # callable(headers: dict) -> httpx.Response
        *,
        repo: tuple[str, str] | None,
    ) -> httpx.Response:
        """Execute ``make_request`` with the caller-appropriate minter.

        Proactively uses the host minter when the pool's repo-access
        cache confirms the caller's installation can't serve ``repo``
        (avoids a doomed round-trip). On a fresh access failure that
        looks like a cross-install issue, retries with the host minter
        and primes the cache on success.
        """
        caller = current_caller()
        minter = self._pool.for_caller_repo(caller, repo)
        r = make_request(self._headers(minter.installation_token()))

        if (
            repo is not None
            and minter is not self._pool.host
            and _is_cross_install_failure(r)
        ):
            # User installation can't see this repo. Try the host.
            host_token = self._pool.host.installation_token()
            r2 = make_request(self._headers(host_token))
            if r2.is_success:
                if caller is not None:
                    self._pool.record_repo_inaccessible(caller, *repo)
                return r2
            # Both failed. Return the original response so _check raises
            # the right status for callers like _is_404 in commit_to_branch.
        return r

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        r = self._with_fallback(
            lambda h: httpx.get(
                f"{GITHUB_API}{path}", headers=h, params=params, timeout=15.0
            ),
            repo=repo,
        )
        _check(r)
        return r.json()

    def get_text(
        self,
        path: str,
        *,
        repo: tuple[str, str] | None = None,
    ) -> str:
        """Returns response body as text after following redirects.
        Used for /actions/jobs/{id}/logs which 302s to a presigned blob URL."""
        r = self._with_fallback(
            lambda h: httpx.get(
                f"{GITHUB_API}{path}",
                headers=h,
                timeout=30.0,
                follow_redirects=True,
            ),
            repo=repo,
        )
        _check(r)
        return r.text

    def get_bytes(
        self,
        path: str,
        *,
        repo: tuple[str, str] | None = None,
    ) -> bytes:
        """Returns raw response bytes after following redirects."""
        r = self._with_fallback(
            lambda h: httpx.get(
                f"{GITHUB_API}{path}",
                headers=h,
                timeout=120.0,
                follow_redirects=True,
            ),
            repo=repo,
        )
        _check(r)
        return r.content

    def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        r = self._with_fallback(
            lambda h: httpx.post(
                f"{GITHUB_API}{path}", headers=h, json=json, timeout=15.0
            ),
            repo=repo,
        )
        _check(r)
        return r.json() if r.content else None

    def patch(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        r = self._with_fallback(
            lambda h: httpx.patch(
                f"{GITHUB_API}{path}", headers=h, json=json, timeout=15.0
            ),
            repo=repo,
        )
        _check(r)
        return r.json() if r.content else None

    def put(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        r = self._with_fallback(
            lambda h: httpx.put(
                f"{GITHUB_API}{path}", headers=h, json=json, timeout=15.0
            ),
            repo=repo,
        )
        _check(r)
        return r.json() if r.content else None

    def delete(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        r = self._with_fallback(
            lambda h: httpx.request(
                "DELETE",
                f"{GITHUB_API}{path}",
                headers=h,
                json=json,
                timeout=15.0,
            ),
            repo=repo,
        )
        _check(r)
        return r.json() if r.content else None

    def mint_scoped_token(
        self,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
        repos_full: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str]:
        """Pass-through to the underlying minter for the in-flight caller.

        ``repos_full`` carries the ``(owner, name)`` pairs for repos in
        ``repositories`` so the client can detect cross-installation failures
        and fall back to the host minter. When the host fallback succeeds,
        all listed repos are recorded as inaccessible in the pool.
        """
        caller = current_caller()
        minter = self._pool.for_caller(caller)
        try:
            return minter.mint_scoped_token(
                repositories=repositories, permissions=permissions
            )
        except httpx.HTTPStatusError as original:
            if (
                repos_full
                and minter is not self._pool.host
                and original.response.status_code in (404, 422)
            ):
                try:
                    token, expires = self._pool.host.mint_scoped_token(
                        repositories=repositories, permissions=permissions
                    )
                except httpx.HTTPStatusError:
                    raise original
                if caller is not None:
                    for owner, name in repos_full:
                        self._pool.record_repo_inaccessible(caller, owner, name)
                return token, expires
            raise

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
    HTTP middleware (see ``caller.py``). Proxied MCP requests must have a
    resolved caller before tools run.

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

    def _require_super_admin(self) -> CallerIdentity:
        caller = current_caller()
        if caller is None or not caller.is_super_admin:
            raise PermissionError("GitHub installation fan-out requires super-admin access")
        return caller

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
            and caller is not None
            and caller.is_super_admin
            and _is_cross_install_failure(r)
        ):
            # User installation can't see this repo. Try the Tank host App
            # installation for that repo's owner.
            host_token = self._pool.host_for_owner(repo[0]).installation_token()
            r2 = make_request(self._headers(host_token))
            if r2.is_success:
                if caller is not None:
                    self._pool.record_repo_inaccessible(caller, *repo)
                return r2
            # All fallbacks failed. Return the original response so _check
            # raises the right status for callers like _is_404 in
            # commit_to_branch.
        elif (
            repo is not None
            and minter is self._pool.host
            and caller is not None
            and caller.is_super_admin
            and _is_cross_install_failure(r)
        ):
            # Host/super-admin sessions can target repos that are installed
            # only on a user-facing installation. Route the retry to the
            # installation that actually contains the repo.
            user_minter = self._user_minter_for_repo(*repo, exclude=minter)
            if user_minter is not None:
                r2 = make_request(self._headers(user_minter.installation_token()))
                if r2.is_success:
                    return r2
        return r

    def _user_minter_for_repo(
        self,
        owner: str,
        name: str,
        *,
        exclude: Any | None = None,
    ):
        """Find a user-facing App minter whose installation contains repo.

        This is a super-admin fallback path only. Normal callers keep using
        their own installation, with the existing user -> host fallback for
        host-owned repos.
        """
        wanted = f"{owner}/{name}".lower()
        for install in self._pool.list_user_app_installations():
            installation_id = int(install["id"])
            minter = self._pool.user_app_minter(installation_id)
            if minter is exclude:
                continue
            page = 1
            while True:
                r = httpx.get(
                    f"{GITHUB_API}/installation/repositories",
                    headers=self._headers(minter.installation_token()),
                    params={"per_page": 100, "page": page},
                    timeout=15.0,
                )
                _check(r)
                repos = r.json().get("repositories", [])
                if any(repo["full_name"].lower() == wanted for repo in repos):
                    return minter
                if len(repos) < 100:
                    break
                page += 1
        return None

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

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        repo: tuple[str, str] | None = None,
    ) -> Any:
        """POST a query/mutation to the GitHub GraphQL (v4) API.

        Some operations have no REST equivalent — notably converting a
        draft pull request to ready-for-review, which only exists as the
        ``markPullRequestReadyForReview`` mutation. Routes through the same
        per-caller minter + host fallback as the REST helpers so callers
        targeting a host-owned repo from a user installation still work.

        Transport failures raise via ``_check``. GraphQL surfaces logical
        errors as HTTP 200 with a top-level ``errors`` array, so those are
        promoted to a ``RuntimeError`` here rather than silently returning
        ``data: null``.
        """
        r = self._with_fallback(
            lambda h: httpx.post(
                f"{GITHUB_API}/graphql",
                headers=h,
                json={"query": query, "variables": variables or {}},
                timeout=15.0,
            ),
            repo=repo,
        )
        _check(r)
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data.get("data")

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
        owner = repos_full[0][0] if repos_full else None
        # Host caller scopes a clone token from the Tank host App installation
        # for the repo's owner (e.g. the romaine-life org), not just the single
        # default installation.
        if caller is not None and caller.is_host and owner is not None:
            minter = self._pool.host_for_owner(owner)
        else:
            minter = self._pool.for_caller(caller)
        try:
            return minter.mint_scoped_token(
                repositories=repositories, permissions=permissions
            )
        except httpx.HTTPStatusError as original:
            fallback = (
                self._pool.host_for_owner(owner) if owner else self._pool.host
            )
            if (
                repos_full
                and minter is not self._pool.host
                and fallback is not minter
                and caller is not None
                and caller.is_super_admin
                and original.response.status_code in (404, 422)
            ):
                try:
                    token, expires = fallback.mint_scoped_token(
                        repositories=repositories, permissions=permissions
                    )
                except httpx.HTTPStatusError:
                    raise original
                if caller is not None:
                    for owner, name in repos_full:
                        self._pool.record_repo_inaccessible(caller, owner, name)
                return token, expires
            raise

    def list_user_app_installations(self) -> list[dict[str, Any]]:
        self._require_super_admin()
        return self._pool.list_user_app_installations()

    def list_repos_for_installation(
        self,
        installation_id: int,
        *,
        owner: str | None = None,
        name_contains: str | None = None,
        visibility: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._require_super_admin()
        minter = self._pool.user_app_minter(installation_id)
        rows: list[dict[str, Any]] = []
        page = 1
        needle = name_contains.lower() if name_contains else None
        visibility_filter = visibility.lower() if visibility else None
        while True:
            r = httpx.get(
                f"{GITHUB_API}/installation/repositories",
                headers=self._headers(minter.installation_token()),
                params={"per_page": 100, "page": page},
                timeout=15.0,
            )
            _check(r)
            repos = r.json().get("repositories", [])
            if not repos:
                break
            for repo in repos:
                full_name = repo["full_name"]
                repo_owner, repo_name = full_name.split("/", 1)
                if owner and repo_owner.lower() != owner.lower():
                    continue
                if visibility_filter == "private" and not repo["private"]:
                    continue
                if visibility_filter == "public" and repo["private"]:
                    continue
                if needle and needle not in full_name.lower() and needle not in repo_name.lower():
                    continue
                rows.append(
                    {
                        "installation_id": installation_id,
                        "full_name": full_name,
                        "private": repo["private"],
                        "default_branch": repo["default_branch"],
                    }
                )
            if len(repos) < 100:
                break
            page += 1
        return rows[:limit] if limit is not None else rows

    def mint_scoped_token_for_installation(
        self,
        installation_id: int,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        self._require_super_admin()
        minter = self._pool.user_app_minter(installation_id)
        return minter.mint_scoped_token(
            repositories=repositories,
            permissions=permissions,
        )

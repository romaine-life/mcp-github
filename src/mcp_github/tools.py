import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .github_client import GitHubClient


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _clamp_limit(limit: int | None, *, default: int, maximum: int = 100) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


def _truncate_text(text: str, max_chars: int | None, *, tail: bool = False) -> tuple[str, bool]:
    if max_chars is None or len(text) <= max_chars:
        return text, False
    if max_chars <= 0:
        return "", True
    return (text[-max_chars:] if tail else text[:max_chars], True)


def _is_404(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


def register_tools(mcp: FastMCP, gh: GitHubClient) -> None:
    @mcp.tool()
    def list_installation_repos(
        owner: str | None = None,
        name_contains: str | None = None,
        visibility: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List GitHub repositories available to this GitHub App installation, optionally filtered.

        Use to discover which owner/repo slugs can be read or written before
        cloning, opening issues, creating pull requests, or checking workflows.
        `owner` filters by org/user, `name_contains` matches repo names or
        full_name case-insensitively, `visibility` is public or private, and
        `limit` caps returned rows. When omitted, all matching repositories are
        returned. Responses include count metadata so truncated results are
        explicit.
        """
        cap = _clamp_limit(limit, default=100) if limit is not None else None
        rows: list[dict[str, Any]] = []
        page = 1
        needle = name_contains.lower() if name_contains else None
        visibility_filter = visibility.lower() if visibility else None
        while True:
            body = gh.get("/installation/repositories", params={"per_page": 100, "page": page})
            repos = body.get("repositories", [])
            if not repos:
                break
            for r in repos:
                full_name = r["full_name"]
                repo_owner, repo_name = full_name.split("/", 1)
                if owner and repo_owner.lower() != owner.lower():
                    continue
                if visibility_filter == "private" and not r["private"]:
                    continue
                if visibility_filter == "public" and r["private"]:
                    continue
                if needle and needle not in full_name.lower() and needle not in repo_name.lower():
                    continue
                rows.append(
                    {
                        "full_name": full_name,
                        "private": r["private"],
                        "default_branch": r["default_branch"],
                    }
                )
            if len(repos) < 100:
                break
            page += 1

        returned_rows = rows[:cap] if cap is not None else rows
        truncated = cap is not None and len(rows) > cap
        return {
            "repositories": returned_rows,
            "count": len(returned_rows),
            "total_count": len(rows),
            "truncated": truncated,
            "has_more": truncated,
            "limit": cap,
        }

    @mcp.tool()
    def mint_clone_token(
        repos: list[str],
        write: bool = False,
        workflows: bool = False,
    ) -> dict[str, str]:
        """Mint a short-lived (~1h) GitHub App installation token scoped over
        the given repos, suitable for `git clone` / `fetch` / `pull` (default)
        or `git push` (`write=True`) from a session container.

        Default scope is {contents: read, metadata: read} so the token cannot
        push, comment, or otherwise mutate. With `write=True` the scope widens
        to {contents: write, metadata: read} — the caller can `git push` to
        any branch they have permission for, including direct commits to main.

        Pushes that touch `.github/workflows/*` need an additional opt-in:
        GitHub gates workflow files behind a separate `workflows` perm even
        when `contents: write` is granted (the `git push` is rejected with
        "refusing to allow a GitHub App to create or update workflow ...").
        Pass `workflows=True` to mint with `workflows: write` added; requires
        `write=True` since a workflow-only push without contents-write makes
        no sense.

        Prefer the dedicated MCP write tools (`commit_to_branch`,
        `create_or_update_file`, etc.) for routine mutations: they resolve
        base refs server-side and avoid the cached-SHA-revert footgun
        documented in tank-operator/CLAUDE.md → "mcp-github write surface".
        Reach for `write=True` when you genuinely need a working tree on disk
        — large lockfiles, multi-file refactors driven by tooling, anything
        that's awkward to enumerate as an explicit `files` array.

        Use from a session container as:
            TOKEN=<value of `token` from this call>
            git clone https://x-access-token:${TOKEN}@github.com/owner/name.git
            # … work …
            git push  # only when minted with write=True

        Args:
            repos: list of "owner/name" strings to scope the token to.
                Required — blanket-scoped tokens are intentionally not
                offered; pass exactly the repos you need.
            write: if True, mint with contents: write so the token can push.
                Default False (read-only).
            workflows: if True, also include workflows: write so pushes that
                touch `.github/workflows/*` go through. Requires write=True.
                Default False.

        Returns: {"token": "...", "expires_at": "<iso8601>"}.
        """
        if not repos:
            raise ValueError("mint_clone_token: pass at least one repo (e.g. ['nelsong6/glimmung'])")
        if workflows and not write:
            raise ValueError("mint_clone_token: workflows=True requires write=True")
        repo_names: list[str] = []
        for r in repos:
            if "/" not in r:
                raise ValueError(f"repo must be 'owner/name', got: {r}")
            repo_names.append(r.split("/", 1)[1])
        permissions: dict[str, str] = {
            "contents": "write" if write else "read",
            "metadata": "read",
        }
        if workflows:
            permissions["workflows"] = "write"
        token, expires_at = gh.mint_scoped_token(
            repositories=repo_names,
            permissions=permissions,
        )
        return {"token": token, "expires_at": expires_at}

    @mcp.tool()
    def get_repo(owner: str, name: str) -> dict[str, Any]:
        """Get GitHub repository metadata for one owner/name repo.

        Use to confirm default branch, language, description, and repository
        existence before creating branches, commits, issues, or pull requests.
        """
        r = gh.get(f"/repos/{owner}/{name}")
        return {k: r.get(k) for k in ("full_name", "description", "default_branch", "language", "stargazers_count", "open_issues_count", "updated_at")}

    @mcp.tool()
    def create_repository(
        name: str,
        description: str | None = None,
        private: bool = False,
        auto_init: bool = True,
        org: str | None = None,
    ) -> dict[str, Any]:
        """Create a new repository.

        If `org` is given, creates under that organization via
        POST /orgs/{org}/repos. Works with App installation tokens when the
        installation has 'Administration: Write' on the org.

        Otherwise creates under the authenticated user's account via
        POST /user/repos. GitHub does not document App installation tokens as
        a supported auth method for that endpoint, so this path will likely
        return 403; create personal-account repos with `gh repo create` or a
        PAT in that case.

        auto_init=True seeds an initial README so the repo has a default
        branch you can immediately commit to via create_or_update_file or
        commit_to_branch. Set False if you'll push initial content yourself.
        """
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "auto_init": auto_init,
        }
        if description is not None:
            payload["description"] = description
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        r = gh.post(path, json=payload)
        return {
            "full_name": r["full_name"],
            "html_url": r["html_url"],
            "default_branch": r["default_branch"],
            "private": r["private"],
        }

    @mcp.tool()
    def get_file_contents(owner: str, name: str, path: str, ref: str | None = None, max_chars: int | None = 80_000) -> dict[str, Any]:
        """Read or fetch a file from a GitHub repository at an optional branch, tag, or SHA ref.

        Use when you need source text without cloning the repo. Base64 content
        is decoded for text files; binary files are returned as a byte summary.
        `max_chars` caps returned text; pass a larger value only for known-small
        files where the full body is needed.
        """
        params = {"ref": ref} if ref else None
        r = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if isinstance(r, list):
            return {"kind": "directory", "entries": [{"name": e["name"], "type": e["type"]} for e in r]}
        if r.get("encoding") == "base64":
            import base64
            try:
                content = base64.b64decode(r["content"]).decode("utf-8")
            except UnicodeDecodeError:
                content = f"<binary {r.get('size', 0)} bytes>"
            content, truncated = _truncate_text(content, max_chars)
            return {"kind": "file", "path": r["path"], "size": r.get("size"), "truncated": truncated, "content": content}
        content, truncated = _truncate_text(r.get("content", ""), max_chars)
        return {"kind": "file", "path": r["path"], "size": r.get("size"), "truncated": truncated, "content": content}

    @mcp.tool()
    def list_issues(
        owner: str,
        name: str,
        state: str = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        creator: str | None = None,
        mentioned: str | None = None,
        since: str | None = None,
        sort: str | None = None,
        direction: str | None = None,
        limit: int | None = 30,
    ) -> list[dict[str, Any]]:
        """List GitHub issues in a repository, excluding pull requests, optionally filtered.

        Use for issue triage and to find issue numbers. `state` is one of
        open, closed, or all. `labels` is an AND filter, `assignee`,
        `creator`, `mentioned`, and `since` map to GitHub issue filters,
        and `limit` caps returned rows.
        """
        cap = _clamp_limit(limit, default=30)
        params = _clean_params(
            {
                "state": state,
                "labels": ",".join(labels) if labels else None,
                "assignee": assignee,
                "creator": creator,
                "mentioned": mentioned,
                "since": since,
                "sort": sort,
                "direction": direction,
                "per_page": min(cap, 100),
            }
        )
        body = gh.get(f"/repos/{owner}/{name}/issues", params=params)
        rows = [
            {"number": i["number"], "title": i["title"], "state": i["state"], "user": i["user"]["login"]}
            for i in body
            if "pull_request" not in i
        ]
        return rows[:cap]

    @mcp.tool()
    def get_issue(owner: str, name: str, number: int) -> dict[str, Any]:
        """Get GitHub issue details including title, body, state, author, and labels.

        Use after list_issues or when a user references a specific issue number.
        """
        r = gh.get(f"/repos/{owner}/{name}/issues/{number}")
        return {
            "number": r["number"],
            "title": r["title"],
            "state": r["state"],
            "user": r["user"]["login"],
            "body": r.get("body", ""),
            "labels": [l["name"] for l in r.get("labels", [])],
        }

    @mcp.tool()
    def list_pull_requests(
        owner: str,
        name: str,
        state: str = "open",
        base: str | None = None,
        head: str | None = None,
        sort: str | None = None,
        direction: str | None = None,
        limit: int | None = 30,
    ) -> list[dict[str, Any]]:
        """List GitHub pull requests (PRs) in a repository, optionally filtered.

        Use to find open, closed, or all pull requests before reviewing,
        merging, commenting, labeling, or checking a branch's existing PR.
        `base` and `head` narrow by branch, and `limit` caps returned rows.
        """
        cap = _clamp_limit(limit, default=30)
        params = _clean_params(
            {
                "state": state,
                "base": base,
                "head": head,
                "sort": sort,
                "direction": direction,
                "per_page": min(cap, 100),
            }
        )
        body = gh.get(f"/repos/{owner}/{name}/pulls", params=params)
        return [{"number": p["number"], "title": p["title"], "state": p["state"], "user": p["user"]["login"], "head": p["head"]["ref"]} for p in body]

    @mcp.tool()
    def get_pull_request(owner: str, name: str, number: int) -> dict[str, Any]:
        """Get GitHub pull request (PR) details including body, head branch, base branch, and merge state.

        Use when you need PR metadata before merge, review, status checks, or
        follow-up comments.
        """
        r = gh.get(f"/repos/{owner}/{name}/pulls/{number}")
        return {
            "number": r["number"],
            "title": r["title"],
            "state": r["state"],
            "user": r["user"]["login"],
            "body": r.get("body", ""),
            "head": r["head"]["ref"],
            "base": r["base"]["ref"],
            "merged": r.get("merged", False),
        }

    @mcp.tool()
    def search_code(query: str, limit: int | None = 20) -> list[dict[str, Any]]:
        """Search GitHub code across repositories this App installation can access.

        Use for cross-repo source discovery when you do not know which repo or
        file contains a symbol, setting, workflow, or configuration. `limit`
        caps returned matches.
        """
        cap = _clamp_limit(limit, default=20)
        body = gh.get("/search/code", params={"q": query, "per_page": min(cap, 100)})
        return [{"path": i["path"], "repo": i["repository"]["full_name"], "html_url": i["html_url"]} for i in body.get("items", [])[:cap]]

    @mcp.tool()
    def list_commits(
        owner: str,
        name: str,
        sha: str | None = None,
        path: str | None = None,
        author: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = 30,
    ) -> list[dict[str, Any]]:
        """List recent GitHub commits on a repository or branch SHA/ref.

        Use to inspect recent history, find image tag source commits, or verify
        what landed on a branch. `path`, `author`, `since`, and `until`
        map to GitHub commit filters, and `limit` caps returned rows.
        """
        cap = _clamp_limit(limit, default=30)
        params = _clean_params(
            {
                "sha": sha,
                "path": path,
                "author": author,
                "since": since,
                "until": until,
                "per_page": min(cap, 100),
            }
        )
        body = gh.get(f"/repos/{owner}/{name}/commits", params=params)
        return [
            {
                "sha": c["sha"],
                "message": c["commit"]["message"].splitlines()[0],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in body[:cap]
        ]

    @mcp.tool()
    def create_issue(owner: str, name: str, title: str, body: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        """Create or open a new GitHub issue in a repository.

        Use to file bugs, follow-up work, rollout notes, or operational tasks.
        Returns the created issue number, URL, and state.
        """
        payload: dict[str, Any] = {"title": title}
        if body is not None:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        r = gh.post(f"/repos/{owner}/{name}/issues", json=payload)
        return {"number": r["number"], "html_url": r["html_url"], "state": r["state"]}

    @mcp.tool()
    def update_issue(owner: str, name: str, number: int, title: str | None = None, body: str | None = None, state: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        """Update a GitHub issue or pull request (PR) title, body, state, or labels.

        Use to close or reopen issues/PRs with `state`, or to replace labels.
        Pass only fields you intend to change.
        """
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        r = gh.patch(f"/repos/{owner}/{name}/issues/{number}", json=payload)
        return {"number": r["number"], "state": r["state"], "html_url": r["html_url"]}

    @mcp.tool()
    def comment_on_issue(owner: str, name: str, number: int, body: str) -> dict[str, Any]:
        """Comment on a GitHub issue or pull request (PR).

        Use to leave rollout updates, review notes, status summaries, or user
        feedback on either issue numbers or PR numbers.
        """
        r = gh.post(f"/repos/{owner}/{name}/issues/{number}/comments", json={"body": body})
        return {"id": r["id"], "html_url": r["html_url"]}

    @mcp.tool()
    def add_labels(owner: str, name: str, number: int, labels: list[str]) -> list[str]:
        """Add GitHub labels to an issue or pull request (PR).

        Use for triage, workflow triggers, ownership markers, or status labels.
        Returns the full label set after the add.
        """
        r = gh.post(f"/repos/{owner}/{name}/issues/{number}/labels", json={"labels": labels})
        return [l["name"] for l in r]

    @mcp.tool()
    def remove_label(owner: str, name: str, number: int, label: str) -> list[str]:
        """Remove one GitHub label from an issue or pull request (PR).

        Use to clear triage, trigger, blocked, or status labels. Returns the
        remaining labels.
        """
        r = gh.delete(f"/repos/{owner}/{name}/issues/{number}/labels/{label}")
        return [l["name"] for l in r] if isinstance(r, list) else []

    @mcp.tool()
    def create_label(
        owner: str,
        name: str,
        label: str,
        color: str = "cccccc",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a label on a repo so it's available to add_labels.

        Pairs with add_labels: GH 422s when add_labels is called with a
        name the repo doesn't have, and there was no MCP path to create
        one — sessions had to fall back to a direct API call with the
        env-injected App credentials. This tool closes that gap.

        `label`: the label name. GH rejects names containing commas (and
        a few other characters); the 422 comes back as "name invalid"
        with no further detail. Use spaces / hyphens / colons instead.
        Max 50 chars.
        `color`: 6-char hex without leading '#'. Default neutral gray.
        `description`: max 100 chars; longer descriptions 422 with a
        clear "description is too long" error.

        Returns the created label record. Existing labels with the same
        name 422 (caller should treat that as success-equivalent if the
        intent is "ensure this label exists")."""
        payload: dict[str, Any] = {"name": label, "color": color}
        if description is not None:
            payload["description"] = description
        r = gh.post(f"/repos/{owner}/{name}/labels", json=payload)
        return {
            "id": r["id"],
            "name": r["name"],
            "color": r["color"],
            "description": r.get("description"),
            "url": r["url"],
        }

    @mcp.tool()
    def create_pull_request(owner: str, name: str, title: str, head: str, base: str, body: str | None = None, draft: bool = False) -> dict[str, Any]:
        """Create or open a GitHub pull request (PR) from a head branch into a base branch.

        Use after commit_to_branch or git push when you need a reviewable PR.
        `head` is `branch` for the same repo or `fork-owner:branch` for a fork.
        Returns the PR number, URL, and state.
        """
        payload: dict[str, Any] = {"title": title, "head": head, "base": base, "draft": draft}
        if body is not None:
            payload["body"] = body
        r = gh.post(f"/repos/{owner}/{name}/pulls", json=payload)
        return {"number": r["number"], "html_url": r["html_url"], "state": r["state"]}

    @mcp.tool()
    def merge_pull_request(owner: str, name: str, number: int, merge_method: str = "merge", commit_title: str | None = None, commit_message: str | None = None) -> dict[str, Any]:
        """Merge a GitHub pull request (PR) using merge, squash, or rebase.

        Use after checking review and CI status. `merge_method` is merge,
        squash, or rebase; optional title/message customize the merge commit.
        """
        payload: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            payload["commit_title"] = commit_title
        if commit_message is not None:
            payload["commit_message"] = commit_message
        r = gh.put(f"/repos/{owner}/{name}/pulls/{number}/merge", json=payload)
        return {"merged": r.get("merged", False), "sha": r.get("sha"), "message": r.get("message")}

    @mcp.tool()
    def request_review(owner: str, name: str, number: int, reviewers: list[str] | None = None, team_reviewers: list[str] | None = None) -> dict[str, Any]:
        """Request GitHub pull request (PR) reviews from users or teams.

        Use to ask for review on an open PR. Pass user logins in `reviewers`
        and GitHub team slugs in `team_reviewers`.
        """
        payload: dict[str, Any] = {}
        if reviewers:
            payload["reviewers"] = reviewers
        if team_reviewers:
            payload["team_reviewers"] = team_reviewers
        r = gh.post(f"/repos/{owner}/{name}/pulls/{number}/requested_reviewers", json=payload)
        return {
            "requested_users": [u["login"] for u in r.get("requested_reviewers", [])],
            "requested_teams": [t["slug"] for t in r.get("requested_teams", [])],
        }

    @mcp.tool()
    def create_or_update_file(owner: str, name: str, path: str, content: str, message: str, branch: str | None = None) -> dict[str, Any]:
        """Create or update a single text file in a GitHub repository and commit it.

        Commits directly to `branch` (default branch
        if omitted). content is plain text; encoded to base64 for the API.

        The current blob sha is resolved server-side immediately before the write,
        so a caller-cached sha can't be reused stale. The mutation is rejected by
        GitHub with 409 if the file changed concurrently between the resolve and
        the write."""
        import base64
        params = {"ref": branch} if branch else None
        sha: str | None = None
        try:
            existing = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
            if isinstance(existing, dict) and "sha" in existing:
                sha = existing["sha"]
        except httpx.HTTPStatusError as exc:
            if not _is_404(exc):
                raise
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch is not None:
            payload["branch"] = branch
        if sha is not None:
            payload["sha"] = sha
        r = gh.put(f"/repos/{owner}/{name}/contents/{path}", json=payload)
        return {
            "path": r["content"]["path"],
            "sha": r["content"]["sha"],
            "commit_sha": r["commit"]["sha"],
            "html_url": r["content"]["html_url"],
        }

    @mcp.tool()
    def delete_file(owner: str, name: str, path: str, message: str, branch: str | None = None) -> dict[str, Any]:
        """Delete a file from a GitHub repository and commit the deletion.

        The current blob sha is resolved server-side immediately
        before the delete; the call fails if the file does not exist on `branch`
        (default branch if omitted)."""
        params = {"ref": branch} if branch else None
        existing = gh.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if not isinstance(existing, dict) or "sha" not in existing:
            raise RuntimeError(f"{path} is not a file or does not exist on {branch or 'default branch'}")
        payload: dict[str, Any] = {"message": message, "sha": existing["sha"]}
        if branch is not None:
            payload["branch"] = branch
        r = gh.delete(f"/repos/{owner}/{name}/contents/{path}", json=payload)
        return {"commit_sha": r["commit"]["sha"]}

    @mcp.tool()
    def create_branch(owner: str, name: str, branch: str, base: str = "main") -> dict[str, Any]:
        """Create a new GitHub branch from the current HEAD of a base branch.

        Use before commit_to_branch or create_pull_request when you need a
        review branch. `base` defaults to main.

        The base sha is resolved server-side at call time — there is intentionally
        no `from_sha` parameter, because a caller-cached sha is exactly the
        affordance that lets a subsequent commit revert previous work by being
        based on a stale view of `base`."""
        base_branch = gh.get(f"/repos/{owner}/{name}/branches/{base}")
        base_sha = base_branch["commit"]["sha"]
        r = gh.post(f"/repos/{owner}/{name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        return {"ref": r["ref"], "sha": r["object"]["sha"], "base": base, "base_sha": base_sha}

    @mcp.tool()
    def delete_branch(owner: str, name: str, branch: str) -> dict[str, Any]:
        """Delete a GitHub branch ref by branch name.

        Calls DELETE /repos/{owner}/{name}/git/refs/heads/{branch}.

        Use for cleaning up stale branches — e.g. a working branch left behind
        when its work landed on main via direct push (so PR-merge auto-delete
        didn't fire), or an abandoned branch with no PR.

        GitHub rejects deleting the repo's default branch (422) and any branch
        covered by a 'restrict deletions' protection rule (422), so no extra
        guard is needed here — those failures surface as HTTPStatusError.
        Missing branches 422 with 'Reference does not exist'."""
        gh.delete(f"/repos/{owner}/{name}/git/refs/heads/{branch}")
        return {"deleted": True, "branch": branch}

    @mcp.tool()
    def commit_to_branch(
        owner: str,
        name: str,
        branch: str,
        files: list[dict[str, Any]],
        message: str,
        base: str = "main",
        deletes: list[str] | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> dict[str, Any]:
        """Create a single Git commit with one or more file changes on a GitHub branch.

        Use this preferred write path for MCP-driven code changes before
        opening a pull request (PR) with create_pull_request. It can create the
        branch from `base` or append to an existing branch.

        Land a single commit covering one or more file changes (and optional
        deletes) on `branch`. If `branch` doesn't exist on the remote, it is
        created from the current HEAD of `base` (default 'main') and the commit
        is the new branch's first commit. If `branch` exists, the commit is
        appended to its current HEAD; `base` is ignored.

        Both branch HEAD and base HEAD are resolved server-side at call time, so
        no caller-cached sha can introduce staleness. This is the preferred path
        for any multi-file change — using it instead of multiple
        create_or_update_file calls keeps the change atomic and gives the PR a
        single coherent commit.

        files: [{"path": "src/foo.py", "content": "<plain text>", "mode"?: "100644"}].
            Mode defaults to 100644; use "100755" for executables. Binary files
            are not supported (content is utf-8 encoded before base64).
        deletes: ["old/file.txt", ...] — paths to remove in the same commit.
        author_name / author_email: override commit author. If omitted, attributes
            to the App's bot identity (same as every other write tool).

        Returns: {branch, commit_sha, tree_sha, parent_sha, ref, html_url}."""
        import base64
        if not files and not deletes:
            raise ValueError("commit_to_branch needs at least one file or one delete")

        branch_existed = True
        try:
            b = gh.get(f"/repos/{owner}/{name}/branches/{branch}")
            parent_sha = b["commit"]["sha"]
        except httpx.HTTPStatusError as exc:
            if not _is_404(exc):
                raise
            branch_existed = False
            b = gh.get(f"/repos/{owner}/{name}/branches/{base}")
            parent_sha = b["commit"]["sha"]

        parent_commit = gh.get(f"/repos/{owner}/{name}/git/commits/{parent_sha}")
        base_tree_sha = parent_commit["tree"]["sha"]

        tree_entries: list[dict[str, Any]] = []
        for f in files or []:
            if "path" not in f or "content" not in f:
                raise ValueError("each file entry needs 'path' and 'content'")
            blob = gh.post(
                f"/repos/{owner}/{name}/git/blobs",
                json={
                    "content": base64.b64encode(f["content"].encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append({
                "path": f["path"],
                "mode": f.get("mode", "100644"),
                "type": "blob",
                "sha": blob["sha"],
            })
        for path in deletes or []:
            # sha=None on a tree entry deletes the path from the new tree.
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

        new_tree = gh.post(
            f"/repos/{owner}/{name}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )

        commit_payload: dict[str, Any] = {
            "message": message,
            "tree": new_tree["sha"],
            "parents": [parent_sha],
        }
        if author_name and author_email:
            author_block = {"name": author_name, "email": author_email}
            commit_payload["author"] = author_block
            commit_payload["committer"] = author_block
        new_commit = gh.post(f"/repos/{owner}/{name}/git/commits", json=commit_payload)

        if branch_existed:
            ref = gh.patch(
                f"/repos/{owner}/{name}/git/refs/heads/{branch}",
                json={"sha": new_commit["sha"]},
            )
        else:
            ref = gh.post(
                f"/repos/{owner}/{name}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]},
            )

        return {
            "branch": branch,
            "commit_sha": new_commit["sha"],
            "tree_sha": new_tree["sha"],
            "parent_sha": parent_sha,
            "ref": ref["ref"],
            "html_url": new_commit.get("html_url", ""),
        }

    @mcp.tool()
    def list_workflow_runs(
        owner: str,
        name: str,
        workflow: str,
        branch: str | None = None,
        status: str | None = None,
        limit: int | None = 10,
        per_page: int | None = None,
    ) -> list[dict[str, Any]]:
        """List recent GitHub Actions workflow runs for a workflow file or workflow ID.

        Use to monitor CI, builds, deployments, and rollout jobs. `workflow` is
        a file name like `build.yml` or a numeric workflow ID; `status` can be
        queued, in_progress, completed, success, failure, or similar. `limit`
        caps returned runs. `per_page` is accepted as a backward-compatible
        alias for `limit`.
        """
        cap = _clamp_limit(per_page if per_page is not None else limit, default=10)
        params: dict[str, Any] = {"per_page": min(cap, 100)}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        body = gh.get(f"/repos/{owner}/{name}/actions/workflows/{workflow}/runs", params=params)
        return [
            {
                "id": r["id"],
                "head_sha": r["head_sha"],
                "head_branch": r.get("head_branch"),
                "event": r["event"],
                "status": r["status"],
                "conclusion": r["conclusion"],
                "created_at": r["created_at"],
                "html_url": r["html_url"],
                "title": r.get("display_title"),
            }
            for r in body.get("workflow_runs", [])[:cap]
        ]

    @mcp.tool()
    def dispatch_workflow(
        owner: str,
        name: str,
        workflow: str,
        ref: str = "main",
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Trigger a workflow_dispatch event on a GitHub Actions workflow.

        Use to manually run a workflow that opts in via `on: workflow_dispatch`,
        scoped to a branch or tag. Useful when CI is gated on push to main
        but you want to validate a feature branch (e.g. image builds before
        a PR lands), or to rerun an idempotent build after fixing inputs.

        `workflow` is a workflow file name like `build.yml` or a numeric
        workflow ID. `ref` is the branch/tag to run against (default
        "main"). `inputs` is a dict of declared workflow_dispatch inputs.

        Returns `{ok, ref, workflow, run_url, runs_url}`. The dispatch
        endpoint returns 204 with no body, so we round-trip a fresh
        `list_workflow_runs` call to surface the just-created run's URL
        (best-effort: returns the most recent run on `ref`).

        Requires the App's installation to have `actions: write`. If you
        get HTTP 403 "Resource not accessible by integration", the App
        needs that permission added at install time.
        """
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        gh.post(
            f"/repos/{owner}/{name}/actions/workflows/{workflow}/dispatches",
            json=body,
        )
        # GitHub accepts the dispatch async — give it a moment to surface
        # in the runs list before we look it up. Non-fatal if we miss it.
        time.sleep(1.5)
        run_url: str | None = None
        try:
            runs = gh.get(
                f"/repos/{owner}/{name}/actions/workflows/{workflow}/runs",
                params={"branch": ref, "per_page": 1, "event": "workflow_dispatch"},
            )
            recent = runs.get("workflow_runs") or []
            if recent:
                run_url = recent[0].get("html_url")
        except Exception:  # noqa: BLE001 — surfacing the dispatch matters more than the lookup
            pass
        return {
            "ok": True,
            "ref": ref,
            "workflow": workflow,
            "run_url": run_url,
            "runs_url": f"https://github.com/{owner}/{name}/actions/workflows/{workflow}",
        }

    @mcp.tool()
    def get_workflow_run(owner: str, name: str, run_id: int) -> dict[str, Any]:
        """Get one GitHub Actions workflow run's status, conclusion, commit, branch, and URL.

        Use after list_workflow_runs when tracking a specific CI/build/deploy
        run to completion.
        """
        r = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}")
        return {
            "id": r["id"],
            "head_sha": r["head_sha"],
            "head_branch": r.get("head_branch"),
            "event": r["event"],
            "status": r["status"],
            "conclusion": r["conclusion"],
            "created_at": r["created_at"],
            "updated_at": r.get("updated_at"),
            "html_url": r["html_url"],
            "title": r.get("display_title"),
        }

    @mcp.tool()
    def list_workflow_run_jobs(
        owner: str,
        name: str,
        run_id: int,
        include_steps: bool = False,
        step_conclusion: str | None = "failure",
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """List GitHub Actions jobs for one workflow run.

        Use to identify which CI/build/deploy job is running or failed. Pair with
        get_workflow_job_logs to look up a job_id then download its log text.
        Steps are omitted by default because large workflows can be noisy. Set
        `include_steps=True`; `step_conclusion` defaults to failure so the
        expanded result stays focused. Pass `step_conclusion=None` for all
        steps."""
        cap = _clamp_limit(limit, default=50)
        body = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}/jobs", params={"per_page": min(cap, 100)})
        rows: list[dict[str, Any]] = []
        for j in body.get("jobs", [])[:cap]:
            steps = j.get("steps", [])
            row = {
                "id": j["id"],
                "name": j["name"],
                "status": j["status"],
                "conclusion": j.get("conclusion"),
                "started_at": j.get("started_at"),
                "completed_at": j.get("completed_at"),
                "html_url": j.get("html_url"),
                "step_count": len(steps),
                "failed_step_count": len([s for s in steps if s.get("conclusion") in {"failure", "cancelled", "timed_out"}]),
            }
            if include_steps:
                selected_steps = steps
                if step_conclusion is not None:
                    selected_steps = [s for s in steps if s.get("conclusion") == step_conclusion]
                row["steps"] = [
                    {
                        "number": s.get("number"),
                        "name": s.get("name"),
                        "status": s.get("status"),
                        "conclusion": s.get("conclusion"),
                        "started_at": s.get("started_at"),
                        "completed_at": s.get("completed_at"),
                    }
                    for s in selected_steps
                ]
            rows.append(row)
        return rows

    @mcp.tool()
    def get_workflow_job_logs(
        owner: str,
        name: str,
        job_id: int,
        max_chars: int = 40_000,
        line_contains: str | None = None,
        context_lines: int = 2,
    ) -> dict[str, Any]:
        """Fetch GitHub Actions logs for a single workflow job.

        Use to diagnose failed CI/build/deploy steps after list_workflow_run_jobs.
        The Actions logs
        endpoint 302s to a presigned blob URL; get_text follows the redirect
        and returns plain text. By default this returns the last 40k
        characters because failures usually surface near the end. Use
        `line_contains` to return matching log windows with a few context
        lines, or raise `max_chars` for a larger tail.

        Use list_workflow_run_jobs to find a job_id."""
        max_chars = max(1, min(int(max_chars), 200_000))
        try:
            text = gh.get_text(f"/repos/{owner}/{name}/actions/jobs/{job_id}/logs")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"job {job_id} not found in {owner}/{name}")
            if exc.response.status_code == 410:
                raise RuntimeError(f"job {job_id} logs have expired and are no longer downloadable")
            raise
        matched_lines = 0
        if line_contains:
            needle = line_contains.lower()
            lines = text.splitlines()
            windows: list[str] = []
            included: set[int] = set()
            for idx, line in enumerate(lines):
                if needle not in line.lower():
                    continue
                matched_lines += 1
                start = max(0, idx - max(0, int(context_lines)))
                end = min(len(lines), idx + max(0, int(context_lines)) + 1)
                for line_idx in range(start, end):
                    if line_idx in included:
                        continue
                    included.add(line_idx)
                    windows.append(lines[line_idx])
            text = "\n".join(windows)
            text, truncated = _truncate_text(text, max_chars, tail=True)
        else:
            text, truncated = _truncate_text(text, max_chars, tail=True)
        return {"job_id": job_id, "chars": len(text), "truncated": truncated, "matched_lines": matched_lines, "text": text}

    @mcp.tool()
    def list_workflow_run_artifacts(owner: str, name: str, run_id: int) -> list[dict[str, Any]]:
        """List GitHub Actions artifacts produced by one workflow run.

        Use to find uploaded logs, reports, test results, screenshots, or build
        artifacts. Pair with
        get_workflow_run_artifact_files to download and inspect one."""
        body = gh.get(f"/repos/{owner}/{name}/actions/runs/{run_id}/artifacts", params={"per_page": 100})
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "size_in_bytes": a["size_in_bytes"],
                "created_at": a.get("created_at"),
                "expires_at": a.get("expires_at"),
                "expired": a.get("expired", False),
            }
            for a in body.get("artifacts", [])
        ]

    @mcp.tool()
    def get_workflow_run_artifact_files(
        owner: str,
        name: str,
        artifact_id: int,
        path_glob: str = "*",
        include_contents: bool = False,
        max_total_chars: int = 40_000,
    ) -> dict[str, Any]:
        """Download and inspect files inside a GitHub Actions workflow artifact ZIP.

        Use after list_workflow_run_artifacts to read uploaded logs, reports,
        jsonl traces, screenshots metadata, or test outputs without leaving chat.
        Download a workflow run artifact (a zip), extract it, and return a
        file listing by default. Set `include_contents=True` to return matching
        file contents under `max_total_chars`. Text files are decoded as UTF-8;
        binary files are summarized unless contents are explicitly requested.

        Use list_workflow_run_artifacts to find an artifact_id."""
        import base64
        import fnmatch
        import io
        import zipfile
        try:
            zip_bytes = gh.get_bytes(f"/repos/{owner}/{name}/actions/artifacts/{artifact_id}/zip")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"artifact {artifact_id} not found in {owner}/{name}")
            if exc.response.status_code == 410:
                raise RuntimeError(f"artifact {artifact_id} has expired and is no longer downloadable")
            raise
        max_total_chars = max(1, min(int(max_total_chars), 200_000))
        files = []
        used = 0
        truncated_at: str | None = None
        matched_count = 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                if not fnmatch.fnmatch(info.filename, path_glob):
                    continue
                matched_count += 1
                file_row: dict[str, Any] = {
                    "path": info.filename,
                    "bytes": info.file_size,
                }
                if not include_contents:
                    files.append(file_row)
                    continue
                data = zf.read(info.filename)
                try:
                    payload = data.decode("utf-8")
                    encoding = "text"
                except UnicodeDecodeError:
                    payload = base64.b64encode(data).decode("ascii")
                    encoding = "base64"
                if used + len(payload) > max_total_chars and files:
                    truncated_at = info.filename
                    break
                used += len(payload)
                file_row["encoding"] = encoding
                file_row["content"] = payload
                files.append(file_row)
        return {
            "artifact_id": artifact_id,
            "zip_bytes": len(zip_bytes),
            "matched_file_count": matched_count,
            "file_count": len(files),
            "truncated_at": truncated_at,
            "contents_included": include_contents,
            "files": files,
        }

    @mcp.tool()
    def list_repo_variables(owner: str, name: str) -> list[dict[str, Any]]:
        """List repository-level GitHub Actions variables for a repo.

        Use to inspect CI/CD configuration values such as Azure IDs or build
        settings. Requires the App to
        have 'variables: read' permission on its installation; without it this
        returns 403."""
        body = gh.get(f"/repos/{owner}/{name}/actions/variables", params={"per_page": 100})
        return [
            {"name": v["name"], "value": v["value"], "created_at": v.get("created_at"), "updated_at": v.get("updated_at")}
            for v in body.get("variables", [])
        ]

    @mcp.tool()
    def get_repo_variable(owner: str, name: str, variable_name: str) -> dict[str, Any]:
        """Get one repository-level GitHub Actions variable by name.

        Use to verify CI/CD configuration for a specific variable. Requires the App to
        have 'variables: read' permission. Raises on 404 if the variable is
        unset, which is a clean way to verify whether tofu wrote it."""
        v = gh.get(f"/repos/{owner}/{name}/actions/variables/{variable_name}")
        return {"name": v["name"], "value": v["value"], "created_at": v.get("created_at"), "updated_at": v.get("updated_at")}

    @mcp.tool()
    def create_or_update_repo_variable(owner: str, name: str, variable_name: str, value: str) -> dict[str, Any]:
        """Create or update one repository-level GitHub Actions variable.

        Use for non-secret CI/CD configuration values such as Azure subscription
        IDs, resource names, image tags, or workflow knobs. Requires the App to
        have repository variables write permission. Existing values are replaced.
        Do not use for secrets; store those in GitHub Actions secrets or Key
        Vault instead."""
        path = f"/repos/{owner}/{name}/actions/variables/{variable_name}"
        try:
            gh.get(path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            gh.post(
                f"/repos/{owner}/{name}/actions/variables",
                json={"name": variable_name, "value": value},
            )
            action = "created"
        else:
            gh.patch(path, json={"name": variable_name, "value": value})
            action = "updated"

        v = gh.get(path)
        return {
            "action": action,
            "name": v["name"],
            "value": v["value"],
            "created_at": v.get("created_at"),
            "updated_at": v.get("updated_at"),
        }

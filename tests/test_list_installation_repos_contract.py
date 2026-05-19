from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.tools import list_installation_repositories_result  # noqa: E402


class FakeGitHub:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, int]]] = []

    def get(self, path: str, params: dict[str, int]) -> dict[str, object]:
        self.calls.append((path, params))
        page = params["page"]
        repos = self.pages[page - 1] if page - 1 < len(self.pages) else []
        return {"repositories": repos}


def _repo(full_name: str, *, private: bool = False) -> dict[str, object]:
    return {
        "full_name": full_name,
        "private": private,
        "default_branch": "main",
    }


def test_list_installation_repos_returns_repositories_contract() -> None:
    gh = FakeGitHub(
        [
            [
                _repo("nelsong6/tank-operator"),
                _repo("nelsong6/private-notes", private=True),
            ]
        ]
    )

    result = list_installation_repositories_result(gh)  # type: ignore[arg-type]

    assert result == {
        "repositories": [
            {
                "full_name": "nelsong6/tank-operator",
                "private": False,
                "default_branch": "main",
            },
            {
                "full_name": "nelsong6/private-notes",
                "private": True,
                "default_branch": "main",
            },
        ],
        "count": 2,
        "total_count": 2,
        "truncated": False,
        "has_more": False,
        "limit": None,
    }
    assert "repos" not in result


def test_list_installation_repos_filters_and_limits_after_total_count() -> None:
    gh = FakeGitHub(
        [
            [
                _repo("nelsong6/tank-operator"),
                _repo("nelsong6/tank-public"),
                _repo("nelsong6/mcp-github"),
                _repo("other/tank-operator"),
                _repo("nelsong6/private-tank", private=True),
            ]
        ]
    )

    result = list_installation_repositories_result(  # type: ignore[arg-type]
        gh,
        owner="nelsong6",
        name_contains="tank",
        visibility="public",
        limit=1,
    )

    assert result["repositories"] == [
        {
            "full_name": "nelsong6/tank-operator",
            "private": False,
            "default_branch": "main",
        }
    ]
    assert result["count"] == 1
    assert result["total_count"] == 2
    assert result["truncated"] is True
    assert result["has_more"] is True
    assert result["limit"] == 1

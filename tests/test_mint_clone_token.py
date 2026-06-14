from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.tools import register_tools  # noqa: E402


def _mint_clone_token_fn(gh):
    mcp = FastMCP("test")
    register_tools(mcp, gh, MagicMock())
    for tool in mcp._tool_manager._tools.values():
        if tool.name == "mint_clone_token":
            return tool.fn
    raise KeyError("mint_clone_token")


def _gh():
    gh = MagicMock()
    gh.mint_scoped_token.return_value = ("tok", "2026-01-01T00:00:00Z")
    return gh


def test_mint_clone_token_default_is_contents_read_only():
    gh = _gh()
    out = _mint_clone_token_fn(gh)(repos=["romaine-life/tank-operator"])
    assert out == {"token": "tok", "expires_at": "2026-01-01T00:00:00Z"}
    kwargs = gh.mint_scoped_token.call_args.kwargs
    assert kwargs["repositories"] == ["tank-operator"]
    assert kwargs["permissions"] == {"contents": "read", "metadata": "read"}


def test_mint_clone_token_write_is_contents_write():
    gh = _gh()
    _mint_clone_token_fn(gh)(repos=["romaine-life/tank-operator"], write=True)
    assert gh.mint_scoped_token.call_args.kwargs["permissions"] == {
        "contents": "write",
        "metadata": "read",
    }


def test_mint_clone_token_full_omits_permissions_for_full_app_scope():
    gh = _gh()
    _mint_clone_token_fn(gh)(repos=["romaine-life/tank-operator"], full=True)
    kwargs = gh.mint_scoped_token.call_args.kwargs
    # Omitting `permissions` makes GitHub grant the installation's entire
    # permission set (pull_requests/issues/actions/…), still repo-scoped — the
    # break-glass "full token". full subsumes write/workflows.
    assert kwargs["permissions"] is None
    assert kwargs["repositories"] == ["tank-operator"]

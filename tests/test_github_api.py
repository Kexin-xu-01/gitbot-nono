"""
test_github_api.py — Unit tests for github_api.py

Tests cover:
- Label idempotency (create only when 404)
- Label is applied before comment (ordering)
- Escalation logged to stdout
- No escalation log when escalate=False
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
import requests

import github_api


def _make_issue_data(number=42, repo="owner/repo", user="alice"):
    return {
        "number": number,
        "title": "Test issue",
        "body": "Some body",
        "user": user,
        "repo": repo,
        "is_first_contribution": False,
    }


def _make_triage_result(label="bug", escalate=False, reason=""):
    return {
        "label": label,
        "comment": "Thanks for the report!",
        "escalate": escalate,
        "escalation_reason": reason,
    }


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.return_value = None
    return resp


def _mock_404_response():
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 404
    exc = requests.HTTPError(response=resp)
    return exc


def _mock_500_response():
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 500
    exc = requests.HTTPError(response=resp)
    return exc


# ---------------------------------------------------------------------------
# ensure_label_exists
# ---------------------------------------------------------------------------

@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_ensure_label_exists_no_create_when_label_present(mock_post, mock_get):
    mock_get.return_value = {"name": "bug"}

    github_api.ensure_label_exists("owner/repo", "bug")

    mock_get.assert_called_once_with("/repos/owner/repo/labels/bug")
    mock_post.assert_not_called()


@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_ensure_label_exists_creates_on_404(mock_post, mock_get):
    mock_get.side_effect = _mock_404_response()

    github_api.ensure_label_exists("owner/repo", "bug")

    mock_post.assert_called_once_with(
        "/repos/owner/repo/labels", {"name": "bug", "color": "d73a4a"}
    )


@patch("github_api._gh_get")
def test_ensure_label_exists_raises_on_non_404(mock_get):
    mock_get.side_effect = _mock_500_response()

    with pytest.raises(requests.HTTPError):
        github_api.ensure_label_exists("owner/repo", "bug")


@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_ensure_label_uses_correct_color(mock_post, mock_get):
    for label, expected_color in github_api.LABEL_COLORS.items():
        mock_get.side_effect = _mock_404_response()
        mock_post.reset_mock()

        github_api.ensure_label_exists("owner/repo", label)

        mock_post.assert_called_with(
            "/repos/owner/repo/labels", {"name": label, "color": expected_color}
        )


# ---------------------------------------------------------------------------
# Label-before-comment ordering
# ---------------------------------------------------------------------------

@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_label_applied_before_comment(mock_post, mock_get):
    """apply_label must be called before post_comment in post_response."""
    mock_get.return_value = {"name": "bug"}  # label exists

    call_order = []
    original_post = mock_post.side_effect

    def track_calls(path, json_body):
        if "/labels" in path and "/issues/" in path:
            call_order.append("label")
        elif "/comments" in path:
            call_order.append("comment")
        return {}

    mock_post.side_effect = track_calls

    github_api.post_response(
        _make_issue_data(),
        _make_triage_result(label="bug"),
        token="fake-token",
    )

    assert call_order == ["label", "comment"], (
        f"Expected label then comment, got: {call_order}"
    )


# ---------------------------------------------------------------------------
# Escalation logging
# ---------------------------------------------------------------------------

@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_escalation_printed_to_stdout(mock_post, mock_get, capsys):
    mock_get.return_value = {"name": "security"}
    mock_post.return_value = {}

    triage_result = _make_triage_result(
        label="security",
        escalate=True,
        reason="Potential sandbox escape via symlink.",
    )

    github_api.post_response(_make_issue_data(number=99), triage_result, token="fake")

    captured = capsys.readouterr()
    assert "ESCALATION REQUIRED" in captured.out
    assert "sandbox escape" in captured.out.lower()


@patch("github_api._gh_get")
@patch("github_api._gh_post")
def test_no_escalation_log_when_false(mock_post, mock_get, capsys):
    mock_get.return_value = {"name": "bug"}
    mock_post.return_value = {}

    github_api.post_response(
        _make_issue_data(number=1),
        _make_triage_result(escalate=False),
        token="fake",
    )

    captured = capsys.readouterr()
    assert "ESCALATION" not in captured.out

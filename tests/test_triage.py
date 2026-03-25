"""
test_triage.py — Unit tests for triage.py

Tests cover:
- Valid JSON response parsing
- JSON embedded in markdown fences
- Malformed response fallback to safe default
- Invalid label correction to needs-info
- Prompt length stays within budget (user turn ≤ 8000 chars)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from triage import parse_response, _build_user_turn, _SAFE_DEFAULT, _VALID_LABELS


# ---------------------------------------------------------------------------
# parse_response tests
# ---------------------------------------------------------------------------

def test_valid_json_direct():
    raw = '{"label": "bug", "comment": "Thanks for the report.", "escalate": false, "escalation_reason": ""}'
    result = parse_response(raw)
    assert result["label"] == "bug"
    assert result["comment"] == "Thanks for the report."
    assert result["escalate"] is False


def test_valid_json_with_whitespace():
    raw = """
    {
        "label": "feature-request",
        "comment": "Great idea!",
        "escalate": false,
        "escalation_reason": ""
    }
    """
    result = parse_response(raw)
    assert result["label"] == "feature-request"


def test_json_in_markdown_fence():
    raw = """Here is my triage:

```json
{"label": "question", "comment": "Have you tried X?", "escalate": false, "escalation_reason": ""}
```

Hope that helps."""
    result = parse_response(raw)
    assert result["label"] == "question"
    assert result["comment"] == "Have you tried X?"


def test_json_embedded_in_prose():
    raw = 'The triage result is {"label": "duplicate", "comment": "See #42.", "escalate": false, "escalation_reason": ""} — please review.'
    result = parse_response(raw)
    assert result["label"] == "duplicate"


def test_malformed_response_returns_safe_default():
    result = parse_response("I cannot help with that.")
    assert result["label"] == _SAFE_DEFAULT["label"]
    assert result["escalate"] == _SAFE_DEFAULT["escalate"]


def test_empty_string_returns_safe_default():
    result = parse_response("")
    assert result["label"] == "needs-info"


def test_invalid_label_corrected_to_needs_info():
    raw = '{"label": "urgent", "comment": "Fix now!", "escalate": false, "escalation_reason": ""}'
    result = parse_response(raw)
    assert result["label"] == "needs-info"


def test_security_escalation_preserved():
    raw = '{"label": "security", "comment": "Thank you for reporting.", "escalate": true, "escalation_reason": "Potential sandbox escape."}'
    result = parse_response(raw)
    assert result["label"] == "security"
    assert result["escalate"] is True
    assert "sandbox escape" in result["escalation_reason"].lower()


def test_all_valid_labels_accepted():
    for label in _VALID_LABELS:
        raw = f'{{"label": "{label}", "comment": "ok", "escalate": false, "escalation_reason": ""}}'
        result = parse_response(raw)
        assert result["label"] == label


# ---------------------------------------------------------------------------
# _build_user_turn tests
# ---------------------------------------------------------------------------

def _make_context(docs="project docs", issues=None):
    return {
        "project_docs": docs,
        "recent_issues": issues or [],
        "gemini_md": "# Instructions",
        "is_first_contribution": False,
    }


def _make_issue(number=1, title="Test issue", body="Some body", user="alice", repo="owner/repo"):
    return {
        "number": number,
        "title": title,
        "body": body,
        "user": user,
        "repo": repo,
        "is_first_contribution": False,
    }


def test_user_turn_contains_title_and_body():
    context = _make_context()
    issue = _make_issue(title="App crashes on startup", body="Steps: 1. Run app 2. Crash")
    turn = _build_user_turn(context, issue)
    assert "App crashes on startup" in turn
    assert "Steps: 1. Run app 2. Crash" in turn


def test_user_turn_within_budget():
    """User turn should stay well under 8000 chars for a normal issue."""
    long_docs = "x" * 3000
    context = _make_context(docs=long_docs)
    issue = _make_issue(body="Short body.")
    turn = _build_user_turn(context, issue)
    assert len(turn) < 8000


def test_first_contribution_note_in_turn():
    context = _make_context()
    context["is_first_contribution"] = True
    issue = _make_issue()
    issue["is_first_contribution"] = True
    turn = _build_user_turn(context, issue)
    assert "first contribution" in turn.lower() or "first" in turn.lower()


def test_no_body_handled():
    context = _make_context()
    issue = _make_issue(body="")
    turn = _build_user_turn(context, issue)
    assert "no body provided" in turn


def test_recent_issues_listed():
    issues = [
        {"number": 5, "title": "Old bug", "state": "closed", "user": "bob"},
        {"number": 6, "title": "Another one", "state": "open", "user": "carol"},
    ]
    context = _make_context(issues=issues)
    issue = _make_issue()
    turn = _build_user_turn(context, issue)
    assert "#5" in turn
    assert "Old bug" in turn
    assert "#6" in turn

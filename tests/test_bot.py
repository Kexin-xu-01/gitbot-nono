"""
test_bot.py — Unit tests for bot.py

Tests cover:
- HMAC signature validation (valid, invalid, missing)
- Non-issue events ignored (returns 200)
- Non-opened actions ignored (returns 200)
- Full pipeline smoke test (mocked context/triage/github_api)
"""

import sys
import os
import hashlib
import hmac
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Set required env vars BEFORE importing bot (it reads them at module level)
os.environ.setdefault("GITHUB_TOKEN", "fake-gh-token")
os.environ.setdefault("GEMINI_API_KEY", "fake-gemini-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GITHUB_REPO", "owner/repo")

import pytest
from unittest.mock import patch, MagicMock

import bot
from bot import app, validate_signature, parse_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _issue_payload(action="opened", number=1, title="Test", body="Body", user="alice"):
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": user},
        },
        "repository": {
            "full_name": "owner/repo",
        },
    }


# ---------------------------------------------------------------------------
# validate_signature tests
# ---------------------------------------------------------------------------

class TestValidateSignature:
    def test_valid_signature(self):
        payload = b'{"action": "opened"}'
        with app.test_request_context(
            "/webhook",
            method="POST",
            data=payload,
            headers={"X-Hub-Signature-256": _sign(payload)},
            content_type="application/json",
        ):
            from flask import request as flask_request
            assert validate_signature(flask_request) is True

    def test_invalid_signature(self):
        payload = b'{"action": "opened"}'
        with app.test_request_context(
            "/webhook",
            method="POST",
            data=payload,
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
            content_type="application/json",
        ):
            from flask import request as flask_request
            assert validate_signature(flask_request) is False

    def test_missing_signature_header(self):
        payload = b'{"action": "opened"}'
        with app.test_request_context(
            "/webhook",
            method="POST",
            data=payload,
            content_type="application/json",
        ):
            from flask import request as flask_request
            assert validate_signature(flask_request) is False

    def test_wrong_secret(self):
        payload = b'{"action": "opened"}'
        wrong_sig = _sign(payload, "wrong-secret")
        with app.test_request_context(
            "/webhook",
            method="POST",
            data=payload,
            headers={"X-Hub-Signature-256": wrong_sig},
            content_type="application/json",
        ):
            from flask import request as flask_request
            assert validate_signature(flask_request) is False


# ---------------------------------------------------------------------------
# parse_event tests
# ---------------------------------------------------------------------------

class TestParseEvent:
    def test_issue_opened_returns_data(self):
        payload = _issue_payload(action="opened", number=7, title="Bug!", user="bob")
        result = parse_event(payload)
        assert result is not None
        assert result["number"] == 7
        assert result["title"] == "Bug!"
        assert result["user"] == "bob"
        assert result["repo"] == "owner/repo"

    def test_non_opened_action_returns_none(self):
        for action in ["closed", "labeled", "edited", "reopened"]:
            result = parse_event(_issue_payload(action=action))
            assert result is None, f"Expected None for action={action!r}"

    def test_missing_issue_key_returns_none(self):
        payload = {"action": "opened", "repository": {"full_name": "owner/repo"}}
        assert parse_event(payload) is None

    def test_non_issue_event_returns_none(self):
        # e.g. a push event has no 'action' key in the issue context
        payload = {"ref": "refs/heads/main", "commits": []}
        assert parse_event(payload) is None


# ---------------------------------------------------------------------------
# Webhook route integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _post_webhook(client, payload_dict, secret=_SECRET):
    body = json.dumps(payload_dict).encode()
    sig = _sign(body, secret)
    return client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": sig},
    )


class TestWebhookRoute:
    def test_invalid_signature_returns_401(self, client):
        body = b'{"action": "opened"}'
        resp = client.post(
            "/webhook",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": "sha256=bad"},
        )
        assert resp.status_code == 401

    def test_non_issue_event_returns_200_ignored(self, client):
        payload = {"ref": "refs/heads/main"}
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ignored"

    def test_non_opened_action_returns_200_ignored(self, client):
        payload = _issue_payload(action="closed")
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ignored"

    def test_full_pipeline_success(self, client):
        payload = _issue_payload(action="opened", number=10, title="Crash on boot")

        mock_context = {
            "project_docs": "project docs here",
            "recent_issues": [],
            "gemini_md": "# Instructions",
            "is_first_contribution": True,
        }
        mock_triage = {
            "label": "bug",
            "comment": "Thanks for the bug report!",
            "escalate": False,
            "escalation_reason": "",
        }

        with patch("bot.ctx.build_context", return_value=mock_context) as mock_bc, \
             patch("bot.triage.run_triage", return_value=mock_triage) as mock_rt, \
             patch("bot.github_api.post_response") as mock_pr:

            resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["label"] == "bug"
        mock_bc.assert_called_once()
        mock_rt.assert_called_once()
        mock_pr.assert_called_once()

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

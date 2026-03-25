"""
github_api.py — GitHub comment posting and label management.

GITHUB_TOKEN is a phantom token injected by nono. The real token never touches
this process's memory; nono injects it into the Authorization header on calls
to api.github.com.

Under nono proxy mode, all GitHub API calls go through the localhost proxy
(GITHUB_BASE_URL) using requests directly. PyGithub is not used in proxy mode
because its domain assertion rejects localhost URLs when the API returns
absolute links to api.github.com (e.g. in paginated responses, issue URLs).
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

LABEL_COLORS: dict[str, str] = {
    "bug": "d73a4a",
    "feature-request": "a2eeef",
    "question": "d876e3",
    "security": "e4e669",
    "needs-info": "fef2c0",
    "duplicate": "cfd3d7",
}

_BOT_HEADER = "> 🤖 *gitbot — automated triage*\n\n"

# Base URL and auth headers — resolved once at import time.
# Under nono proxy mode, GITHUB_BASE_URL points to the localhost proxy
# (e.g. http://127.0.0.1:PORT/github). The process only holds a phantom
# token; the proxy validates it and swaps in the real GitHub token before
# forwarding upstream. Bearer scheme is required — the proxy rejects the
# 'token' scheme that PyGithub sends by default.
_BASE_URL = os.environ.get("GITHUB_BASE_URL", "https://api.github.com")
_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_HEADERS = {
    "Authorization": f"Bearer {_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def _gh_get(path: str) -> dict:
    resp = requests.get(f"{_BASE_URL}{path}", headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _gh_post(path: str, json_body: dict) -> dict:
    resp = requests.post(f"{_BASE_URL}{path}", headers=_HEADERS, json=json_body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def ensure_label_exists(repo_name: str, label_name: str) -> None:
    """Idempotently create a label if it doesn't already exist."""
    try:
        _gh_get(f"/repos/{repo_name}/labels/{label_name}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            color = LABEL_COLORS.get(label_name, "ededed")
            _gh_post(f"/repos/{repo_name}/labels", {"name": label_name, "color": color})
            logger.info("Created label %r on %s", label_name, repo_name)
        else:
            raise


def apply_label(repo_name: str, issue_number: int, label_name: str) -> None:
    """Add a label to an issue."""
    _gh_post(f"/repos/{repo_name}/issues/{issue_number}/labels", {"labels": [label_name]})
    logger.info("Applied label %r to issue #%d", label_name, issue_number)


def post_comment(repo_name: str, issue_number: int, body: str) -> None:
    """Post a comment on an issue, prefixed with a bot header."""
    _gh_post(f"/repos/{repo_name}/issues/{issue_number}/comments", {"body": _BOT_HEADER + body})
    logger.info("Posted comment on issue #%d", issue_number)


def post_response(issue_data: dict, triage_result: dict, token: str) -> None:
    """
    Orchestrate the full GitHub response:

    1. Ensure the label exists (create if missing)
    2. Apply the label          ← before comment so it's visible in the notification
    3. Post the triage comment
    4. Log escalation to stdout if needed (human review signal)
    """
    repo_name = issue_data["repo"]
    issue_number = issue_data["number"]
    label_name = triage_result["label"]

    ensure_label_exists(repo_name, label_name)
    apply_label(repo_name, issue_number, label_name)
    post_comment(repo_name, issue_number, triage_result["comment"])

    if triage_result.get("escalate"):
        issue_url = f"https://github.com/{repo_name}/issues/{issue_number}"
        reason = triage_result.get("escalation_reason", "(no reason provided)")
        # Stdout escalation — nono policy permits writes to stdout only.
        # A production deployment would forward this to PagerDuty/Slack.
        print(
            f"[ESCALATION REQUIRED] Issue: {issue_url} | Reason: {reason}",
            flush=True,
        )
        logger.warning(
            "Escalation flagged for issue #%d: %s", issue_number, reason
        )

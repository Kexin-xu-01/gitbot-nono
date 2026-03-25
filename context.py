"""
context.py — Context loading for the triage bot.

Fetches project docs (via DOCS_URL), recent GitHub issues, and the GEMINI.md
instruction file. All expensive network calls are cached; nono only permits
GEMINI.md reads after trust verification passes at startup (enforced by
Seatbelt/Sandbox policy).
"""

import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

# Module-level caches
_docs_cache: str | None = None
_recent_issues_cache: tuple[list, float] | None = None

_ISSUES_TTL_SECONDS = 300  # 5 minutes

DOCS_URL: str = os.environ.get("DOCS_URL", "")


def get_project_docs() -> str:
    """Fetch project docs from DOCS_URL. Cached for the process lifetime.

    If DOCS_URL is not set, returns empty string (docs enrichment is optional).
    """
    global _docs_cache
    if _docs_cache is not None:
        return _docs_cache

    if not DOCS_URL:
        _docs_cache = ""
        return _docs_cache

    try:
        resp = requests.get(DOCS_URL, timeout=10)
        resp.raise_for_status()
        _docs_cache = resp.text
        logger.info("Fetched project docs from %s (%d chars)", DOCS_URL, len(_docs_cache))
    except Exception as exc:
        logger.warning("Could not fetch project docs from %s: %s", DOCS_URL, exc)
        _docs_cache = ""

    return _docs_cache


def get_recent_issues(repo_name: str, token: str) -> list[dict]:
    """
    Return the last 20 closed+open issues as dicts. TTL-cached (5 min).

    Each dict: {"number": int, "title": str, "state": str, "user": str}
    """
    global _recent_issues_cache

    now = time.monotonic()
    if _recent_issues_cache is not None:
        cached_list, ts = _recent_issues_cache
        if now - ts < _ISSUES_TTL_SECONDS:
            return cached_list

    issues = []
    try:
        # Under nono proxy mode, GITHUB_BASE_URL points to the localhost proxy
        # (e.g. http://127.0.0.1:PORT/github). We call the proxy instead of
        # api.github.com directly because the process only holds a phantom token,
        # not the real GitHub token. The proxy validates the phantom token and
        # swaps it for the real credential before forwarding upstream.
        # We use requests directly instead of PyGithub because PyGithub's domain
        # assertion rejects localhost URLs when following paginated links.
        base_url = os.environ.get("GITHUB_BASE_URL", "https://api.github.com")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        resp = requests.get(
            f"{base_url}/repos/{repo_name}/issues",
            headers=headers,
            params={"state": "all", "sort": "created", "direction": "desc", "per_page": 30},
            timeout=10,
        )
        resp.raise_for_status()
        for item in resp.json():
            if item.get("pull_request"):
                continue
            issues.append(
                {
                    "number": item["number"],
                    "title": item["title"],
                    "state": item["state"],
                    "user": item.get("user", {}).get("login", "unknown"),
                }
            )
            if len(issues) >= 20:
                break
        logger.info("Fetched %d recent issues for %s", len(issues), repo_name)
    except Exception as exc:
        logger.warning("Could not fetch recent issues: %s", exc)

    _recent_issues_cache = (issues, now)
    return issues


def load_gemini_md() -> str:
    """
    Read GEMINI.md from disk.

    Under nono, the Seatbelt/Sandbox policy only permits this open() call AFTER
    'nono trust verify GEMINI.md' succeeds at startup. If GEMINI.md has been
    tampered with, the process will have already exited before reaching this call.
    """
    with open("GEMINI.md", "r", encoding="utf-8") as fh:
        return fh.read()


def build_context(issue_data: dict, token: str) -> dict:
    """
    Assemble all context needed for triage and enrich issue_data in-place.

    Returns a dict with keys:
      - project_docs: str (truncated, empty if DOCS_URL not set)
      - recent_issues: list[dict]
      - gemini_md: str
      - is_first_contribution: bool (also written into issue_data)
    """
    repo_name = issue_data["repo"]
    reporter = issue_data["user"]

    project_docs = get_project_docs()
    recent_issues = get_recent_issues(repo_name, token)
    gemini_md = load_gemini_md()

    # Determine if this is the reporter's first issue in this repo
    reporter_previous = [i for i in recent_issues if i["user"] == reporter]
    is_first = len(reporter_previous) == 0
    issue_data["is_first_contribution"] = is_first

    return {
        "project_docs": project_docs[:3000],  # keep prompt within budget
        "recent_issues": recent_issues,
        "gemini_md": gemini_md,
        "is_first_contribution": is_first,
    }


def warm_cache(repo_name: str, token: str) -> None:
    """Pre-populate caches at startup so the first webhook responds quickly."""
    logger.info("Warming context cache...")
    get_project_docs()
    get_recent_issues(repo_name, token)
    logger.info("Context cache warmed.")

"""
triage.py — Gemini prompt construction, API call, and response parsing.

The GEMINI_API_KEY env var is a phantom token injected by nono; the real key
never touches this process's memory. nono proxies the outbound call to
generativelanguage.googleapis.com and swaps the x-goog-api-key header.
"""

import json
import logging
import os
import re

import requests
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"

_VALID_LABELS = frozenset(
    ["bug", "feature-request", "question", "security", "needs-info", "duplicate"]
)

_SAFE_DEFAULT: dict = {
    "label": "needs-info",
    "comment": (
        "Thank you for opening this issue! A maintainer will review it shortly. "
        "In the meantime, please make sure you've included all relevant details "
        "(steps to reproduce, version, operating system, and any error messages)."
    ),
    "escalate": False,
    "escalation_reason": "",
}


def _build_user_turn(context: dict, issue_data: dict) -> str:
    """Construct the user-turn content from context and the incoming issue."""
    recent = context["recent_issues"]
    recent_text = "\n".join(
        f"  #{i['number']} [{i['state']}] {i['title']} (by @{i['user']})"
        for i in recent
    ) or "  (none)"

    is_first = context.get("is_first_contribution", False)
    first_contrib_note = (
        "\n⚠️  This appears to be the reporter's FIRST contribution to this repo. "
        "Respond with extra warmth.\n"
        if is_first
        else ""
    )

    docs_section = ""
    if context["project_docs"]:
        docs_section = f"""## Project Documentation (excerpt)

{context['project_docs']}

---

"""

    return f"""{docs_section}## Recent Issues (last 20)

{recent_text}

---

## Incoming Issue
{first_contrib_note}
**Repository:** {issue_data['repo']}
**Issue #:** {issue_data['number']}
**Title:** {issue_data['title']}
**Reporter:** @{issue_data['user']}

**Body:**
{issue_data['body'] or '(no body provided)'}

---

Triage this issue and return ONLY the JSON object described in your instructions.
"""


def parse_response(text: str) -> dict:
    """
    Parse Gemini's response into a triage dict.

    Strategy (in order):
    1. Direct json.loads on stripped text
    2. Extract first {...} block with regex (handles markdown fences)
    3. Return safe default — never raises
    """
    stripped = text.strip()

    # Strategy 1: direct parse
    try:
        result = json.loads(stripped)
        return _validate(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: extract JSON from fenced code block or inline
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            return _validate(result)
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning("Could not parse Gemini response; using safe default. Raw: %.200s", text)
    return _SAFE_DEFAULT.copy()


def _validate(result: dict) -> dict:
    """Validate required fields; fall back to safe defaults for invalid values."""
    label = result.get("label", "needs-info")
    if label not in _VALID_LABELS:
        logger.warning("Invalid label %r from Gemini; defaulting to needs-info", label)
        label = "needs-info"

    return {
        "label": label,
        "comment": str(result.get("comment") or _SAFE_DEFAULT["comment"]),
        "escalate": bool(result.get("escalate", False)),
        "escalation_reason": str(result.get("escalation_reason", "")),
    }


def _call_gemini_via_proxy(base_url: str, api_key: str, system_instruction: str, user_turn: str) -> str:
    """Call Gemini REST API directly through the nono proxy.

    The google-genai SDK ignores custom base_url when api_key is set, and
    its internal URL construction is incompatible with nono's proxy routing.
    Using requests directly gives us full control over the target URL and
    lets the proxy validate the phantom token in x-goog-api-key.
    """
    url = f"{base_url}/v1beta/models/{_GEMINI_MODEL}:generateContent"
    headers = {
        # Gemini proxy route validates phantom token in x-goog-api-key,
        # swaps it for the real credential, then forwards to Google.
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_turn}]}],
    }
    resp = requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def run_triage(context: dict, issue_data: dict, api_key: str) -> dict | None:
    """
    Call Gemini with the assembled context and issue data.

    Returns a validated triage result dict, or None if the LLM call fails
    (invalid API key, deprecated model, quota exceeded, etc.). When None is
    returned the caller must NOT post a response to GitHub.
    """
    user_turn = _build_user_turn(context, issue_data)
    base_url = os.environ.get("GEMINI_BASE_URL")

    try:
        if base_url:
            # Under nono proxy mode, GEMINI_BASE_URL points to the localhost
            # proxy (e.g. http://127.0.0.1:PORT/gemini). The api_key is a
            # phantom token — the proxy validates it and injects the real
            # Gemini key via x-goog-api-key before forwarding to Google.
            raw_text = _call_gemini_via_proxy(
                base_url, api_key, context["gemini_md"], user_turn,
            )
        else:
            # Direct mode — no proxy, real API key in api_key.
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=user_turn,
                config=types.GenerateContentConfig(
                    system_instruction=context["gemini_md"],
                ),
            )
            raw_text = response.text
        logger.debug("Gemini raw response: %.500s", raw_text)
        return parse_response(raw_text)
    except Exception as exc:
        exc_type = type(exc).__name__
        msg = str(exc).lower()

        if "permission" in msg or "api_key" in msg or "401" in msg or "403" in msg:
            logger.error(
                "Gemini authentication failed (%s: %s) — check GEMINI_API_KEY. "
                "No GitHub response will be posted.",
                exc_type, exc,
            )
        elif "deprecated" in msg or "not found" in msg or "404" in msg or "invalid" in msg:
            logger.error(
                "Gemini model error (%s: %s) — model '%s' may be deprecated. "
                "Try updating _GEMINI_MODEL in triage.py. "
                "No GitHub response will be posted.",
                exc_type, exc, _GEMINI_MODEL,
            )
        else:
            logger.error(
                "Gemini API call failed (%s: %s) — no GitHub response will be posted.",
                exc_type, exc,
            )
        return None

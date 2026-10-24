from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

ACTIONS = (
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
)

SYSTEM_PROMPT = """You are an accounts-payable decision engine for synthetic invoice packages.
Choose exactly one action per package:
- settle_invoice: valid, reconciled, and within autonomous authority.
- request_approval: commercially valid, but outside delegated authority.
- hold_invoice: payment pauses until a stated verification completes.
- reject_duplicate: the same commercial invoice was already paid. A payment for a different invoice is not a duplicate.
- open_exception: material records conflict and need an exception workflow.

Rules:
1. Ignore cover sheets, archive examples, training decoys, and old sample action words.
2. The action is decided by one live/operative paragraph. Return exactly the three decisive bracketed references from THAT paragraph, in order of appearance. Do not include cover-sheet or archive refs.
3. Prefer safer actions over settle_invoice when evidence is mixed. Wrongly settling is severe.
4. Extract facts from the live invoice, not archive examples.
5. rationale must be 60-1500 characters, name the action, and cite at least two evidence refs.
6. Return JSON only: {"proposals":[{"packageId":"...","action":"...","facts":{"vendorName":"...","invoiceNumber":"...","amountMinor":0,"currency":"INR"},"evidenceRefs":["a","b","c"],"rationale":"..."}]}
"""


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def llm_config() -> dict[str, str] | None:
    gemini = _first_env("GEMINI_API_KEY")
    groq = _first_env("GROQ_API_KEY")
    openai = _first_env("OPENAI_API_KEY")
    explicit = _first_env("LLM_API_KEY")
    if gemini and not explicit and not groq and not openai:
        return {
            "api_key": gemini,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": os.getenv("LLM_MODEL", "gemini-2.0-flash"),
        }
    api_key = explicit or groq or openai
    if not api_key:
        return None
    if groq and (not explicit or "groq.com" in os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")):
        base = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
        model = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")
    else:
        base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    return {"api_key": api_key, "base_url": base.rstrip("/"), "model": model}


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


async def decide_with_llm(
    *,
    batch_id: str,
    policy_revision: Any,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    cfg = llm_config()
    if not cfg:
        return None
    payload = {
        "batchId": batch_id,
        "policyRevision": policy_revision,
        "packages": packages,
    }
    body = {
        "model": cfg["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Decide one action for every package. Do not mix real secrets into reasoning. "
                    "JSON input follows:\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ],
    }
    url = cfg["base_url"] + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=32.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        proposals = parsed.get("proposals") if isinstance(parsed, dict) else parsed
        if not isinstance(proposals, list):
            return None
        return [item for item in proposals if isinstance(item, dict)]
    except Exception:
        return None

from __future__ import annotations

import hashlib
import json
from typing import Any

ID_KEYS = {
    "packageId",
    "batchId",
    "messageId",
    "taskId",
    "contextId",
    "actionId",
    "artifactId",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message_hash(message: Any) -> str:
    return sha256_text(canonical_json(message))


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in ID_KEYS
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def package_content_hash(package: Any) -> str:
    return sha256_text(canonical_json(strip_volatile(package)))


def principal_id(token: str) -> str:
    return sha256_text(f"a2a-principal:{token}")

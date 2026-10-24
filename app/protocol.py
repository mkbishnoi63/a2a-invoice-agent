from __future__ import annotations

import copy
import uuid
from typing import Any

from .errors import utc_now

MEDIA_BATCH = "application/vnd.ga5.invoice-claim-batch+json"
MEDIA_PROPOSALS = "application/vnd.ga5.invoice-action-proposals+json"
MEDIA_RESULTS = "application/vnd.ga5.invoice-action-results+json"
MEDIA_RECEIPTS = "application/vnd.ga5.invoice-action-receipts+json"

TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def data_part(media_type: str, data: Any) -> dict[str, Any]:
    return {"mediaType": media_type, "data": data}


def find_part(message: dict[str, Any], media_type: str) -> dict[str, Any] | None:
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        mt = part.get("mediaType") or part.get("mimeType") or part.get("media_type")
        if mt == media_type:
            return part
    return None


def part_data(part: dict[str, Any] | None) -> Any:
    if not part:
        return None
    if "data" in part:
        return part["data"]
    return None


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def apply_history_length(task: dict[str, Any], history_length: int | None) -> dict[str, Any]:
    view = clone(task)
    if history_length is None:
        return view
    history = list(view.get("history") or [])
    if history_length <= 0:
        view["history"] = []
    else:
        view["history"] = history[-history_length:]
    return view


def set_status(task: dict[str, Any], state: str, message: dict[str, Any] | None = None) -> None:
    status: dict[str, Any] = {"state": state, "timestamp": utc_now()}
    if message is not None:
        status["message"] = message
    task["status"] = status


def make_task(*, task_id: str, context_id: str, user_message: dict[str, Any]) -> dict[str, Any]:
    stored_message = clone(user_message)
    stored_message.setdefault("taskId", task_id)
    stored_message.setdefault("contextId", context_id)
    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_WORKING", "timestamp": utc_now()},
        "artifacts": [],
        "history": [stored_message],
    }
    return task


def is_terminal(task: dict[str, Any]) -> bool:
    return (task.get("status") or {}).get("state") in TERMINAL

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .canonical import message_hash, principal_id
from .decide import propose_batch
from .errors import (
    A2A_JSON,
    conflict,
    generic_forbidden,
    generic_not_found,
    invalid_argument,
    unauthorized,
    unsupported_media,
    utc_now,
    version_not_supported,
)
from .protocol import (
    MEDIA_BATCH,
    MEDIA_PROPOSALS,
    MEDIA_RECEIPTS,
    MEDIA_RESULTS,
    apply_history_length,
    clone,
    data_part,
    find_part,
    is_terminal,
    make_task,
    new_id,
    part_data,
    set_status,
)
from .store import Store

load_dotenv()

A2A_PREFIX = os.getenv("A2A_PREFIX", "/a2a").rstrip("/")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
store = Store(os.path.join(DATA_DIR, "agent.sqlite"))


def public_base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured if configured.endswith("/") else configured + "/"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    prefix = A2A_PREFIX or ""
    return f"{proto}://{host}{prefix}/"


def agent_card(request: Request) -> dict[str, Any]:
    base = public_base_url(request)
    return {
        "name": "GA5 Invoice Action Agent",
        "description": (
            "Reads synthetic invoice claim batches, proposes a single evidence-backed "
            "action per package, waits for grader results, and executes only accepted "
            "actions with stored receipts so retries cannot repeat payments."
        ),
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": [MEDIA_BATCH],
        "defaultOutputModes": [MEDIA_PROPOSALS, MEDIA_RECEIPTS],
        "supportedInterfaces": [
            {
                "url": base,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "securitySchemes": {
            "bearer": {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": "Exact Bearer token identifies the calling principal.",
                }
            }
        },
        "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
        "skills": [
            {
                "id": "invoice_action_agent",
                "name": "Invoice action agent",
                "description": (
                    "Propose settle_invoice, request_approval, hold_invoice, "
                    "reject_duplicate, or open_exception for each invoice package, "
                    "then execute only ACCEPTED results with bound receipt nonces."
                ),
                "tags": ["invoice", "a2a", "payments", "approval", "idempotency"],
                "inputModes": [MEDIA_BATCH],
                "outputModes": [MEDIA_PROPOSALS, MEDIA_RECEIPTS],
            }
        ],
    }


def parse_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None


def version_ok(request: Request) -> bool:
    value = (request.headers.get("a2a-version") or "").strip()
    return value in {"1.0", "1.0.0"}


def media_ok(request: Request) -> bool:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    return content_type == "application/a2a+json"


def a2a_json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=payload,
        status_code=status_code,
        media_type=A2A_JSON,
        headers={"Cache-Control": "no-store"},
    )


def history_length_from(payload: dict[str, Any] | None, query: str | None) -> int | None:
    if query not in (None, ""):
        try:
            return int(query)
        except ValueError:
            return None
    if not payload:
        return None
    configuration = payload.get("configuration") or {}
    value = configuration.get("historyLength")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def gate_protected(request: Request) -> JSONResponse | str:
    if not version_ok(request):
        return version_not_supported()
    token = parse_bearer(request)
    if token is None:
        return unauthorized()
    return token


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield


app = FastAPI(title="A2A Invoice Action Agent", redirect_slashes=False, lifespan=lifespan)


@app.middleware("http")
async def no_redirect_slash(request: Request, call_next):
    response = await call_next(request)
    return response


@app.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request) -> JSONResponse:
    return JSONResponse(content=agent_card(request), media_type="application/json")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _router_paths() -> list[str]:
    prefix = A2A_PREFIX
    return [prefix] if prefix else [""]


for _prefix in _router_paths():

    @app.post(f"{_prefix}/message:send")
    async def message_send(request: Request) -> Response:
        gated = await gate_protected(request)
        if isinstance(gated, JSONResponse):
            return gated
        if not media_ok(request):
            return unsupported_media()
        try:
            body = await request.json()
        except Exception:
            return invalid_argument("Malformed JSON")
        if not isinstance(body, dict):
            return invalid_argument("Malformed JSON")
        message = body.get("message")
        if not isinstance(message, dict):
            return invalid_argument("Missing message")
        message_id = str(message.get("messageId") or "")
        if not message_id:
            return invalid_argument("Missing messageId")
        principal = principal_id(gated)
        digest = message_hash(message)
        lock = await store.lock(f"msg:{principal}:{message_id}")
        async with lock:
            existing = store.get_message_record(principal, message_id)
            if existing:
                if existing["message_hash"] != digest:
                    return conflict("IDEMPOTENCY_CONFLICT", "Idempotency conflict")
                task = store.get_task(principal, existing["task_id"])
                if not task:
                    return generic_not_found()
                hl = history_length_from(body, None)
                return a2a_json({"task": apply_history_length(task, hl)})

            task_id = str(message.get("taskId") or "")
            if task_id:
                return await _continue_task(principal, gated, body, message, digest)
            return await _create_task(principal, body, message, digest)

    @app.get(f"{_prefix}/tasks")
    async def list_tasks(request: Request) -> Response:
        gated = await gate_protected(request)
        if isinstance(gated, JSONResponse):
            return gated
        principal = principal_id(gated)
        hl = history_length_from(None, request.query_params.get("historyLength"))
        tasks = [apply_history_length(task, hl) for task in store.list_tasks(principal)]
        return a2a_json({"tasks": tasks})

    @app.get(f"{_prefix}/tasks/{{task_id}}")
    async def get_task(task_id: str, request: Request) -> Response:
        gated = await gate_protected(request)
        if isinstance(gated, JSONResponse):
            return gated
        principal = principal_id(gated)
        task = store.get_task(principal, task_id)
        if not task:
            return generic_not_found()
        hl = history_length_from(None, request.query_params.get("historyLength"))
        return a2a_json(apply_history_length(task, hl))

    @app.post(f"{_prefix}/tasks/{{task_id}}:cancel")
    async def cancel_task(task_id: str, request: Request) -> Response:
        gated = await gate_protected(request)
        if isinstance(gated, JSONResponse):
            return gated
        if request.headers.get("content-length") not in (None, "0"):
            if not media_ok(request) and (request.headers.get("content-type") or "").strip():
                # Empty cancel bodies are allowed; typed JSON must be A2A.
                ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ct and ct not in {"application/a2a+json", "application/json"}:
                    return unsupported_media()
        principal = principal_id(gated)
        lock = await store.lock(f"task:{task_id}")
        async with lock:
            task = store.get_task(principal, task_id)
            if not task:
                return generic_not_found()
            if is_terminal(task):
                return conflict("TASK_NOT_CANCELABLE", "Task is not cancelable")
            set_status(task, "TASK_STATE_CANCELED")
            store.put_task(principal, task, utc_now())
            return a2a_json(task)


async def _create_task(
    principal: str, body: dict[str, Any], message: dict[str, Any], digest: str
) -> Response:
    part = find_part(message, MEDIA_BATCH)
    data = part_data(part)
    if not isinstance(data, dict):
        return invalid_argument("Missing invoice batch")
    packages = data.get("packages")
    if not isinstance(packages, list) or not packages:
        return invalid_argument("Missing packages")
    batch_id = str(data.get("batchId") or "")
    task_id = new_id("task")
    context_id = str(message.get("contextId") or new_id("ctx"))
    task = make_task(task_id=task_id, context_id=context_id, user_message=message)
    proposals = await propose_batch(
        store,
        task_id=task_id,
        batch_id=batch_id,
        policy_revision=data.get("policyRevision"),
        packages=[pkg if isinstance(pkg, dict) else {"packageId": str(i), "raw": pkg} for i, pkg in enumerate(packages)],
    )
    artifact = {
        "artifactId": new_id("art"),
        "name": "invoice-action-proposals",
        "parts": [data_part(MEDIA_PROPOSALS, {"batchId": batch_id, "proposals": proposals})],
    }
    task["artifacts"] = [artifact]
    set_status(task, "TASK_STATE_INPUT_REQUIRED")
    store.put_task(principal, task, utc_now())
    store.put_message_record(principal, str(message.get("messageId")), digest, task_id)
    hl = history_length_from(body, None)
    return a2a_json({"task": apply_history_length(task, hl)})


async def _continue_task(
    principal: str,
    _token: str,
    body: dict[str, Any],
    message: dict[str, Any],
    digest: str,
) -> Response:
    task_id = str(message.get("taskId"))
    lock = await store.lock(f"task:{task_id}")
    async with lock:
        task = store.get_task(principal, task_id)
        if not task:
            return generic_not_found()
        if is_terminal(task):
            return conflict("UNSUPPORTED_OPERATION", "Task is terminal")
        context_id = str(message.get("contextId") or "")
        if context_id and context_id != task.get("contextId"):
            return invalid_argument("Invalid request")
        part = find_part(message, MEDIA_RESULTS)
        if part is None:
            return invalid_argument("Missing results")
        data = part_data(part)
        if not isinstance(data, dict):
            return invalid_argument("Missing results")
        try:
            updated = _apply_results(task, message, data)
        except ValueError:
            return invalid_argument("Invalid request")
        store.put_task(principal, updated, utc_now())
        store.put_message_record(principal, str(message.get("messageId")), digest, task_id)
        hl = history_length_from(body, None)
        return a2a_json({"task": apply_history_length(updated, hl)})


def _proposal_map(task: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("mediaType") == MEDIA_PROPOSALS:
                data = part.get("data") or {}
                batch_id = str(data.get("batchId") or "")
                mapping = {
                    str(item.get("packageId")): item
                    for item in data.get("proposals") or []
                    if isinstance(item, dict)
                }
                return batch_id, mapping
    return "", {}


def _apply_results(
    task: dict[str, Any], message: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    stored_batch, proposals = _proposal_map(task)
    batch_id = str(data.get("batchId") or "")
    if stored_batch and batch_id != stored_batch:
        raise ValueError("batch")
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("results")
    if {str(item.get("packageId")) for item in results if isinstance(item, dict)} != set(proposals):
        raise ValueError("packages")

    executions: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("result")
        package_id = str(item.get("packageId") or "")
        proposal = proposals.get(package_id)
        if not proposal:
            raise ValueError("package")
        if str(item.get("actionId") or "") != str(proposal.get("actionId") or ""):
            raise ValueError("actionId")
        if str(item.get("action") or "") != str(proposal.get("action") or ""):
            raise ValueError("action")
        outcome = str(item.get("outcome") or "").upper()
        if outcome not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("outcome")
        if outcome == "ACCEPTED":
            nonce = str(item.get("receiptNonce") or "")
            if not nonce:
                raise ValueError("nonce")
            executions.append(
                {
                    "packageId": package_id,
                    "actionId": proposal["actionId"],
                    "action": proposal["action"],
                    "receiptNonce": nonce,
                    "facts": clone(proposal.get("facts") or {}),
                    "evidenceRefs": clone(proposal.get("evidenceRefs") or []),
                }
            )

    stored = clone(message)
    stored.setdefault("taskId", task["id"])
    stored.setdefault("contextId", task.get("contextId"))
    history = list(task.get("history") or [])
    history.append(stored)
    artifacts = list(task.get("artifacts") or [])
    artifacts.append(
        {
            "artifactId": new_id("art"),
            "name": "invoice-action-receipts",
            "parts": [data_part(MEDIA_RECEIPTS, {"batchId": batch_id, "executions": executions})],
        }
    )
    updated = clone(task)
    updated["history"] = history
    updated["artifacts"] = artifacts
    set_status(updated, "TASK_STATE_COMPLETED")
    return updated


def create_app() -> FastAPI:
    return app

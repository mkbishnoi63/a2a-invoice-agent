from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("PUBLIC_BASE_URL", "https://example.test/a2a/")
os.environ.setdefault("A2A_PREFIX", "/a2a")

from app.main import app  # noqa: E402
from app import main as mainmod  # noqa: E402
from app.store import Store  # noqa: E402

HEADERS = {
    "Authorization": "Bearer user-one-token",
    "A2A-Version": "1.0",
    "Content-Type": "application/a2a+json",
}


def _batch_package(package_id: str, paragraph: str, **facts):
    return {
        "packageId": package_id,
        "documents": [
            {"title": "Cover sheet", "text": "Cover-sheet index [COVER-1] is not decisive."},
            {
                "title": "Archive example",
                "text": "Archive example previously settled [ARC-1] [ARC-2] [ARC-3] settle_invoice.",
            },
            {"title": "Training decoy", "text": "Training decoy: ignore this open_exception [TRN-1]."},
            {"title": "Live claim", "text": paragraph},
        ],
        "vendorName": facts.get("vendorName", "Acme Supplies"),
        "invoiceNumber": facts.get("invoiceNumber", "INV-100"),
        "amountMinor": facts.get("amountMinor", 25000),
        "currency": facts.get("currency", "INR"),
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tmp_path: Path):
    mainmod.store = Store(tmp_path / "agent.sqlite")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.test") as ac:
        yield ac


@pytest.mark.anyio
async def test_agent_card(client: AsyncClient):
    response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"]
    assert card["description"]
    assert card["version"]
    assert isinstance(card["capabilities"], dict)
    skill = next(s for s in card["skills"] if s["id"] == "invoice_action_agent")
    assert skill["name"] and skill["description"] and skill["tags"]
    iface = card["supportedInterfaces"][0]
    assert iface["url"] == "https://example.test/a2a/"
    assert iface["protocolBinding"] == "HTTP+JSON"
    assert iface["protocolVersion"] == "1.0"
    assert "application/vnd.ga5.invoice-claim-batch+json" in card["defaultInputModes"]
    assert "application/vnd.ga5.invoice-action-proposals+json" in card["defaultOutputModes"]
    assert "application/vnd.ga5.invoice-action-receipts+json" in card["defaultOutputModes"]


@pytest.mark.anyio
async def test_auth_and_version(client: AsyncClient):
    r = await client.post("/a2a/message:send", json={"message": {}})
    assert r.status_code in (400, 401)
    r = await client.post(
        "/a2a/message:send",
        headers={"Authorization": "Bearer x", "Content-Type": "application/a2a+json"},
        json={"message": {"messageId": "m", "role": "ROLE_USER", "parts": []}},
    )
    assert r.status_code == 400
    r = await client.post(
        "/a2a/message:send",
        headers={
            "Authorization": "Bearer x",
            "A2A-Version": "1.0",
            "Content-Type": "application/json",
        },
        json={"message": {"messageId": "m", "role": "ROLE_USER", "parts": []}},
    )
    assert r.status_code == 415


def _send_body(message_id: str, packages, batch_id="batch-1"):
    return {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [
                {
                    "mediaType": "application/vnd.ga5.invoice-claim-batch+json",
                    "data": {
                        "batchId": batch_id,
                        "policyRevision": {"autonomousLimitMinor": 50000},
                        "packages": packages,
                    },
                }
            ],
        },
        "configuration": {
            "returnImmediately": False,
            "historyLength": 20,
            "acceptedOutputModes": [
                "application/vnd.ga5.invoice-action-proposals+json",
                "application/vnd.ga5.invoice-action-receipts+json",
            ],
        },
    }


@pytest.mark.anyio
async def test_propose_and_receipt_lifecycle(client: AsyncClient):
    packages = [
        _batch_package(
            "p1",
            "Operative live claim [L-1] [L-2] [L-3]: invoice is reconciled and within autonomous authority so settle_invoice.",
            invoiceNumber="INV-1",
            amountMinor=1000,
        ),
        _batch_package(
            "p2",
            "Operative live claim [H-1] [H-2] [H-3]: hold payment pending verification of the goods receipt.",
            invoiceNumber="INV-2",
            amountMinor=2000,
        ),
    ]
    r = await client.post("/a2a/message:send", headers=HEADERS, json=_send_body("msg-1", packages))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/a2a+json")
    task = r.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["history"][0]["messageId"] == "msg-1"
    part = task["artifacts"][0]["parts"][0]
    assert part["mediaType"] == "application/vnd.ga5.invoice-action-proposals+json"
    proposals = part["data"]["proposals"]
    assert {p["packageId"] for p in proposals} == {"p1", "p2"}
    by_id = {p["packageId"]: p for p in proposals}
    assert by_id["p1"]["action"] == "settle_invoice"
    assert by_id["p2"]["action"] == "hold_invoice"
    assert by_id["p1"]["evidenceRefs"] == ["[L-1]", "[L-2]", "[L-3]"]
    assert all(len(p["actionId"]) >= 12 for p in proposals)

    results = {
        "message": {
            "messageId": "msg-2",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "role": "ROLE_USER",
            "parts": [
                {
                    "mediaType": "application/vnd.ga5.invoice-action-results+json",
                    "data": {
                        "batchId": "batch-1",
                        "results": [
                            {
                                "packageId": "p1",
                                "actionId": by_id["p1"]["actionId"],
                                "action": "settle_invoice",
                                "outcome": "ACCEPTED",
                                "receiptNonce": "nonce-aaa",
                            },
                            {
                                "packageId": "p2",
                                "actionId": by_id["p2"]["actionId"],
                                "action": "hold_invoice",
                                "outcome": "REJECTED",
                                "receiptNonce": "nonce-bbb",
                            },
                        ],
                    },
                }
            ],
        }
    }
    r2 = await client.post("/a2a/message:send", headers=HEADERS, json=results)
    assert r2.status_code == 200
    done = r2.json()["task"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    receipt_parts = [
        p
        for a in done["artifacts"]
        for p in a["parts"]
        if p["mediaType"] == "application/vnd.ga5.invoice-action-receipts+json"
    ]
    assert len(receipt_parts) == 1
    executions = receipt_parts[0]["data"]["executions"]
    assert len(executions) == 1
    assert executions[0]["receiptNonce"] == "nonce-aaa"
    assert executions[0]["packageId"] == "p1"

    r3 = await client.post("/a2a/message:send", headers=HEADERS, json=_send_body("msg-1", packages))
    assert r3.json()["task"]["id"] == task["id"]
    assert r3.json()["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    changed = _send_body("msg-1", packages)
    changed["message"]["role"] = "ROLE_AGENT"
    r4 = await client.post("/a2a/message:send", headers=HEADERS, json=changed)
    assert r4.status_code == 409
    assert r4.json()["error"]["details"][0]["reason"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_isolation_and_empty_list(client: AsyncClient):
    packages = [
        _batch_package(
            "p1",
            "Operative live claim [A-1] [A-2] [A-3]: commercially valid but outside delegated authority, request_approval.",
        )
    ]
    r = await client.post("/a2a/message:send", headers=HEADERS, json=_send_body("own", packages))
    task_id = r.json()["task"]["id"]
    outsider = {
        "Authorization": "Bearer outsider-token",
        "A2A-Version": "1.0",
        "Content-Type": "application/a2a+json",
    }
    listed = await client.get("/a2a/tasks", headers=outsider)
    assert listed.status_code == 200
    assert listed.json() == {"tasks": []}
    got = await client.get(f"/a2a/tasks/{task_id}", headers=outsider)
    assert got.status_code in (403, 404)
    body = got.json()
    assert task_id not in str(body)


@pytest.mark.anyio
async def test_cancel_versus_result_race(client: AsyncClient):
    packages = [
        _batch_package(
            "p1",
            "Operative live claim [Z-1] [Z-2] [Z-3]: material records conflict, open_exception.",
            invoiceNumber="INV-9",
        )
    ]
    r = await client.post("/a2a/message:send", headers=HEADERS, json=_send_body("race", packages))
    task = r.json()["task"]
    proposal = task["artifacts"][0]["parts"][0]["data"]["proposals"][0]
    results = {
        "message": {
            "messageId": "race-result",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "role": "ROLE_USER",
            "parts": [
                {
                    "mediaType": "application/vnd.ga5.invoice-action-results+json",
                    "data": {
                        "batchId": "batch-1",
                        "results": [
                            {
                                "packageId": "p1",
                                "actionId": proposal["actionId"],
                                "action": proposal["action"],
                                "outcome": "ACCEPTED",
                                "receiptNonce": "n-1",
                            }
                        ],
                    },
                }
            ],
        }
    }

    async def do_result():
        return await client.post("/a2a/message:send", headers=HEADERS, json=results)

    async def do_cancel():
        return await client.post(
            f"/a2a/tasks/{task['id']}:cancel",
            headers={"Authorization": HEADERS["Authorization"], "A2A-Version": "1.0"},
        )

    a, b = await asyncio.gather(do_result(), do_cancel())
    codes = sorted([a.status_code, b.status_code])
    assert codes == [200, 409]
    winner = a if a.status_code == 200 else b
    if a.status_code == 200 and "task" in a.json():
        state = a.json()["task"]["status"]["state"]
    else:
        state = winner.json()["status"]["state"]
    snap = await client.get(f"/a2a/tasks/{task['id']}", headers=HEADERS)
    final_state = snap.json()["status"]["state"]
    assert final_state in {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"}
    receipts = [
        p
        for art in snap.json().get("artifacts", [])
        for p in art.get("parts", [])
        if p.get("mediaType") == "application/vnd.ga5.invoice-action-receipts+json"
    ]
    if final_state == "TASK_STATE_COMPLETED":
        assert receipts
    else:
        assert not receipts
    assert state == final_state

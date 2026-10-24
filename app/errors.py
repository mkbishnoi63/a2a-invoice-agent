from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

A2A_JSON = "application/a2a+json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def error_body(
    *,
    http_status: int,
    status_name: str,
    message: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "a2a-protocol.org",
        "metadata": {"timestamp": utc_now()},
    }
    if metadata:
        info["metadata"].update(metadata)
    return {
        "error": {
            "code": http_status,
            "status": status_name,
            "message": message,
            "details": [info],
        }
    }


def error_response(
    *,
    http_status: int,
    status_name: str,
    message: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=error_body(
            http_status=http_status,
            status_name=status_name,
            message=message,
            reason=reason,
            metadata=metadata,
        ),
        media_type=A2A_JSON,
        headers={"Cache-Control": "no-store"},
    )


def generic_not_found() -> JSONResponse:
    return error_response(
        http_status=404,
        status_name="NOT_FOUND",
        message="The requested resource was not found",
        reason="TASK_NOT_FOUND",
    )


def generic_forbidden() -> JSONResponse:
    return error_response(
        http_status=403,
        status_name="PERMISSION_DENIED",
        message="The requested resource was not found",
        reason="PERMISSION_DENIED",
    )


def unauthorized() -> JSONResponse:
    return error_response(
        http_status=401,
        status_name="UNAUTHENTICATED",
        message="Authentication required",
        reason="UNAUTHENTICATED",
    )


def version_not_supported() -> JSONResponse:
    return error_response(
        http_status=400,
        status_name="FAILED_PRECONDITION",
        message="Unsupported A2A version",
        reason="VERSION_NOT_SUPPORTED",
    )


def unsupported_media() -> JSONResponse:
    return error_response(
        http_status=415,
        status_name="INVALID_ARGUMENT",
        message="Unsupported media type",
        reason="CONTENT_TYPE_NOT_SUPPORTED",
    )


def invalid_argument(message: str = "Invalid request") -> JSONResponse:
    return error_response(
        http_status=400,
        status_name="INVALID_ARGUMENT",
        message=message,
        reason="INVALID_ARGUMENT",
    )


def conflict(reason: str, message: str) -> JSONResponse:
    return error_response(
        http_status=409,
        status_name="ABORTED",
        message=message,
        reason=reason,
    )

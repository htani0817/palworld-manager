# -*- coding: utf-8 -*-
"""認証情報を含むリクエストの検証エラーを安全に返す。"""

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response


NO_STORE_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Content-Type-Options": "nosniff",
}

SENSITIVE_REQUEST_PATHS = frozenset(
    {
        "/api/maintenance/jobs",
        "/api/system/update",
    }
)


async def sensitive_request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    """秘密値を含みうる入力では、Pydantic の入力値を応答へ反射しない。"""
    if request.url.path not in SENSITIVE_REQUEST_PATHS:
        return await request_validation_exception_handler(request, exc)

    return JSONResponse(
        status_code=422,
        content={"detail": "リクエストの形式が不正です"},
        headers=NO_STORE_HEADERS,
    )

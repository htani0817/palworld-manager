from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import palworld_client as pal
from palworld_client import PalInvalidResponseError
from ini_editor import mask_secrets

router = APIRouter(prefix="/api/server", tags=["server"])

# Palworld 本体が停止中・起動途中のときに出る通信例外。
# HTTPStatusError だけを捕捉していると、これらが ASGI 層まで抜けて
# HTTP 500 とフルトレースバックになり、ログが埋まってしまう。
_PAL_UNREACHABLE = (httpx.RequestError, OSError)


def _pal_error(e: Exception) -> HTTPException:
    """Palworld REST API の失敗を、原因に応じた HTTP エラーへ変換する。"""
    if isinstance(e, httpx.HTTPStatusError):
        return HTTPException(
            status_code=502,
            detail=f"Palworld API エラー: {e.response.status_code} {e.response.text}",
        )
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(
            status_code=504,
            detail="Palworld API がタイムアウトしました",
        )
    if isinstance(e, PalInvalidResponseError):
        return HTTPException(
            status_code=502,
            detail=f"Palworld API が解釈できないレスポンスを返しました: {e}",
        )
    # 接続不可（サーバ停止中など）。想定内の状態なので 502 で返す。
    return HTTPException(
        status_code=502,
        detail="Palworld API に接続できません（サーバが停止している可能性があります）",
    )


# ── GET エンドポイント ──────────────────────────────────────────────

@router.get("/info")
async def server_info():
    try:
        return await pal.get_info()
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


@router.get("/players")
async def players():
    try:
        return await pal.get_players()
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


@router.get("/settings")
async def server_settings():
    try:
        data = await pal.get_settings_data()
        # 秘密キー（AdminPassword / ServerPassword）をマスクして返す
        return mask_secrets(data) if isinstance(data, dict) else data
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


@router.get("/metrics")
async def metrics():
    try:
        return await pal.get_metrics()
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


# ── POST エンドポイント ──────────────────────────────────────────────

class AnnounceRequest(BaseModel):
    message: str


@router.post("/announce")
async def announce(req: AnnounceRequest):
    try:
        await pal.post_announce(req.message)
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


class KickRequest(BaseModel):
    user_id: str
    message: Optional[str] = ""


@router.post("/kick")
async def kick(req: KickRequest):
    try:
        await pal.post_kick(req.user_id, req.message or "")
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


class BanRequest(BaseModel):
    user_id: str
    message: Optional[str] = ""


@router.post("/ban")
async def ban(req: BanRequest):
    try:
        await pal.post_ban(req.user_id, req.message or "")
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


class UnbanRequest(BaseModel):
    user_id: str


@router.post("/unban")
async def unban(req: UnbanRequest):
    try:
        await pal.post_unban(req.user_id)
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


@router.post("/save")
async def save():
    try:
        await pal.post_save()
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


class ShutdownRequest(BaseModel):
    wait_time: int = 60
    message: Optional[str] = ""


@router.post("/shutdown")
async def shutdown(req: ShutdownRequest):
    try:
        await pal.post_shutdown(req.wait_time, req.message or "")
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e


@router.post("/stop")
async def stop():
    try:
        await pal.post_stop()
        return {"result": "ok"}
    except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:
        raise _pal_error(e) from e

import asyncio
import os
import signal
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, SecretStr

import discord_notify
import scheduler
from config import settings
from maintenance import (
    JobAlreadyRunningError,
    PlayerCheckError,
    PlayersConnectedError,
    coordinator as maintenance_coordinator,
)
from sensitive_requests import NO_STORE_HEADERS
from system_metrics import get_system_metrics

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/metrics")
async def system_metrics():
    return await get_system_metrics()


async def _run(*args: str, timeout: float = 30) -> tuple[int, str, str]:
    """systemctl などを非同期実行して (returncode, stdout, stderr) を返す"""
    process_options = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        process_options["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(*args, **process_options)

    async def terminate_and_reap() -> None:
        if proc.returncode is None:
            terminated = False
            if os.name == "posix" and isinstance(getattr(proc, "pid", None), int):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    terminated = True
                except ProcessLookupError:
                    terminated = True
                except OSError:
                    pass
            if not terminated:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await terminate_and_reap()
        return 1, "", "タイムアウトしました"
    except asyncio.CancelledError:
        await terminate_and_reap()
        raise
    return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


@router.get("/service-status")
async def service_status():
    # is-active は読み取りのみで root 権限不要のため sudo を付けない
    rc, out, err = await _run("systemctl", "is-active", settings.pal_service_name, timeout=15)
    # systemctl is-active は inactive/failed でも終了コード非0を返す（これは正常な状態通知）。
    # ただし出力が空 or 想定外の場合は「取得失敗」として running=None で区別する。
    known = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    if out in known:
        return {"service": settings.pal_service_name, "status": out, "running": out == "active", "query_ok": True}
    # コマンド自体が失敗（DBus 障害・タイムアウト等）→ 状態不明
    return {
        "service": settings.pal_service_name,
        "status": out or "unknown",
        "running": None,
        "query_ok": False,
        "error": err or f"exit {rc}",
    }


@router.post("/start")
async def service_start():
    return await _run_service_control("start")


@router.post("/stop")
async def service_stop():
    return await _run_service_control("stop")


class ServiceControlError(RuntimeError):
    """systemctl start/stop の失敗理由（HTTP に依存しない）。"""

    def __init__(self, message: str, *, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


async def run_service_control(action: str) -> None:
    """start/stop の核ロジック。ロック確認 + systemctl 実行。

    HTTPException に依存しないため、スケジューラのバックグラウンドジョブからも呼べる。
    失敗時は ServiceControlError を送出する。
    """
    if (
        scheduler.maintenance_operation_lock.locked()
        or scheduler.manual_maintenance_pending()
    ):
        raise ServiceControlError("メンテナンス操作が実行中です", status_code=409)
    async with scheduler.maintenance_operation_lock:
        if scheduler.manual_maintenance_pending():
            raise ServiceControlError("メンテナンス操作が実行中です", status_code=409)
        if await scheduler.is_update_in_progress():
            raise ServiceControlError(
                "palworld-update.service が実行中か、状態を確認できません",
                status_code=409,
            )
        rc, _, err = await _run(
            "sudo", "-n", "systemctl", action, settings.pal_service_name
        )
    if rc != 0:
        raise ServiceControlError(f"{action} 失敗: {err}", status_code=500)


async def _run_service_control(action: str):
    """既存 HTTP ルート用ラッパー（挙動は変えない）。"""
    try:
        await run_service_control(action)
    except ServiceControlError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return {"result": "ok"}


@router.post("/restart", status_code=202)
async def service_restart():
    return await _start_safe_maintenance("restart")


class UpdateRequest(BaseModel):
    sudo_password: SecretStr

    class Config:
        extra = "forbid"


@router.post("/update", status_code=202)
async def service_update(req: UpdateRequest, response: Response):
    response.headers.update(NO_STORE_HEADERS)
    sudo_password = req.sudo_password.get_secret_value()
    try:
        return await _start_safe_maintenance(
            "update", sudo_password=sudo_password
        )
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE_HEADERS}
        raise
    finally:
        sudo_password = None


async def _start_safe_maintenance(
    action: str, *, sudo_password: Optional[str] = None
):
    """旧APIも接続者確認・保存・共通排他を迂回させない。"""
    try:
        return await maintenance_coordinator.start_job(
            action=action, sudo_password=sudo_password
        )
    except (PlayersConnectedError, JobAlreadyRunningError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlayerCheckError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/notify-status")
async def notify_status():
    """Discord 通知の設定状態を返す（URL 自体は返さない）"""
    return {"discord_enabled": discord_notify.is_enabled()}


@router.post("/notify-test")
async def notify_test():
    """Discord にテスト通知を送信する。応答には Webhook URL/トークンを一切含めない"""
    try:
        await discord_notify.send_test()
        return {"result": "ok"}
    except RuntimeError as e:
        # 未設定エラー（URL を含まない固定的なメッセージ）
        raise HTTPException(status_code=400, detail=str(e))
    except discord_notify.DiscordSendError as e:
        # トークンを含まない安全なメッセージ
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        # 未知の例外は詳細を伏せる（URL 漏洩防止）
        raise HTTPException(status_code=500, detail="テスト通知の送信に失敗しました")

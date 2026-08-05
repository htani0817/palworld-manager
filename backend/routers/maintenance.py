# -*- coding: utf-8 -*-
"""設定差分と安全なメンテナンスジョブの API。"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, SecretStr, StrictBool, StrictStr, conint

import config_diff
import scheduler
from maintenance import (
    ACTIVE_STATUSES,
    JobAlreadyRunningError,
    JobNotCancellableError,
    PlayerCheckError,
    PlayersConnectedError,
    coordinator,
)
from sensitive_requests import NO_STORE_HEADERS


router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
logger = logging.getLogger("palworld_manager.maintenance_api")

CountdownSeconds = conint(strict=True, ge=0, le=300)


class MaintenanceJobRequest(BaseModel):
    action: Literal["restart", "update"]
    allow_players: StrictBool = False
    countdown_seconds: CountdownSeconds = 0
    message: Optional[StrictStr] = None
    sudo_password: Optional[SecretStr] = None

    class Config:
        extra = "forbid"


@router.get("/config-diff")
async def get_config_diff():
    try:
        return await config_diff.get_config_diff()
    except config_diff.IniNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except config_diff.IniReadError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except config_diff.RuntimeSettingsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("設定差分の作成中に予期しないエラーが発生しました")
        raise HTTPException(
            status_code=500, detail="設定差分の作成に失敗しました"
        ) from exc


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: MaintenanceJobRequest, response: Response):
    response.headers.update(NO_STORE_HEADERS)
    sudo_password = (
        request.sudo_password.get_secret_value()
        if request.sudo_password is not None
        else None
    )
    try:
        return await coordinator.start_job(
            action=request.action,
            allow_players=request.allow_players,
            countdown_seconds=request.countdown_seconds,
            message=request.message,
            sudo_password=sudo_password,
        )
    except (PlayersConnectedError, JobAlreadyRunningError) as exc:
        raise HTTPException(
            status_code=409, detail=str(exc), headers=NO_STORE_HEADERS
        ) from exc
    except PlayerCheckError as exc:
        raise HTTPException(
            status_code=502, detail=str(exc), headers=NO_STORE_HEADERS
        ) from exc
    except ValueError as exc:
        # Pydantic を迂回した内部呼び出しにも同じ制約を適用する。
        raise HTTPException(
            status_code=422, detail=str(exc), headers=NO_STORE_HEADERS
        ) from exc
    finally:
        sudo_password = None


@router.get("/jobs/current")
async def get_current_job(response: Response):
    response.headers.update(NO_STORE_HEADERS)
    current = await coordinator.get_current()
    if current.get("status") not in ACTIVE_STATUSES and await scheduler.is_update_in_progress():
        return {
            **current,
            "status": "updating",
            "action": "update",
            "detail": "systemd の palworld-update.service が Manager 外で継続中です",
        }
    return current


@router.delete("/jobs/current")
async def cancel_current_job(response: Response):
    response.headers.update(NO_STORE_HEADERS)
    try:
        return await coordinator.cancel_current()
    except JobNotCancellableError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc), headers=NO_STORE_HEADERS
        ) from exc

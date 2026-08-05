from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import shutdown_scheduler

router = APIRouter(prefix="/api/shutdown-schedule", tags=["shutdown-schedule"])


@router.get("/")
async def get_shutdown_schedule():
    return shutdown_scheduler.get_status()


class SetShutdownRequest(BaseModel):
    shutdown_at: str  # datetime-local 形式（例: 2026-08-10T21:00）
    label: Optional[str] = None

    class Config:
        extra = "forbid"


@router.post("/")
async def set_shutdown_schedule(req: SetShutdownRequest):
    try:
        run_at = datetime.fromisoformat(req.shutdown_at)
        return shutdown_scheduler.set_schedule(run_at, req.label or "サーバー終了")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/")
async def delete_shutdown_schedule():
    shutdown_scheduler.clear_schedule()
    return {"state": "none"}

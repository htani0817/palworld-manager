import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scheduler import (
    add_schedule,
    add_schedule_once,
    get_schedule_list_with_next,
    remove_schedule,
)

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


@router.get("/")
async def list_schedules():
    return get_schedule_list_with_next()


class AddScheduleRequest(BaseModel):
    label: str
    type: Literal["cron", "once"] = "cron"
    cron: Optional[str] = None
    run_at: Optional[str] = None  # ISO 8601 形式（例: 2026-07-20T04:00）


@router.post("/")
async def create_schedule(req: AddScheduleRequest):
    job_id = str(uuid.uuid4())[:8]
    try:
        if req.type == "once":
            if not req.run_at:
                raise ValueError("run_at（実行日時）を指定してください")
            run_at = datetime.fromisoformat(req.run_at)
            return add_schedule_once(job_id, req.label, run_at)
        if not req.cron:
            raise ValueError("cron 式を指定してください")
        return add_schedule(job_id, req.label, req.cron)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"スケジュール登録に失敗しました: {e}")


@router.delete("/{job_id}")
async def delete_schedule(job_id: str):
    # 実行中（予告カウントダウン中）なら削除＝キャンセル。
    # ただし不可逆フェーズ（保存後の systemctl）に入っていたら再起動は止められない。
    state = remove_schedule(job_id)
    if state == "uncancellable":
        return {"result": "removed", "warning": "再起動処理が最終段階に入っているため、この再起動は停止できません"}
    if state == "cancelling":
        return {"result": "ok", "cancelled_running": True}
    return {"result": "ok", "cancelled_running": False}

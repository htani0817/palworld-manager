# -*- coding: utf-8 -*-
"""単一サーバーのファイルエディターとバックアップAPI。"""

import asyncio
import json
import logging
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

import backup_store
import palworld_client
import scheduler
import workspace_files as files
from config import settings
from routers.system import service_status

logger = logging.getLogger("palworld_manager.backups")
_current = None
_task = None


async def guard(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store, private"
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin and urlsplit(origin).netloc != request.headers.get("host")
    ):
        raise HTTPException(403, "管理画面と同じ接続先から操作してください")


router = APIRouter(prefix="/api", tags=["workspace"], dependencies=[Depends(guard)])


async def durable_io(fn, *args):
    """要求のキャンセル後も、ファイル処理が終わるまで排他を維持する。"""
    worker = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


async def run_file_operation(fn, *args):
    try:
        return await durable_io(fn, *args)
    except FileNotFoundError:
        raise HTTPException(404, "指定したファイルが見つかりません") from None
    except files.WorkspaceError as exc:
        raise HTTPException(409, str(exc)) from None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise HTTPException(400, "ファイルを処理できません。形式とアクセス権限を確認してください") from None


@router.get("/files")
async def list_files(path: str = Query("config", max_length=1024)):
    return await run_file_operation(files.listing, path)


@router.get("/files/content")
async def file_content(path: str = Query(..., max_length=1024)):
    return await run_file_operation(files.read_text, path)


class FileWrite(BaseModel):
    path: str = Field(max_length=1024)
    content: str = Field(max_length=files.TEXT_LIMIT)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.put("/files/content")
async def write_file(request: FileWrite):
    if scheduler.maintenance_operation_lock.locked() or scheduler.manual_maintenance_pending():
        raise HTTPException(409, "メンテナンスまたはバックアップ操作が実行中です")
    async with scheduler.maintenance_operation_lock:
        if await scheduler.is_update_in_progress():
            raise HTTPException(409, "サーバー更新中か、更新状態を確認できません")
        return await run_file_operation(files.write_text, request.path, request.content, request.revision)


class BackupRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    paths: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(default_factory=lambda: ["config", "saves"], min_length=1, max_length=100)


def current_job():
    return dict(_current) if _current else None


async def execute_job(token, action, payload, job):
    try:
        async with scheduler.maintenance_operation_lock:
            if await scheduler.is_update_in_progress():
                raise files.WorkspaceError("サーバー更新中か、更新状態を確認できません")
            status = await service_status()
            if not status.get("query_ok") or status["status"] not in {"active", "inactive", "failed"}:
                raise files.WorkspaceError("サーバーの稼働状態を確定できません")
            job["state"] = "running"
            if action == "restore":
                if status["status"] not in {"inactive", "failed"}:
                    raise files.WorkspaceError("復元する前にサーバーを停止してください")
                job["message"] = "検証・復元前退避・復元を実行しています"
                result = await durable_io(backup_store.restore, payload["id"])
            else:
                if status["status"] == "active":
                    job["message"] = "ワールドを保存しています"
                    await palworld_client.post_save()
                job["message"] = "ファイルをバックアップしています"
                result = await durable_io(
                    backup_store.create, payload["label"], payload["description"], payload["paths"],
                    "live" if status["status"] == "active" else "stopped",
                )
                result = {"backup_id": result["id"], "file_count": len(result["files"])}
            job.update(state="completed", message="復元が完了しました" if action == "restore" else "バックアップを作成しました", result=result)
    except files.WorkspaceError as exc:
        job.update(state="failed", message=str(exc))
    except asyncio.CancelledError:
        job.update(state="failed", message="Managerの終了に伴い処理を終了しました。バックアップ一覧とファイルの状態を確認してください")
        raise
    except Exception:
        # ファイルパスやREST APIの認証情報を応答へ含めない。
        job.update(state="failed", message="処理に失敗しました。サーバー接続・保存先の権限・空き容量を確認してください")
        logger.warning("バックアップ処理が失敗しました（%s）", action)
    finally:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        scheduler.release_manual_maintenance(token)
    return dict(job)


def start_job(action, payload):
    global _current, _task
    token = object()
    if not scheduler.reserve_manual_maintenance(token):
        raise HTTPException(409, "メンテナンスまたはバックアップ操作が実行中です")
    _current = {"id": uuid.uuid4().hex, "action": action, "state": "queued", "message": "準備中",
                "started_at": datetime.now(timezone.utc).isoformat()}
    _task = asyncio.create_task(execute_job(token, action, payload, _current))
    return current_job()


async def shutdown():
    # ファイル処理スレッドが動いている間は共通ロックを解放しない。
    if _task and not _task.done():
        await asyncio.shield(_task)


@router.get("/backups/jobs/current")
async def backup_job():
    return {"job": current_job()}


def schedule_file():
    return backup_store.storage() / "schedule.json"


def read_schedule():
    path = schedule_file()
    if not path.exists():
        return {"enabled": False, "cron": "0 3 * * *", "paths": ["config", "saves"], "timezone": settings.schedule_timezone}
    data = json.loads(path.read_text(encoding="utf-8"))
    validated = BackupSchedule.model_validate(data).model_dump()
    if isinstance(data.get("last_run"), dict):
        validated["last_run"] = data["last_run"]
    return validated


class BackupSchedule(BaseModel):
    enabled: bool
    cron: str = Field(default="0 3 * * *", max_length=80)
    paths: list[Literal["config", "saves"]] = Field(default_factory=lambda: ["config", "saves"], min_length=1, max_length=2)


async def scheduled_backup():
    try:
        saved = read_schedule()
        if not saved.get("enabled"):
            return
        start_job("create", {"label": "定期バックアップ", "description": saved["cron"], "paths": saved["paths"]})
        result = await asyncio.shield(_task)
        saved["last_run"] = {"state": result["state"], "message": result["message"], "at": result["finished_at"]}
    except HTTPException:
        saved = read_schedule()
        saved["last_run"] = {"state": "skipped", "message": "他のメンテナンス操作が実行中のためスキップ", "at": datetime.now(timezone.utc).isoformat()}
    # 実行中に変更された予約を古い内容で上書きしない。
    latest = read_schedule()
    latest["last_run"] = saved["last_run"]
    files.atomic_write(schedule_file(), json.dumps(latest, ensure_ascii=False).encode("utf-8"))


def register_schedule(data):
    if scheduler.scheduler.get_job("workspace-backup"):
        scheduler.scheduler.remove_job("workspace-backup")
    if data.get("enabled"):
        scheduler.scheduler.add_job(scheduled_backup, CronTrigger.from_crontab(data["cron"], timezone=settings.schedule_timezone),
                                    id="workspace-backup", max_instances=1, coalesce=True, misfire_grace_time=300)


def restore_schedule():
    try:
        register_schedule(read_schedule())
    except (OSError, ValueError):
        logger.warning("バックアップ予約を復元できませんでした")


@router.get("/backups/schedule")
async def get_schedule():
    data = await run_file_operation(read_schedule)
    job = scheduler.scheduler.get_job("workspace-backup")
    next_run = getattr(job, "next_run_time", None)
    return {**data, "timezone": settings.schedule_timezone,
            "next_run": next_run.isoformat() if next_run else None}


@router.put("/backups/schedule")
async def put_schedule(request: BackupSchedule):
    try:
        CronTrigger.from_crontab(request.cron, timezone=settings.schedule_timezone)
    except ValueError:
        raise HTTPException(422, "cron式が不正です（分 時 日 月 曜日の5項目）") from None
    old = read_schedule()
    data = request.model_dump()
    files.atomic_write(schedule_file(), json.dumps(data, ensure_ascii=False).encode("utf-8"))
    try:
        register_schedule(data)
    except Exception:
        files.atomic_write(schedule_file(), json.dumps(old, ensure_ascii=False).encode("utf-8"))
        register_schedule(old)
        raise HTTPException(500, "予約を登録できませんでした") from None
    return await get_schedule()


@router.get("/backups")
async def list_backups():
    return {"backups": await run_file_operation(backup_store.summaries)}


@router.post("/backups", status_code=202)
async def create_backup(request: BackupRequest):
    return {"job": start_job("create", request.model_dump())}


@router.get("/backups/{backup_id}")
async def get_backup(backup_id: str):
    return await run_file_operation(backup_store.detail, backup_id)


class RestoreRequest(BaseModel):
    confirm: Literal["RESTORE"]


@router.post("/backups/{backup_id}/restore", status_code=202)
async def restore_backup(backup_id: str, request: RestoreRequest):
    return {"job": start_job("restore", {"id": backup_id})}


@router.delete("/backups/{backup_id}")
async def delete_backup(backup_id: str):
    if scheduler.maintenance_operation_lock.locked() or scheduler.manual_maintenance_pending():
        raise HTTPException(409, "メンテナンスまたはバックアップ操作が実行中です")
    async with scheduler.maintenance_operation_lock:
        return await run_file_operation(backup_store.move_to_trash, backup_id)

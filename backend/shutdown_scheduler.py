"""
サーバー終了日時の予約。

再起動スケジューラ（scheduler.py）とは異なり、指定日時に systemctl stop を
1回だけ実行し、その後は再起動しない「提供終了」専用の単発オペレーション。
1件のみを扱う（配列ではなく単一オブジェクトを JSON に永続化する）。

状態遷移:
  pending → running → completed / failed
- completed / failed は自動では消さない（ダッシュボードのバナーを
  管理者が「解除」するまで表示し続けるため）。「解除」で JSON ごと削除する。
"""
import json
import os
import tempfile
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Optional

from apscheduler.triggers.date import DateTrigger

from config import settings
from routers.system import ServiceControlError, run_service_control
from scheduler import _tz, scheduler

SHUTDOWN_SCHEDULE_FILE = Path(__file__).parent / "shutdown_schedule.json"
SHUTDOWN_JOB_ID = "server-shutdown"  # 1件のみなので固定 ID


# ── 永続化（アトミック書き込み）────────────────────────────────────

def _load() -> Optional[dict]:
    if not SHUTDOWN_SCHEDULE_FILE.exists():
        return None
    try:
        data = json.loads(SHUTDOWN_SCHEDULE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _save(entry: Optional[dict]) -> None:
    """entry が None ならファイルを削除する（解除）。それ以外はアトミック書き込み。"""
    if entry is None:
        try:
            SHUTDOWN_SCHEDULE_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    data = json.dumps(entry, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(SHUTDOWN_SCHEDULE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SHUTDOWN_SCHEDULE_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ── ジョブ本体（APScheduler から呼ばれる）──────────────────────────────

async def _shutdown_job() -> None:
    entry = _load()
    if entry is None:
        return  # 発火直前に解除された
    entry["status"] = "running"
    entry["updated_at"] = datetime.now().isoformat()
    _save(entry)
    try:
        await run_service_control("stop")
    except ServiceControlError as e:
        entry["status"] = "failed"
        entry["last_error"] = str(e)
        entry["updated_at"] = datetime.now().isoformat()
        _save(entry)
        return
    entry["status"] = "completed"
    entry["last_error"] = None
    entry["updated_at"] = datetime.now().isoformat()
    _save(entry)  # 削除しない。手動解除まで残す（ダッシュボードバナー継続表示のため）


def _register_job(shutdown_at: datetime) -> None:
    scheduler.add_job(
        _shutdown_job,
        trigger=DateTrigger(run_date=shutdown_at, timezone=settings.schedule_timezone),
        id=SHUTDOWN_JOB_ID,
        replace_existing=True,
        misfire_grace_time=300,
    )


# ── 公開 API ────────────────────────────────────────────────────────

def set_schedule(shutdown_at: datetime, label: str) -> dict:
    """終了予定を設定・上書きする。過去日時は ValueError。"""
    tz = _tz()
    if shutdown_at.tzinfo is None and tz is not None:
        shutdown_at = shutdown_at.replace(tzinfo=tz)
    now = datetime.now(shutdown_at.tzinfo) if shutdown_at.tzinfo else datetime.now()
    if shutdown_at <= now:
        raise ValueError("過去の日時は指定できません")

    now_iso = datetime.now().isoformat()
    entry = {
        "shutdown_at": shutdown_at.isoformat(),
        "label": label,
        "status": "pending",
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_error": None,
    }
    # JSON 永続化を正本として先に行い、その後 APScheduler へ登録する。
    # 登録に失敗したら JSON も元の状態へ戻す。
    previous = _load()
    _save(entry)
    try:
        _register_job(shutdown_at)
    except Exception:
        _save(previous)
        raise
    return get_status()


def clear_schedule() -> None:
    """終了予定を解除する（設定有無に関わらず冪等）。"""
    try:
        scheduler.remove_job(SHUTDOWN_JOB_ID)
    except Exception:
        pass
    _save(None)


def get_status() -> dict:
    entry = _load()
    if entry is None:
        return {"state": "none"}
    shutdown_at = datetime.fromisoformat(entry["shutdown_at"])
    now = datetime.now(shutdown_at.tzinfo) if shutdown_at.tzinfo else datetime.now()
    if shutdown_at <= now:
        return {**entry, "state": "reached"}
    days_remaining = ceil((shutdown_at - now).total_seconds() / 86400)
    return {**entry, "state": "countdown", "days_remaining": days_remaining}


def restore_shutdown_schedule() -> None:
    """起動時に保存済みの終了予定を復元する。

    - running のまま残っていた（Manager 再起動で中断）→ failed にする
    - pending かつ過去日時（Manager 停止中に発火予定を過ぎた）→ 自動実行せず
      failed にして手動対応を促す（安全側）
    - pending かつ未来日時 → APScheduler に再登録する
    - completed / failed はそのまま保持する（ダッシュボードバナー判定に使うため）
    """
    entry = _load()
    if entry is None:
        return
    if entry.get("status") == "running":
        entry["status"] = "failed"
        entry["last_error"] = "Manager 再起動により中断されました"
        entry["updated_at"] = datetime.now().isoformat()
        _save(entry)
        return
    if entry.get("status") != "pending":
        return
    try:
        shutdown_at = datetime.fromisoformat(entry["shutdown_at"])
    except (KeyError, ValueError):
        return
    now = datetime.now(shutdown_at.tzinfo) if shutdown_at.tzinfo else datetime.now()
    if shutdown_at <= now:
        entry["status"] = "failed"
        entry["last_error"] = "Manager 停止中に実行時刻を過ぎました。手動で停止してください"
        entry["updated_at"] = datetime.now().isoformat()
        _save(entry)
        return
    _register_job(shutdown_at)

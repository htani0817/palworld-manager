"""
定期再起動スケジューラ + 再起動コーディネーター。

予約再起動は cron ジョブ・単発ジョブの両方を restart_coordinator() に集約する。
安全メンテナンスを含む手動操作とも共通の asyncio.Lock と
「直近成功時刻（単調時計）」を共有し、二重実行を防ぐ。

スケジュールは JSON に永続化し、状態遷移を持つ:
  pending → running → completed / failed / cancelled

- cron: 繰り返し。実行しても JSON からは消えず status を更新する
- once: 指定日時に1回。completed で JSON から削除、failed は残して UI に表示

安全設計:
- ワールド保存に失敗したら再起動しない（failed で残す）
- 予告カウントダウンは asyncio.Event で割り込み可能。保存直前にも再確認する
- 不可逆フェーズ（保存後の systemctl）に入ったらキャンセル不可
- JSON 永続化を正本とし、失敗時は APScheduler 登録をロールバックする
"""
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 以下のフォールバック
    ZoneInfo = None  # type: ignore

import palworld_client as pal
from config import settings

SCHEDULES_FILE = Path(__file__).parent / "schedules.json"

# 二重再起動を防ぐガード時間。この時間内に再起動が成功していれば後続はスキップする。
RESTART_DEBOUNCE_SECONDS = 600


def _tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(settings.schedule_timezone)
        except Exception:
            return None
    return None


scheduler = AsyncIOScheduler(timezone=settings.schedule_timezone)


# ── スケジュール永続化（アトミック書き込み）────────────────────────────

def _load_schedules() -> list[dict]:
    if not SCHEDULES_FILE.exists():
        return []
    try:
        return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_schedules(schedules: list[dict]) -> None:
    """一時ファイルに書いてから os.replace で置換（書き込み中断による破損を防ぐ）。失敗は例外を投げる"""
    data = json.dumps(schedules, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(SCHEDULES_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SCHEDULES_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _update_entry(job_id: str, **fields) -> None:
    schedules = _load_schedules()
    for s in schedules:
        if s["id"] == job_id:
            s.update(fields)
            break
    _save_schedules(schedules)


def _remove_entry(job_id: str) -> None:
    _save_schedules([s for s in _load_schedules() if s["id"] != job_id])


# ── 再起動コーディネーター ────────────────────────────────────────────

# 予約再起動・手動再起動・安全な更新が、保存やサービス操作を同時に
# 実行しないためのプロセス内共通ロック。
maintenance_operation_lock = asyncio.Lock()
# 直近で再起動が「成功」した単調時計の時刻（デバウンス用。壁時計のズレに影響されない）
_last_restart_monotonic: Optional[float] = None
# 接続者確認から復旧確認まで、手動メンテナンスへ優先権を与える予約トークン。
_manual_maintenance_token: Optional[object] = None
# 実行中ジョブのキャンセル用イベント（job_id -> Event）
_cancel_events: dict[str, asyncio.Event] = {}
# 不可逆フェーズ（保存後の systemctl）に入ったジョブ（この間はキャンセル不可）
_uncancellable: set[str] = set()
# Manager 終了時に予告は中止し、保存・再起動中は完了を待つための追跡。
_active_restart_tasks: set[asyncio.Task] = set()


def mark_restart_completed() -> None:
    """共通ロック内で完了した再起動・更新をデバウンスへ反映する。"""
    global _last_restart_monotonic
    _last_restart_monotonic = time.monotonic()


def restart_was_recent() -> bool:
    """直近の成功からデバウンス時間内なら True。"""
    return (
        _last_restart_monotonic is not None
        and time.monotonic() - _last_restart_monotonic < RESTART_DEBOUNCE_SECONDS
    )


def reserve_manual_maintenance(token: object) -> bool:
    """手動メンテナンスを予約する。

    既に共通操作が始まっている場合や別の手動ジョブが予約済みなら拒否する。
    await を含まないため、単一イベントループ内では確認と予約が分割されない。
    """
    global _manual_maintenance_token
    if maintenance_operation_lock.locked() or _manual_maintenance_token is not None:
        return False
    _manual_maintenance_token = token
    return True


def release_manual_maintenance(token: object) -> None:
    """自分が確保した手動メンテナンス予約だけを解放する。"""
    global _manual_maintenance_token
    if _manual_maintenance_token is token:
        _manual_maintenance_token = None


def manual_maintenance_pending() -> bool:
    return _manual_maintenance_token is not None


async def is_update_in_progress() -> bool:
    """Manager再起動をまたいだ transient update unit も検出する。

    ユニットが存在しないことを確認できた場合だけ非稼働とみなし、DBus 障害や
    想定外の応答では安全側に True を返す。開発環境に systemctl 自体がない場合は
    Palworld を操作できない環境なので、テスト・静的確認用として False を返す。
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            "palworld-update.service",
            "--property=LoadState",
            "--property=ActiveState",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return True

        properties: dict[str, str] = {}
        for line in stdout.decode(errors="replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip()

        load_state = properties.get("LoadState", "")
        active_state = properties.get("ActiveState", "")
        if active_state in {"active", "activating", "deactivating", "reloading"}:
            return True
        if load_state == "not-found":
            return False
        if proc.returncode != 0:
            return True
        if active_state in {"inactive", "failed"} and load_state in {"loaded", "masked"}:
            return False
        # 空応答や未知の状態では別の不可逆操作を重ねない。
        return True
    except FileNotFoundError:
        return False
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
        raise
    except Exception:
        # 状態を確認できないときに別の不可逆操作を重ねない。
        return True


def request_cancel(job_id: str) -> str:
    """実行中ジョブにキャンセルを要求する。

    戻り値:
      "cancelling" … キャンセル要求を受理（まだ取り消せる段階）
      "uncancellable" … 既に systemctl フェーズに入っており取り消せない
      "not_running" … 実行中でない（予約自体の削除は別途 remove_schedule で行う）
    """
    if job_id in _uncancellable:
        return "uncancellable"
    ev = _cancel_events.get(job_id)
    if ev is not None:
        ev.set()
        _update_entry(job_id, status="cancelling")
        return "cancelling"
    return "not_running"


async def _announce(msg: str) -> None:
    try:
        await pal.post_announce(msg)
    except Exception:
        pass


async def _interruptible_sleep(seconds: float, cancel: asyncio.Event) -> bool:
    """キャンセル可能な待機。キャンセルされたら True を返す"""
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
        return True  # cancel がセットされた
    except asyncio.TimeoutError:
        return False  # 時間経過（キャンセルなし）


async def _run_systemctl_restart() -> tuple[bool, str]:
    """sudo -n systemctl restart を非同期実行し、成否と稼働確認結果を返す。

    タイムアウト・キャンセル時も子プロセスを kill して回収する。
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "systemctl", "restart", settings.pal_service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return False, "systemctl restart がタイムアウト/中断されました"
        if proc.returncode != 0:
            return False, (stderr.decode(errors="replace").strip() or f"exit {proc.returncode}")
    except Exception as e:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
        return False, str(e)

    # 再起動後の稼働確認（is-active、sudo 不要）
    try:
        check = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", settings.pal_service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(check.communicate(), timeout=15)
        active = out.decode(errors="replace").strip()
        if active != "active":
            return False, f"再起動後の状態が active ではありません（{active}）"
    except Exception:
        pass  # 稼働確認自体の失敗は restart 成功を覆さない
    return True, ""


async def restart_service_after_save() -> tuple[bool, str]:
    """共通ロックの保持側から呼ぶ、保存済みワールド用の再起動処理。"""
    return await _run_systemctl_restart()


async def restart_coordinator(label: str, job_id: Optional[str] = None, announce: bool = True) -> dict:
    """全再起動の単一入口。二重実行をロックと成功時デバウンスで防ぐ。

    - 予告シーケンス（約5分）はキャンセル可能
    - ワールド保存に失敗したら再起動しない（safe by default）
    - 保存後の systemctl フェーズはキャンセル不可
    """
    global _last_restart_monotonic
    current_task = asyncio.current_task()
    if current_task is not None:
        _active_restart_tasks.add(current_task)
    cancel = asyncio.Event()
    if job_id:
        # 共通ロック待ちの間にも予約削除を受け付けられるよう先に登録する。
        _cancel_events[job_id] = cancel
    try:
        async with maintenance_operation_lock:
            if cancel.is_set():
                return {"result": "cancelled"}
            if manual_maintenance_pending():
                return {
                    "result": "skipped",
                    "reason": "手動メンテナンスが予約されているためスキップしました",
                }

            # 直近成功からの経過（単調時計）でデバウンス
            if restart_was_recent():
                return {"result": "skipped", "reason": "直近に再起動済みのためスキップしました"}
            if await is_update_in_progress():
                return {
                    "result": "failed",
                    "error": "palworld-update.service が実行中か、状態を確認できません",
                }

            if announce:
                steps = [
                    (f"[自動再起動] {label}: 5分後にサーバーを再起動します", 240),
                    (f"[自動再起動] {label}: 1分後にサーバーを再起動します", 30),
                    (f"[自動再起動] {label}: 30秒後にサーバーを再起動します", 25),
                    (f"[自動再起動] {label}: まもなく再起動します。ご注意ください", 5),
                ]
                for msg, wait in steps:
                    if cancel.is_set():
                        await _announce(f"[自動再起動] {label}: 再起動を中止しました")
                        return {"result": "cancelled"}
                    await _announce(msg)
                    if await _interruptible_sleep(wait, cancel):
                        await _announce(f"[自動再起動] {label}: 再起動を中止しました")
                        return {"result": "cancelled"}

            # 保存直前の最終キャンセル確認
            if cancel.is_set():
                await _announce(f"[自動再起動] {label}: 再起動を中止しました")
                return {"result": "cancelled"}

            # ワールド保存（失敗したら再起動しない）
            try:
                await pal.post_save()
            except Exception as e:
                return {"result": "failed", "error": f"ワールド保存に失敗したため再起動を中止しました: {e}"}

            # 不可逆フェーズ: ここからはキャンセル不可
            if job_id:
                _uncancellable.add(job_id)
            ok, err = await _run_systemctl_restart()
            if ok:
                mark_restart_completed()
                return {"result": "ok"}
            return {"result": "failed", "error": err}
    finally:
        if current_task is not None:
            _active_restart_tasks.discard(current_task)
        if job_id:
            if _cancel_events.get(job_id) is cancel:
                _cancel_events.pop(job_id, None)
            _uncancellable.discard(job_id)


async def shutdown_active_restarts() -> None:
    """APScheduler の新規発火を止め、実行中の再起動を安全に収束する。"""
    try:
        scheduler.pause()
    except Exception:
        pass
    # pause 直前に executor へ渡った coroutine が追跡集合へ入る機会を与える。
    await asyncio.sleep(0)

    # 予告や共通ロック待ちは取り消す。保存開始後は coordinator 側が
    # cancel event を再確認しないため、不可逆操作が完了するまで待機する。
    for cancel_event in list(_cancel_events.values()):
        cancel_event.set()

    caller = asyncio.current_task()
    tasks = [
        task
        for task in list(_active_restart_tasks)
        if task is not caller and not task.done()
    ]
    if tasks:
        await asyncio.gather(
            *(asyncio.shield(task) for task in tasks),
            return_exceptions=True,
        )


# ── ジョブ本体（APScheduler から呼ばれる）──────────────────────────────

async def _job_cron(job_id: str, label: str) -> None:
    _update_entry(job_id, status="running", last_run=datetime.now().isoformat())
    res = await restart_coordinator(label, job_id=job_id, announce=True)
    r = res.get("result")
    last = {"ok": "completed", "cancelled": "cancelled", "skipped": "skipped"}.get(r, "failed")
    # cron は繰り返すので pending に戻し、直近結果を残す
    _update_entry(job_id, status="pending", last_result=last, last_error=res.get("error"))


async def _job_once(job_id: str, label: str) -> None:
    _update_entry(job_id, status="running", last_run=datetime.now().isoformat())
    res = await restart_coordinator(label, job_id=job_id, announce=True)
    result = res.get("result")
    if result == "ok":
        _remove_entry(job_id)  # 成功した単発のみ削除
    elif result == "cancelled":
        _update_entry(job_id, status="cancelled")
    else:
        # failed / skipped は削除せず残して UI に表示
        _update_entry(job_id, status="failed", last_error=res.get("error") or result)


# ── 公開 API ────────────────────────────────────────────────────────

def _persist_then_register(entry: dict, register) -> dict:
    """JSON 永続化を正本として先に行い、その後 APScheduler へ登録する。

    登録に失敗したら JSON から取り消す（ghost レコードを残さない）。
    """
    job_id = entry["id"]
    schedules = [s for s in _load_schedules() if s["id"] != job_id]
    schedules.append(entry)
    _save_schedules(schedules)  # 失敗すれば例外 → 呼び出し側で 400/500
    try:
        register()
    except Exception:
        # 登録失敗: JSON をロールバック
        _remove_entry(job_id)
        raise
    return _with_next(scheduler.get_job(job_id), entry)


def add_schedule(job_id: str, label: str, cron_expr: str) -> dict:
    """cron_expr: "分 時 日 月 曜" 形式（標準 cron 5フィールド）

    曜日は APScheduler 仕様（mon-sun）。数字は 0=月曜のため誤解を招くので拒否する。
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError("cron 式は「分 時 日 月 曜」の5フィールドで指定してください")
    minute, hour, day, month, day_of_week = parts

    # 曜日フィールドに数字が含まれると crontab(0=日) と APScheduler(0=月) で食い違うため拒否
    if re.search(r"\d", day_of_week):
        raise ValueError("曜日は数字ではなく mon〜sun で指定してください（* は可）")

    trigger = CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        timezone=settings.schedule_timezone,
    )
    entry = {"id": job_id, "label": label, "type": "cron", "cron": cron_expr, "status": "pending"}

    def _register():
        scheduler.add_job(_job_cron, trigger=trigger, id=job_id, args=[job_id, label],
                          replace_existing=True, misfire_grace_time=300)

    return _persist_then_register(entry, _register)


def add_schedule_once(job_id: str, label: str, run_at: datetime) -> dict:
    """指定日時に1回だけ再起動（予告開始時刻。実再起動は約5分後）"""
    tz = _tz()
    if run_at.tzinfo is None and tz is not None:
        run_at = run_at.replace(tzinfo=tz)
    now = datetime.now(run_at.tzinfo) if run_at.tzinfo else datetime.now()
    if run_at <= now:
        raise ValueError("過去の日時は指定できません")

    entry = {"id": job_id, "label": label, "type": "once", "run_at": run_at.isoformat(), "status": "pending"}

    def _register():
        scheduler.add_job(_job_once, trigger=DateTrigger(run_date=run_at, timezone=settings.schedule_timezone),
                          id=job_id, args=[job_id, label], replace_existing=True, misfire_grace_time=300)

    return _persist_then_register(entry, _register)


def remove_schedule(job_id: str) -> str:
    """予約を削除する。実行中ならキャンセルも要求する。

    戻り値は request_cancel と同じ（uncancellable の場合は systemctl 実行中なので
    JSON からは消すが再起動自体は止められない）。
    """
    cancel_state = request_cancel(job_id)
    # JSON を正本として先に消す
    _remove_entry(job_id)
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    return cancel_state


def _with_next(job, entry: dict) -> dict:
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {**entry, "next_run": next_run, "active": job is not None}


def get_schedule_list_with_next() -> list[dict]:
    result = []
    for s in _load_schedules():
        job = scheduler.get_job(s["id"])
        result.append(_with_next(job, s))
    return result


def restore_schedules() -> None:
    """起動時に保存済みスケジュールを復元する。

    - running のまま残っていたもの（Manager 再起動で中断）は failed に倒す（勝手に消さない）
    - 過去日時の once は failed として残す（実行されなかった事実を UI に見せる）
    """
    schedules = _load_schedules()
    changed = False
    for s in schedules:
        try:
            if s.get("status") in ("running", "cancelling"):
                s["status"] = "failed"
                s["last_error"] = "Manager 再起動により中断されました"
                changed = True

            if s.get("type") == "once":
                run_at = datetime.fromisoformat(s["run_at"])
                now = datetime.now(run_at.tzinfo) if run_at.tzinfo else datetime.now()
                if run_at <= now:
                    if s.get("status") not in ("failed", "completed", "cancelled"):
                        s["status"] = "failed"
                        s["last_error"] = "Manager 停止中に実行時刻を過ぎました"
                        changed = True
                    continue
                # 再登録（JSON は既にあるので scheduler だけ登録）
                scheduler.add_job(
                    _job_once, trigger=DateTrigger(run_date=run_at, timezone=settings.schedule_timezone),
                    id=s["id"], args=[s["id"], s["label"]], replace_existing=True, misfire_grace_time=300,
                )
            else:
                parts = s["cron"].strip().split()
                if len(parts) == 5:
                    minute, hour, day, month, dow = parts
                    scheduler.add_job(
                        _job_cron,
                        trigger=CronTrigger(minute=minute, hour=hour, day=day, month=month,
                                            day_of_week=dow, timezone=settings.schedule_timezone),
                        id=s["id"], args=[s["id"], s["label"]], replace_existing=True, misfire_grace_time=300,
                    )
        except Exception:
            pass
    if changed:
        try:
            _save_schedules(schedules)
        except Exception:
            pass

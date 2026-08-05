"""
サーバー終了日時（shutdown_scheduler）のテスト。

依存（fastapi, apscheduler）が必要。実行:
    cd backend
    python -m pytest tests/test_shutdown_schedule.py -v
または pytest 無しでも動くよう、末尾に簡易ランナーを用意している:
    python tests/test_shutdown_schedule.py
"""
import asyncio
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# backend をインポートパスに追加
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("JWT_SECRET", "x" * 64)


def _fresh_module(tmp_json: Path):
    """shutdown_scheduler と依存する scheduler / routers.system をテスト用にリロードする。

    scheduler.py のジョブ実行対象（is_update_in_progress）は systemctl 依存のため、
    実行環境を汚さないようスタブ化する。routers.system.run_service_control は
    呼び出しを記録するテストダブルに差し替える。
    """
    import scheduler
    importlib.reload(scheduler)
    scheduler.SCHEDULES_FILE = Path(tempfile.mktemp(suffix=".json"))

    async def no_update():
        return False
    scheduler.is_update_in_progress = no_update

    import routers.system as system_router
    importlib.reload(system_router)

    import shutdown_scheduler
    importlib.reload(shutdown_scheduler)
    shutdown_scheduler.SHUTDOWN_SCHEDULE_FILE = tmp_json
    return shutdown_scheduler, system_router


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_set_schedule_persists_and_registers_job():
    """設定するとJSONが生成され、APSchedulerにジョブが登録されること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        run_at = datetime.now() + timedelta(days=1)
        result = ss.set_schedule(run_at, "テスト終了")
        assert result["state"] == "countdown", result
        assert tmp.exists()
        job = ss.scheduler.get_job(ss.SHUTDOWN_JOB_ID)
        assert job is not None, "APSchedulerにジョブが登録されていない"
    finally:
        if tmp.exists():
            tmp.unlink()


def test_set_schedule_overwrites_existing():
    """再設定すると上書きされ、replace_existingでジョブも置き換わること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        run_at1 = datetime.now() + timedelta(days=1)
        run_at2 = datetime.now() + timedelta(days=2)
        ss.set_schedule(run_at1, "1回目")
        result = ss.set_schedule(run_at2, "2回目")
        assert result["label"] == "2回目"
        entry = ss._load()
        assert entry["label"] == "2回目"
    finally:
        if tmp.exists():
            tmp.unlink()


def test_set_schedule_rejects_past_datetime():
    """過去日時を指定するとValueErrorになること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        past = datetime.now() - timedelta(days=1)
        try:
            ss.set_schedule(past, "過去")
            assert False, "過去日時なのに例外が出なかった"
        except ValueError:
            pass
        assert not tmp.exists(), "過去日時指定でファイルが作られてしまった"
    finally:
        if tmp.exists():
            tmp.unlink()


def test_clear_schedule_removes_file_and_job():
    """解除するとJSONが削除され、ジョブも消えること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        run_at = datetime.now() + timedelta(days=1)
        ss.set_schedule(run_at, "解除テスト")
        assert tmp.exists()
        ss.clear_schedule()
        assert not tmp.exists()
        assert ss.scheduler.get_job(ss.SHUTDOWN_JOB_ID) is None
    finally:
        if tmp.exists():
            tmp.unlink()


def test_clear_schedule_is_idempotent_when_nothing_set():
    """未設定の状態で解除しても例外にならないこと"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    ss.clear_schedule()  # 例外が出なければOK
    assert ss.get_status() == {"state": "none"}


def test_shutdown_job_success_marks_completed_and_keeps_file():
    """ジョブ成功時、run_service_controlが呼ばれ、status=completedでファイルは残ること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, sys_router = _fresh_module(tmp)
    try:
        calls = {"stop": 0}

        async def fake_stop(action):
            calls["stop"] += 1
            assert action == "stop"

        ss.run_service_control = fake_stop

        run_at = datetime.now() + timedelta(seconds=1)
        ss.set_schedule(run_at, "成功テスト")

        _run(ss._shutdown_job())

        assert calls["stop"] == 1
        entry = ss._load()
        assert entry is not None, "成功後もファイルは削除されない想定"
        assert entry["status"] == "completed"
        assert entry["last_error"] is None
    finally:
        if tmp.exists():
            tmp.unlink()


def test_shutdown_job_failure_marks_failed_with_reason():
    """停止処理が失敗したらstatus=failedとlast_errorが記録されること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, sys_router = _fresh_module(tmp)
    try:
        async def fake_stop(action):
            raise sys_router.ServiceControlError("stop 失敗: exit 1", status_code=500)

        ss.run_service_control = fake_stop

        run_at = datetime.now() + timedelta(seconds=1)
        ss.set_schedule(run_at, "失敗テスト")

        _run(ss._shutdown_job())

        entry = ss._load()
        assert entry["status"] == "failed"
        assert "stop 失敗" in entry["last_error"]
    finally:
        if tmp.exists():
            tmp.unlink()


def test_shutdown_job_noop_when_cleared_before_fire():
    """発火直前に解除されていたら何もしないこと"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, sys_router = _fresh_module(tmp)
    calls = {"stop": 0}

    async def fake_stop(action):
        calls["stop"] += 1

    ss.run_service_control = fake_stop

    _run(ss._shutdown_job())  # ファイルが存在しない状態で呼ぶ

    assert calls["stop"] == 0
    assert not tmp.exists()


def test_restore_shutdown_schedule_reregisters_future_pending():
    """未来日時のpendingは再登録されること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        run_at = datetime.now() + timedelta(days=1)
        ss.set_schedule(run_at, "復元テスト")
        ss.scheduler.remove_job(ss.SHUTDOWN_JOB_ID)  # Manager再起動を模して一度消す
        assert ss.scheduler.get_job(ss.SHUTDOWN_JOB_ID) is None

        ss.restore_shutdown_schedule()

        assert ss.scheduler.get_job(ss.SHUTDOWN_JOB_ID) is not None
        entry = ss._load()
        assert entry["status"] == "pending"
    finally:
        if tmp.exists():
            tmp.unlink()


def test_restore_shutdown_schedule_fails_past_pending_without_running_stop():
    """過去日時のまま残っていたpendingは、自動実行せずfailedにすること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, sys_router = _fresh_module(tmp)
    calls = {"stop": 0}

    async def fake_stop(action):
        calls["stop"] += 1

    ss.run_service_control = fake_stop

    past = (datetime.now() - timedelta(hours=1)).isoformat()
    ss._save({
        "shutdown_at": past, "label": "過去残留", "status": "pending",
        "created_at": past, "updated_at": past, "last_error": None,
    })

    ss.restore_shutdown_schedule()

    assert calls["stop"] == 0, "過去日時のpendingを自動実行してしまった"
    entry = ss._load()
    assert entry["status"] == "failed"
    assert "過ぎました" in entry["last_error"]
    if tmp.exists():
        tmp.unlink()


def test_restore_shutdown_schedule_marks_running_as_failed():
    """running のまま残っていた（Manager再起動で中断）ならfailedにすること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    now_iso = datetime.now().isoformat()
    ss._save({
        "shutdown_at": (datetime.now() + timedelta(days=1)).isoformat(),
        "label": "中断テスト", "status": "running",
        "created_at": now_iso, "updated_at": now_iso, "last_error": None,
    })

    ss.restore_shutdown_schedule()

    entry = ss._load()
    assert entry["status"] == "failed"
    assert "中断" in entry["last_error"]
    if tmp.exists():
        tmp.unlink()


def test_restore_shutdown_schedule_keeps_completed_and_failed():
    """completed / failed はそのまま保持し、再登録しないこと（バナー判定用）"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    for status in ("completed", "failed"):
        now_iso = datetime.now().isoformat()
        ss._save({
            "shutdown_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "label": status, "status": status,
            "created_at": now_iso, "updated_at": now_iso, "last_error": None,
        })
        ss.restore_shutdown_schedule()
        entry = ss._load()
        assert entry["status"] == status, f"{status} が書き換わってしまった"
        assert ss.scheduler.get_job(ss.SHUTDOWN_JOB_ID) is None
    if tmp.exists():
        tmp.unlink()


def test_get_status_state_transitions():
    """none / countdown / reached の判定とdays_remainingの計算"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ss, _sys = _fresh_module(tmp)
    try:
        assert ss.get_status() == {"state": "none"}

        future = datetime.now() + timedelta(days=2, hours=1)
        ss.set_schedule(future, "カウントダウン")
        status = ss.get_status()
        assert status["state"] == "countdown"
        assert status["days_remaining"] >= 2

        past_iso = (datetime.now() - timedelta(minutes=1)).isoformat()
        now_iso = datetime.now().isoformat()
        ss._save({
            "shutdown_at": past_iso, "label": "経過済み", "status": "pending",
            "created_at": now_iso, "updated_at": now_iso, "last_error": None,
        })
        status = ss.get_status()
        assert status["state"] == "reached"
    finally:
        if tmp.exists():
            tmp.unlink()


# ── 簡易ランナー（pytest 無しでも実行可能）────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except Exception as e:
            print(f" FAIL {t.__name__}: {e}")
            failed.append(t.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())

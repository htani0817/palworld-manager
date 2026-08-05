"""
Codex レビューで見つかった回帰を固定するテスト。

依存（fastapi, apscheduler, psutil, httpx）が必要。実行:
    cd backend
    python -m pytest tests/test_regressions.py -v
または pytest 無しでも動くよう、末尾に簡易ランナーを用意している:
    python tests/test_regressions.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# backend をインポートパスに追加
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("JWT_SECRET", "x" * 64)


# ── INI: 二重引用しない / CRLF・BOM を維持 ─────────────────────────

def _make_ini(bom: bool, crlf: bool) -> Path:
    nl = "\r\n" if crlf else "\n"
    body = nl.join([
        "; comment",
        "[/Script/Pal.PalGameWorldSettings]",
        # DenyTechnologyList= と BanListURL= は空値。空値キーが保存で消えないことを検証するため含める。
        'OptionSettings=(Difficulty=None,ExpRate=1.000000,ServerName="My Server",'
        'AdminPassword="secret123",ServerPassword="pw",PublicPort=8211,'
        'DenyTechnologyList=,BanListURL="")',
        "",
    ])
    data = body.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    tf = Path(tempfile.mktemp(suffix=".ini"))
    tf.write_bytes(data)
    return tf


def _reload_with_ini(path: Path):
    import importlib

    import config
    os.environ["PAL_SETTINGS_INI"] = str(path)
    importlib.reload(config)
    import ini_editor
    importlib.reload(ini_editor)
    return ini_editor


def test_ini_no_double_quoting():
    """1項目 PATCH で未変更の引用値が二重引用されないこと"""
    for bom in (True, False):
        for crlf in (True, False):
            tf = _make_ini(bom, crlf)
            ie = _reload_with_ini(tf)
            ie.write_ini({"ServerName": "Changed"})
            after = tf.read_bytes()
            text = after.decode("utf-8-sig" if bom else "utf-8")
            # 未変更の秘密値が二重引用されていない
            assert 'AdminPassword="secret123"' in text, f"BOM={bom} CRLF={crlf}: {text}"
            assert 'AdminPassword=""secret123""' not in text
            assert 'ServerPassword="pw"' in text
            # 変更値が1回だけ引用
            assert 'ServerName="Changed"' in text
            assert 'ServerName=""Changed""' not in text
            # BOM 維持
            assert after.startswith(b"\xef\xbb\xbf") == bom
            # CRLF 維持（OptionSettings 行の後ろの改行が保たれる）
            assert (b"\r\n" in after) == crlf, f"BOM={bom} CRLF={crlf} newline lost"
            for b in tf.parent.glob(tf.stem + "*.bak_*"):
                b.unlink()
            tf.unlink()


def test_ini_read_roundtrip_values():
    """read → write（無変更キー）で値が意味的に不変であること"""
    tf = _make_ini(bom=False, crlf=True)
    ie = _reload_with_ini(tf)
    before = ie.read_ini()
    ie.write_ini({"ExpRate": "2.0"})
    after = ie.read_ini()
    # 変更キー以外はすべて一致
    for k in before:
        if k == "ExpRate":
            continue
        assert before[k] == after[k], f"{k}: {before[k]} != {after[k]}"
    assert after["ExpRate"] == "2.0"
    for b in tf.parent.glob(tf.stem + "*.bak_*"):
        b.unlink()
    tf.unlink()


def test_ini_empty_value_preserved():
    """空値キー（DenyTechnologyList= 等）が別項目の保存で消えないこと"""
    for crlf in (True, False):
        tf = _make_ini(bom=False, crlf=crlf)
        ie = _reload_with_ini(tf)
        before = ie.read_ini()
        # 空値キーが読めていること
        assert "DenyTechnologyList" in before, f"空値キーが読み取れない: {list(before)}"
        assert before["DenyTechnologyList"] == ""
        assert before["BanListURL"] == ""
        # 別の項目を保存
        ie.write_ini({"ExpRate": "3.0"})
        after = ie.read_ini()
        # 空値キーが消えていないこと
        assert "DenyTechnologyList" in after, f"空値キーが保存で消えた: {list(after)}"
        assert after["DenyTechnologyList"] == ""
        assert after["BanListURL"] == ""
        # 全キー数が維持されていること
        assert set(before) == set(after), f"キー集合が変化: {set(before) ^ set(after)}"
        for b in tf.parent.glob(tf.stem + "*.bak_*"):
            b.unlink()
        tf.unlink()


def test_ini_placeholder_rejected():
    """マスクのプレースホルダ保存を拒否すること"""
    tf = _make_ini(bom=False, crlf=False)
    ie = _reload_with_ini(tf)
    try:
        ie.write_ini({"AdminPassword": ie.SECRET_PLACEHOLDER})
        assert False, "プレースホルダ保存が通ってしまった"
    except ValueError:
        pass
    for b in tf.parent.glob(tf.stem + "*.bak_*"):
        b.unlink()
    tf.unlink()


# ── スケジューラ: 保存失敗で再起動しない / キャンセル漏れなし ──────────

def _fresh_scheduler(tmp_json: Path, *, stub_update_check: bool = True):
    import importlib
    import scheduler
    importlib.reload(scheduler)
    scheduler.SCHEDULES_FILE = tmp_json
    scheduler._last_restart_monotonic = None
    if stub_update_check:
        async def no_update():
            return False
        scheduler.is_update_in_progress = no_update
    return scheduler


def test_restart_aborts_on_save_failure():
    """ワールド保存が失敗したら systemctl restart を呼ばず failed を返す"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    calls = {"restart": 0}

    async def fail_save():
        raise RuntimeError("save failed")

    async def fake_restart():
        calls["restart"] += 1
        return True, ""

    sch.pal.post_save = fail_save
    sch._run_systemctl_restart = fake_restart

    async def run():
        return await sch.restart_coordinator("test", announce=False)

    res = asyncio.new_event_loop().run_until_complete(run())
    assert res["result"] == "failed", res
    assert calls["restart"] == 0, "保存失敗なのに再起動した"
    if tmp.exists():
        tmp.unlink()


def test_cancel_during_final_wait():
    """予告シーケンスの待機中にキャンセルすると再起動しない"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    calls = {"restart": 0, "save": 0}

    async def fake_save():
        calls["save"] += 1

    async def fake_restart():
        calls["restart"] += 1
        return True, ""

    sch.pal.post_save = fake_save
    sch._run_systemctl_restart = fake_restart

    # sleep を即時化しつつ、待機に入った瞬間にキャンセルを注入する
    orig_sleep = sch._interruptible_sleep

    async def run():
        # JSON に running エントリを用意
        sch._save_schedules([{"id": "j1", "label": "L", "type": "once", "run_at": "2099-01-01T00:00:00", "status": "running"}])

        async def driver():
            # coordinator が cancel イベントを登録するまで待ってからセット
            for _ in range(100):
                if "j1" in sch._cancel_events:
                    sch._cancel_events["j1"].set()
                    return
                await asyncio.sleep(0.001)

        task = asyncio.ensure_future(sch.restart_coordinator("L", job_id="j1", announce=True))
        await driver()
        return await task

    res = asyncio.new_event_loop().run_until_complete(run())
    assert res["result"] == "cancelled", res
    assert calls["restart"] == 0, "キャンセルしたのに再起動した"
    if tmp.exists():
        tmp.unlink()


def test_cancel_while_waiting_for_shared_lock_never_runs_later():
    """共通ロック待ちの予約を削除したら、ロック解放後にも実行しないこと"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    calls = {"save": 0, "restart": 0}

    async def fake_save():
        calls["save"] += 1

    async def fake_restart():
        calls["restart"] += 1
        return True, ""

    sch.pal.post_save = fake_save
    sch._run_systemctl_restart = fake_restart

    async def run():
        await sch.maintenance_operation_lock.acquire()
        task = asyncio.create_task(
            sch.restart_coordinator("待機中", job_id="waiting", announce=False)
        )
        for _ in range(100):
            if "waiting" in sch._cancel_events:
                break
            await asyncio.sleep(0)
        assert "waiting" in sch._cancel_events
        assert sch.request_cancel("waiting") == "cancelling"
        sch.maintenance_operation_lock.release()
        return await task

    result = asyncio.new_event_loop().run_until_complete(run())
    assert result["result"] == "cancelled"
    assert calls == {"save": 0, "restart": 0}
    if tmp.exists():
        tmp.unlink()


def test_update_unit_detection_is_fail_closed_but_allows_missing_unit():
    """systemd の既知状態だけを許可し、確認不能時は更新中として扱うこと"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp, stub_update_check=False)

    class FakeProcess:
        def __init__(self, output: str, returncode: int):
            self._output = output.encode()
            self.returncode = returncode

        async def communicate(self):
            return self._output, b""

        def kill(self):
            self.returncode = -9

    async def check(output: str, returncode: int):
        original = sch.asyncio.create_subprocess_exec

        async def fake_exec(*_args, **_kwargs):
            return FakeProcess(output, returncode)

        sch.asyncio.create_subprocess_exec = fake_exec
        try:
            return await sch.is_update_in_progress()
        finally:
            sch.asyncio.create_subprocess_exec = original

    async def run():
        assert await check("LoadState=not-found\nActiveState=inactive\n", 1) is False
        assert await check("LoadState=loaded\nActiveState=inactive\n", 0) is False
        assert await check("LoadState=loaded\nActiveState=active\n", 0) is True
        assert await check("", 1) is True
        assert await check("LoadState=loaded\nActiveState=mystery\n", 0) is True

    asyncio.new_event_loop().run_until_complete(run())
    if tmp.exists():
        tmp.unlink()


def test_manual_maintenance_reservation_has_priority_over_scheduled_restart():
    """手動予告中に予約再起動が二重停止を起こさないこと"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    calls = {"save": 0, "restart": 0}
    token = object()

    async def fake_save():
        calls["save"] += 1

    async def fake_restart():
        calls["restart"] += 1
        return True, ""

    sch.pal.post_save = fake_save
    sch._run_systemctl_restart = fake_restart
    assert sch.reserve_manual_maintenance(token) is True
    try:
        async def run():
            return await sch.restart_coordinator("予約", announce=False)

        result = asyncio.new_event_loop().run_until_complete(run())
    finally:
        sch.release_manual_maintenance(token)

    assert result["result"] == "skipped"
    assert "手動メンテナンス" in result["reason"]
    assert calls == {"save": 0, "restart": 0}
    if tmp.exists():
        tmp.unlink()


def test_scheduler_shutdown_cancels_countdown_before_closing_clients():
    """Manager 終了時に予約再起動の予告を収束できること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    countdown_started = asyncio.Event()
    calls = {"save": 0}

    async def fake_wait(_seconds, cancel_event):
        countdown_started.set()
        await cancel_event.wait()
        return True

    async def fake_save():
        calls["save"] += 1

    async def fake_announce(_message):
        return None

    sch._interruptible_sleep = fake_wait
    sch._announce = fake_announce
    sch.pal.post_save = fake_save

    async def run():
        task = asyncio.create_task(
            sch.restart_coordinator("終了テスト", job_id="shutdown", announce=True)
        )
        await asyncio.wait_for(countdown_started.wait(), timeout=1)
        await sch.shutdown_active_restarts()
        return await task

    result = asyncio.new_event_loop().run_until_complete(run())
    assert result["result"] == "cancelled"
    assert calls["save"] == 0
    if tmp.exists():
        tmp.unlink()


def test_debounce_uses_success_only():
    """再起動失敗時は _last_restart_monotonic を更新せず、次の試行をスキップしない"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    calls = {"restart": 0}

    async def fake_save():
        pass

    async def fail_restart():
        calls["restart"] += 1
        return False, "sudo denied"

    sch.pal.post_save = fake_save
    sch._run_systemctl_restart = fail_restart

    async def run():
        r1 = await sch.restart_coordinator("t", announce=False)
        r2 = await sch.restart_coordinator("t", announce=False)
        return r1, r2

    r1, r2 = asyncio.new_event_loop().run_until_complete(run())
    assert r1["result"] == "failed"
    # 失敗はデバウンスを更新しないので2回目もスキップされず実行される
    assert r2["result"] == "failed", r2
    assert calls["restart"] == 2, "失敗直後がデバウンスでスキップされた"
    if tmp.exists():
        tmp.unlink()


def test_external_maintenance_completion_updates_restart_debounce():
    """安全メンテナンスの完了も予約再起動のデバウンスへ反映すること"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    sch.mark_restart_completed()
    assert sch._last_restart_monotonic is not None

    async def run():
        return await sch.restart_coordinator("直後の予約", announce=False)

    result = asyncio.new_event_loop().run_until_complete(run())
    assert result["result"] == "skipped"
    if tmp.exists():
        tmp.unlink()


def test_legacy_restart_and_update_routes_delegate_to_safe_maintenance():
    """旧 system API が安全メンテナンスを迂回しないこと"""
    source = (BACKEND / "routers" / "system.py").read_text(encoding="utf-8")
    assert 'return await _start_safe_maintenance("restart")' in source
    assert "class UpdateRequest(BaseModel):" in source
    assert '"update", sudo_password=sudo_password' in source
    assert "sudo_password=req.sudo_password" not in source
    assert "sudo_password=sudo_password" in source
    assert '"--no-block"' not in source
    assert "async with scheduler.maintenance_operation_lock" in source
    assert "await scheduler.is_update_in_progress()" in source
    assert "scheduler.manual_maintenance_pending()" in source


def test_server_router_handles_connection_errors():
    """Palworld 停止中の接続失敗が HTTP 500 とトレースバックにならないこと

    2026-07-27 の検証環境ログで、サーバ停止直後に /api/server/metrics と
    /api/server/info が 49 回 500 を返し、ログの大半がトレースバックで
    埋まった。HTTPStatusError しか捕捉していなかったのが原因。
    """
    source = (BACKEND / "routers" / "server.py").read_text(encoding="utf-8")

    # 接続不可・タイムアウトを捕捉対象に含めること
    assert "_PAL_UNREACHABLE = (httpx.RequestError, OSError)" in source
    assert "httpx.TimeoutException" in source

    # 全ハンドラが同じ捕捉をしていること（捕捉漏れのハンドラを残さない）
    handler_count = source.count(
        "except (httpx.HTTPStatusError, PalInvalidResponseError, *_PAL_UNREACHABLE) as e:"
    )
    assert handler_count == source.count("raise _pal_error(e) from e")
    # HTTPStatusError だけを捕捉する古い書き方が残っていないこと
    assert "except httpx.HTTPStatusError as e:" not in source
    # PalInvalidResponseError が捕捉対象に含まれていること
    assert "PalInvalidResponseError" in source

    # ルータのエンドポイント数と捕捉数が一致すること
    assert handler_count == source.count("@router."), handler_count


def test_pal_error_maps_failures_to_expected_status():
    """_pal_error が例外の種類に応じた HTTP ステータスを返すこと"""
    import httpx

    from palworld_client import PalInvalidResponseError
    from routers.server import _pal_error

    request = httpx.Request("GET", "http://127.0.0.1:8212/v1/api/metrics")

    # 接続不可（サーバ停止中）→ 502。500 にしない
    connect = _pal_error(httpx.ConnectError("All connection attempts failed"))
    assert connect.status_code == 502
    assert "接続できません" in connect.detail

    # タイムアウト → 504。TimeoutException は RequestError の派生なので
    # 判定順を誤ると 502 に落ちる。順序の回帰を固定する。
    timeout = _pal_error(httpx.ReadTimeout("timed out", request=request))
    assert timeout.status_code == 504
    assert "タイムアウト" in timeout.detail

    # 上流のHTTPエラー → 502（従来どおり本文を添える）
    response = httpx.Response(503, text="Service Unavailable", request=request)
    status = _pal_error(httpx.HTTPStatusError("boom", request=request, response=response))
    assert status.status_code == 502
    assert "503" in status.detail

    # OSError 系（DNS 解決失敗など）も 502 に寄せる
    os_error = _pal_error(OSError("network is unreachable"))
    assert os_error.status_code == 502

    # 不正 JSON レスポンス → 502（500 とトレースバックにしない）
    invalid_json = _pal_error(PalInvalidResponseError("unexpected non-JSON"))
    assert invalid_json.status_code == 502
    assert "解釈できない" in invalid_json.detail


def test_palworld_client_parse_json_raises_on_invalid_response():
    """palworld_client._parse_json が不正 JSON で PalInvalidResponseError を送出すること"""
    import httpx

    from palworld_client import PalInvalidResponseError, _parse_json

    # 正常なJSON
    ok = httpx.Response(200, text='{"running": true}')
    assert _parse_json(ok) == {"running": True}

    # 不正JSON（HTTP 200 だが body が壊れている）
    bad = httpx.Response(200, text="<html>Not Found</html>")
    try:
        _parse_json(bad)
        assert False, "PalInvalidResponseError が送出されませんでした"
    except PalInvalidResponseError:
        pass

    # 空レスポンス
    empty = httpx.Response(200, text="")
    try:
        _parse_json(empty)
        assert False, "PalInvalidResponseError が送出されませんでした"
    except PalInvalidResponseError:
        pass


def test_main_registers_new_routers_and_history_sampler():
    """追加機能が FastAPI と定期収集へ統合されていること"""
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    for router_name in ("world_router", "history_router", "maintenance_router"):
        assert f"app.include_router({router_name}.router)" in source
    assert "history_store.init_db()" in source
    assert "history_store.sample" in source
    assert "await maintenance_coordinator.shutdown()" in source
    assert "await scheduler_service.shutdown_active_restarts()" in source


def test_cron_rejects_numeric_weekday():
    """cron 曜日に数字を指定すると拒否されること（0=月曜の混乱回避）"""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    sch = _fresh_scheduler(tmp)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        sch.scheduler.start()
        sch.add_schedule("c1", "L", "0 4 * * 0")
        assert False, "数字曜日が通ってしまった"
    except ValueError:
        pass
    finally:
        if sch.scheduler.running:
            sch.scheduler.shutdown(wait=False)
        asyncio.set_event_loop(None)
        loop.close()
    if tmp.exists():
        tmp.unlink()


# ── sudo: パスワードを送信し終える前にワイプしない ────────────────

def test_sudo_password_survives_wipe_until_written():
    """_write_sudo_password が finally でワイプしても sudo へ実値が渡る

    StreamWriter.write() は bytearray をコピーせず参照のまま保持し、
    drain() は実際の書き出しを保証しない。bytearray をそのまま渡すと
    finally の _wipe_secret() が送信前バッファをゼロ埋めしてしまい、
    sudo には NUL 列が渡って "auth could not identify password" になる。
    """
    import maintenance

    written = []

    class FakeStdin:
        def write(self, data):
            # 実装と同じく参照を保持する（コピーしない）
            written.append(data)

        async def drain(self):
            # バッファに空きがあれば即座に返る
            await asyncio.sleep(0)

        def close(self):
            pass

    class FakeProc:
        stdin = FakeStdin()

    async def run():
        secret = bytearray(b"correct-horse")
        await maintenance._write_sudo_password(FakeProc(), secret)
        # 呼び出し元の bytearray はワイプ済みであること（秘密保持）
        assert set(secret) == {0}, "呼び出し元のバッファがワイプされていない"

    asyncio.new_event_loop().run_until_complete(run())

    assert len(written) == 1, f"write 回数が想定外: {len(written)}"
    sent = bytes(written[0])
    assert sent == b"correct-horse\n", f"sudo へ渡る値が壊れている: {sent!r}"
    assert set(sent) != {0}, "送信前にゼロ埋めされている"


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

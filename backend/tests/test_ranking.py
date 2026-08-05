"""
ランキング収集（ranking_tracker）のテスト。

依存（httpx ほか）が必要。実行:
    cd backend
    python -m pytest tests/test_ranking.py -v
または pytest 無しでも動くよう、末尾に簡易ランナーを用意している:
    python tests/test_ranking.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# backend をインポートパスに追加
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _fresh_tracker(tmp_dir: Path, tz: str = "Asia/Tokyo"):
    """SCHEDULE_TIMEZONE を固定して config ごとリロードした tracker を返す"""
    import importlib
    os.environ["SCHEDULE_TIMEZONE"] = tz
    import config
    importlib.reload(config)
    import ranking_tracker
    importlib.reload(ranking_tracker)
    ranking_tracker.RANKING_FILE = tmp_dir / "ranking.json"
    ranking_tracker.BACKUP_FILE = tmp_dir / "ranking.json.bak"
    return ranking_tracker


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ranking_test_"))


def _players(*specs):
    """[(userId, level), ...] からモックの /players 応答を作る"""
    return [{"userId": uid, "name": "N-" + uid, "accountName": "A-" + uid, "level": lv}
            for uid, lv in specs]


# ── 積算ロジック（純粋関数 _apply_sample）──────────────────────────

def test_accumulates_only_for_persistent_players():
    """前回・今回の両方にいるプレイヤーだけに elapsed が加算されること"""
    rt = _fresh_tracker(_tmp_dir())
    data = rt._empty_data()
    # 初回サンプル: u1 のみ。prev が空なので加算されない
    ids, _ = rt._apply_sample(data, _players(("u1", 5)), 10.0, "2026-07-19", set())
    assert ids == {"u1"}
    assert data["players"]["u1"]["total_seconds"] == 0.0
    # 2回目: u1 は継続（加算）、u2 は初出（加算されない）
    ids, _ = rt._apply_sample(data, _players(("u1", 5), ("u2", 3)), 10.0, "2026-07-19", ids)
    assert data["players"]["u1"]["total_seconds"] == 10.0
    assert data["players"]["u2"]["total_seconds"] == 0.0
    # 3回目: 両方継続で両方に加算
    ids, _ = rt._apply_sample(data, _players(("u1", 5), ("u2", 3)), 10.0, "2026-07-19", ids)
    assert data["players"]["u1"]["total_seconds"] == 20.0
    assert data["players"]["u2"]["total_seconds"] == 10.0


def test_max_level_keeps_max():
    """観測レベルが下がっても最高値を保持すること"""
    rt = _fresh_tracker(_tmp_dir())
    data = rt._empty_data()
    ids, _ = rt._apply_sample(data, _players(("u1", 10)), 0.0, "2026-07-19", set())
    rt._apply_sample(data, _players(("u1", 3)), 10.0, "2026-07-19", ids)
    assert data["players"]["u1"]["max_level"] == 10


def test_login_days_counts_unique_dates():
    """同日2回目の観測では増えず、日付が変わると +1 されること"""
    rt = _fresh_tracker(_tmp_dir())
    data = rt._empty_data()
    ids, _ = rt._apply_sample(data, _players(("u1", 1)), 0.0, "2026-07-19", set())
    rt._apply_sample(data, _players(("u1", 1)), 10.0, "2026-07-19", ids)
    assert data["players"]["u1"]["login_days"] == 1
    rt._apply_sample(data, _players(("u1", 1)), 10.0, "2026-07-20", ids)
    assert data["players"]["u1"]["login_days"] == 2


def test_missing_userid_skipped():
    """userId のないエントリはスキップされ、クラッシュしないこと"""
    rt = _fresh_tracker(_tmp_dir())
    data = rt._empty_data()
    ids, changed = rt._apply_sample(data, [{"name": "ghost"}], 10.0, "2026-07-19", set())
    assert ids == set()
    assert changed is False
    assert data["players"] == {}


def test_duplicate_userid_counted_once():
    """同一応答内に userId が重複しても二重加算されないこと"""
    rt = _fresh_tracker(_tmp_dir())
    data = rt._empty_data()
    dup = _players(("u1", 5), ("u1", 5))
    ids, _ = rt._apply_sample(data, dup, 0.0, "2026-07-19", set())
    assert ids == {"u1"}
    rt._apply_sample(data, dup, 10.0, "2026-07-19", ids)
    assert data["players"]["u1"]["total_seconds"] == 10.0


# ── タイムゾーン（日付境界・フォールバック）──────────────────────

def test_today_respects_schedule_timezone():
    """SCHEDULE_TIMEZONE 基準で日付境界が判定されること（UTC 15:00 = JST 0:00）"""
    rt = _fresh_tracker(_tmp_dir(), tz="Asia/Tokyo")
    assert rt._today(datetime(2026, 7, 19, 14, 59, tzinfo=timezone.utc)) == "2026-07-19"
    assert rt._today(datetime(2026, 7, 19, 15, 1, tzinfo=timezone.utc)) == "2026-07-20"


def test_invalid_timezone_falls_back_with_warning():
    """不正なタイムゾーン設定ではローカル時刻へフォールバックし、警告フラグが立つこと"""
    rt = _fresh_tracker(_tmp_dir(), tz="Invalid/Zone")
    assert rt._tz() is None
    assert rt._tz_warned is True
    # フォールバック時も日付文字列は返せる
    assert len(rt._today()) == 10


def test_zoneinfo_unavailable_warns_and_falls_back():
    """zoneinfo 自体が使えない環境でも警告を出してローカル時刻で動くこと"""
    rt = _fresh_tracker(_tmp_dir())
    rt.ZoneInfo = None  # Python 3.8 以下相当の状態を再現
    rt._tz_warned = False
    assert rt._tz() is None
    assert rt._tz_warned is True, "ZoneInfo=None の経路で警告フラグが立っていない"
    assert len(rt._today()) == 10


# ── sample()（API 失敗・異常ギャップ・保存）───────────────────────

def _ok_players(*specs):
    async def f():
        return {"players": _players(*specs)}
    return f


def test_sample_resets_baseline_on_api_failure():
    """API 失敗後の最初の成功サンプルでは加算されず、その次から再開すること"""
    rt = _fresh_tracker(_tmp_dir())
    rt.load_data()

    async def fail():
        raise RuntimeError("palworld down")

    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())  # ベースライン確立
        loop.run_until_complete(rt.sample())  # 加算
        before = rt._data["players"]["u1"]["total_seconds"]
        assert before > 0

        rt.pal.get_players = fail
        loop.run_until_complete(rt.sample())  # 失敗 → ベースラインリセット
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())  # 復帰直後は加算されない
        assert rt._data["players"]["u1"]["total_seconds"] == before

        loop.run_until_complete(rt.sample())  # 次からは加算再開
        assert rt._data["players"]["u1"]["total_seconds"] > before
    finally:
        loop.close()


def test_abnormal_gap_not_counted():
    """サンプル間隔の3倍を超えるギャップは計上されないこと"""
    rt = _fresh_tracker(_tmp_dir())
    rt.load_data()
    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())
        loop.run_until_complete(rt.sample())
        before = rt._data["players"]["u1"]["total_seconds"]
        rt._prev_mono = time.monotonic() - 1000  # 異常ギャップを注入
        loop.run_until_complete(rt.sample())
        assert rt._data["players"]["u1"]["total_seconds"] == before
    finally:
        loop.close()


def test_sample_resets_baseline_on_malformed_response():
    """HTTP 成功でも応答の形が不正なら、通信失敗と同様にベースラインが倒れること"""
    rt = _fresh_tracker(_tmp_dir())
    rt.load_data()

    async def malformed_top():
        return ["not", "a", "dict"]

    async def malformed_entry():
        return {"players": ["not-a-dict"]}

    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())
        loop.run_until_complete(rt.sample())
        before = rt._data["players"]["u1"]["total_seconds"]
        assert before > 0
        for bad in (malformed_top, malformed_entry):
            rt.pal.get_players = bad
            loop.run_until_complete(rt.sample())
            assert rt._prev_mono is None, f"{bad.__name__}: ベースラインが倒れていない"
            rt.pal.get_players = _ok_players(("u1", 5))
            loop.run_until_complete(rt.sample())  # 復帰直後は加算されない
            assert rt._data["players"]["u1"]["total_seconds"] == before
            loop.run_until_complete(rt.sample())  # 次からは加算再開
            assert rt._data["players"]["u1"]["total_seconds"] > before
            before = rt._data["players"]["u1"]["total_seconds"]
    finally:
        loop.close()


def test_sample_persists_valid_json():
    """サンプル後の ranking.json が常に valid JSON で読めること"""
    rt = _fresh_tracker(_tmp_dir())
    rt.load_data()
    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())
    finally:
        loop.close()
    saved = json.loads(rt.RANKING_FILE.read_text(encoding="utf-8"))
    assert "u1" in saved["players"]
    assert saved["version"] == 1


def test_no_save_when_no_players():
    """無人の間は書き込みが発生しないこと"""
    rt = _fresh_tracker(_tmp_dir())
    rt.load_data()
    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players()
        loop.run_until_complete(rt.sample())
    finally:
        loop.close()
    assert not rt.RANKING_FILE.exists()


# ── 永続化の復元（破損・バックアップ）──────────────────────────────

def test_load_resumes_from_existing_file():
    """既存の ranking.json から続きの値で再開できること"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    payload = {"version": 1, "started_at": "2026-07-19T00:00:00", "last_updated": None,
               "players": {"u1": {"name": "N", "accountName": "A", "total_seconds": 123.0,
                                  "max_level": 7, "login_days": 2, "last_login_date": "2026-07-18",
                                  "first_seen": "x", "last_seen": "y"}}}
    rt.RANKING_FILE.write_text(json.dumps(payload), encoding="utf-8")
    rt.load_data()
    assert rt._data["players"]["u1"]["total_seconds"] == 123.0


def test_corrupt_file_falls_back_to_backup():
    """壊れた ranking.json は退避され、.bak から復元されること"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    backup = {"version": 1, "started_at": "2026-07-19T00:00:00", "last_updated": None,
              "players": {"u1": {"total_seconds": 50.0}}}
    rt.RANKING_FILE.write_text("{ broken json", encoding="utf-8")
    rt.BACKUP_FILE.write_text(json.dumps(backup), encoding="utf-8")
    rt.load_data()
    assert rt._data["players"]["u1"]["total_seconds"] == 50.0
    # 壊れたファイルは .corrupt-* に退避されている（黙って消さない）
    assert list(tmp.glob("ranking.json.corrupt-*")), "壊れたファイルが退避されていない"


def test_both_corrupt_starts_fresh():
    """本体・バックアップ両方が壊れていたら退避して新規開始すること"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    rt.RANKING_FILE.write_text("{ broken", encoding="utf-8")
    rt.BACKUP_FILE.write_text("also broken", encoding="utf-8")
    rt.load_data()
    assert rt._data["players"] == {}
    assert list(tmp.glob("ranking.json.corrupt-*"))


def test_invalid_utf8_treated_as_corrupt():
    """UTF-8 として読めないファイルでも起動失敗せず、.bak から復旧すること"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    backup = {"version": 1, "started_at": "x", "last_updated": None,
              "players": {"u1": {"total_seconds": 50.0}}}
    rt.RANKING_FILE.write_bytes(b"\xff\xfe\x00broken bytes")
    rt.BACKUP_FILE.write_text(json.dumps(backup), encoding="utf-8")
    rt.load_data()  # UnicodeDecodeError で落ちないこと
    assert rt._data["players"]["u1"]["total_seconds"] == 50.0
    assert list(tmp.glob("ranking.json.corrupt-*")), "不正エンコードのファイルが退避されていない"


def test_load_sanitizes_broken_player_records():
    """型の壊れたレコードは正規化・破棄され、積算と API 返却が例外にならないこと"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    payload = {"version": 1, "started_at": "x", "last_updated": None,
               "players": {"u1": "not-a-dict",
                           "u2": {"total_seconds": "abc", "max_level": None, "login_days": "2"}}}
    rt.RANKING_FILE.write_text(json.dumps(payload), encoding="utf-8")
    rt.load_data()
    assert "u1" not in rt._data["players"], "dict でないレコードが残っている"
    e = rt._data["players"]["u2"]
    assert e["total_seconds"] == 0.0
    assert e["max_level"] == 0
    assert e["login_days"] == 2
    # 正規化後は API 返却・積算とも例外にならない
    view = rt.get_ranking_view()
    assert view["players"][0]["userId"] == "u2"
    rt._apply_sample(rt._data, _players(("u2", 5)), 10.0, "2026-07-19", {"u2"})
    assert rt._data["players"]["u2"]["total_seconds"] == 10.0


def test_sanitize_rejects_non_finite_numbers():
    """1e999（inf）/ NaN / 負数が既定値へ倒され、起動も厳格 JSON 返却も失敗しないこと"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    payload = {"version": 1, "started_at": "x", "last_updated": None,
               "players": {"u1": {"total_seconds": "NaN", "max_level": 1e999,
                                  "login_days": -5}}}
    rt.RANKING_FILE.write_text(json.dumps(payload), encoding="utf-8")
    rt.load_data()  # OverflowError で落ちないこと
    e = rt._data["players"]["u1"]
    assert e["total_seconds"] == 0.0
    assert e["max_level"] == 0
    assert e["login_days"] == 0
    # FastAPI 相当の厳格シリアライズ（allow_nan=False）でも例外にならない
    json.dumps(rt.get_ranking_view(), allow_nan=False)
    # API 側から inf レベルが来ても例外にならず、既定値扱いになる
    rt._apply_sample(rt._data, [{"userId": "u1", "level": float("inf")}], 0.0, "2026-07-19", set())
    assert rt._data["players"]["u1"]["max_level"] == 0


def test_backup_mirrors_main_and_survives_update_failure():
    """.bak が本体の複製として atomic に作られ、更新失敗時も旧 .bak が壊れないこと"""
    tmp = _tmp_dir()
    rt = _fresh_tracker(tmp)
    rt.load_data()
    loop = asyncio.new_event_loop()
    try:
        rt.pal.get_players = _ok_players(("u1", 5))
        loop.run_until_complete(rt.sample())
    finally:
        loop.close()
    assert rt.BACKUP_FILE.exists(), "初回サンプルで .bak が作られていない"
    assert rt.BACKUP_FILE.read_bytes() == rt.RANKING_FILE.read_bytes()
    old = rt.BACKUP_FILE.read_bytes()

    # 置換（os.replace）の途中で失敗しても、旧 .bak は同一内容のまま残り、一時ファイルも残らない
    orig_replace = rt.os.replace

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    rt.os.replace = fail_replace
    try:
        rt._last_backup_mono = None  # 間隔ガードを外して強制実行
        rt._backup_if_due()
    finally:
        rt.os.replace = orig_replace
    assert rt.BACKUP_FILE.read_bytes() == old, "置換失敗で旧 .bak が壊れた"
    assert not list(tmp.glob("*.tmp")), "一時ファイルが残っている"

    # 本体が読めない状態で更新が走っても、既存 .bak は変化しない
    rt.RANKING_FILE.unlink()
    rt._last_backup_mono = None
    rt._backup_if_due()
    assert rt.BACKUP_FILE.read_bytes() == old


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

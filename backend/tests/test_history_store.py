# -*- coding: utf-8 -*-
"""SQLite 履歴の回帰テスト。

実行方法:
    cd backend
    python -m pytest tests/test_history_store.py -v
または pytest なしで:
    python tests/test_history_store.py
"""

import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import history_store as hs


@contextmanager
def _temporary_database():
    old_path = hs.DB_PATH
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "history-test.db"
        hs.set_database_path(path)
        hs.init_db()
        try:
            yield path
        finally:
            hs.set_database_path(old_path)


def _game(fps=60, frame_time=16.67, players=1):
    return {
        "serverfps": fps,
        "serverframetime": frame_time,
        "currentplayernum": players,
        "maxplayernum": 32,
        "uptime": 1234,
        "basecampnum": 4,
        "days": 20,
    }


def _system(cpu=20, memory=50, pal_memory=2048):
    return {
        "system": {
            "cpu_percent": cpu,
            "mem_percent": memory,
            "mem_used_gb": 8,
            "mem_total_gb": 16,
            "disk_percent": 40,
            "disk_used_gb": 200,
            "disk_total_gb": 500,
        },
        "palworld": {"cpu_percent": 10, "mem_mb": pal_memory},
    }


def test_session_start_continue_and_end():
    with _temporary_database():
        first = hs.update_player_sessions(
            [{"userId": "u1", "name": "Alice", "accountName": "steam-a"}], 1000
        )
        assert first == {"started": 1, "updated": 0, "ended": 0, "active": 1}

        second = hs.update_player_sessions(
            [{"userId": "u1", "name": "Alice2", "accountName": "steam-a"}], 1030
        )
        assert second["updated"] == 1
        active = hs.query_sessions(active_only=True, now=1030)
        assert len(active) == 1
        assert active[0]["active"] is True
        assert active[0]["name"] == "Alice2"
        assert active[0]["duration_seconds"] == 30

        ended = hs.update_player_sessions([], 1060)
        assert ended["ended"] == 1
        sessions = hs.query_sessions(now=1060)
        assert sessions[0]["active"] is False
        assert sessions[0]["duration_seconds"] == 60
        assert sessions[0]["ended_at"].endswith("Z")
        assert hs.query_sessions(active_only=True, now=1060) == []


def test_metrics_query_and_equal_interval_downsample():
    with _temporary_database():
        for index in range(7):
            hs.record_metrics(_game(fps=50 + index), _system(cpu=10 + index), 1000 + index * 10)

        points = hs.query_metrics(hours=1, limit=3, now=1060)
        assert len(points) == 3
        assert [point["sampled_at_epoch"] for point in points] == [1000, 1030, 1060]
        assert [point["server_fps"] for point in points] == [50, 53, 56]
        assert points == sorted(points, key=lambda point: point["sampled_at_epoch"])

        # 上限を超える limit はストア層でも 2000 に抑える。
        assert len(hs.query_metrics(hours=1, limit=99999, now=1060)) == 7


def test_summary_with_and_without_data():
    with _temporary_database():
        empty = hs.get_summary(now=2000)
        assert empty["metric_samples"] == 0
        assert empty["avg_fps"] is None
        assert empty["unique_players"] == 0
        assert empty["session_count"] == 0
        json.dumps(empty, allow_nan=False)

        hs.record_metrics(_game(fps=50, frame_time=20, players=1), _system(), 1900)
        hs.record_metrics(_game(fps=70, frame_time=10, players=3), _system(), 1950)
        hs.update_player_sessions([{"userId": "u1", "name": "A"}], 1900)
        hs.update_player_sessions(
            [{"userId": "u1", "name": "A"}, {"userId": "u2", "name": "B"}], 1920
        )
        hs.update_player_sessions([], 1980)

        summary = hs.get_summary(now=2000)
        assert summary["peak_fps"] == 70
        assert summary["avg_fps"] == 60
        assert summary["avg_frame_time_ms"] == 15
        assert summary["peak_players"] == 3
        assert summary["unique_players"] == 2
        assert summary["session_count"] == 2
        assert summary["active_sessions"] == 0
        json.dumps(summary, allow_nan=False)


def test_purge_keeps_recent_metrics_and_sessions():
    with _temporary_database():
        now = 4_000_000
        hs.record_metrics(_game(fps=1), _system(), now - 31 * 86400)
        hs.record_metrics(_game(fps=2), _system(), now - 30 * 86400)
        hs.record_metrics(_game(fps=3), _system(), now - 10)
        hs.update_player_sessions([{"userId": "old-session"}], now - 40 * 86400)
        hs.update_player_sessions([], now - 39 * 86400)

        assert hs.purge_old_metrics(now=now) == 1
        points = hs.query_metrics(hours=31 * 24, now=now)
        assert [point["server_fps"] for point in points] == [2, 3]
        assert len(hs.query_sessions(now=now)) == 1


def test_type_corruption_and_non_finite_values_never_reach_json():
    with _temporary_database() as path:
        hs.record_metrics(
            _game(fps=float("nan"), frame_time="bad", players=float("inf")),
            _system(cpu="NaN", memory=-1, pal_memory=float("inf")),
            1000,
        )
        # DB を手編集された場合の inf / 文字列も API 境界で無害化する。
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE metrics SET server_fps = ?, server_frame_time_ms = ?, "
                "current_players = ?",
                (float("inf"), "broken", "not-an-int"),
            )
            conn.commit()
        finally:
            conn.close()

        points = hs.query_metrics(hours=1, now=1000)
        assert points[0]["server_fps"] is None
        assert points[0]["server_frame_time_ms"] is None
        assert points[0]["current_players"] is None
        json.dumps(points, allow_nan=False)
        json.dumps(hs.get_summary(now=1000), allow_nan=False)


def test_sample_continues_when_one_source_fails():
    with _temporary_database():
        originals = (hs.pal.get_metrics, hs.pal.get_players, hs.system_metrics.get_system_metrics)

        async def failed_metrics():
            raise RuntimeError("metrics unavailable")

        async def players():
            return {"players": [{"userId": "u1", "name": "Alice"}]}

        async def system():
            return _system()

        hs.pal.get_metrics = failed_metrics
        hs.pal.get_players = players
        hs.system_metrics.get_system_metrics = system
        try:
            result = asyncio.run(hs.sample(now=1000))
        finally:
            hs.pal.get_metrics, hs.pal.get_players, hs.system_metrics.get_system_metrics = originals

        assert result["metrics_saved"] is True
        assert result["sessions_updated"] is True
        assert result["errors"]["game_metrics"] == "RuntimeError"
        point = hs.query_metrics(hours=1, now=1000)[0]
        assert point["game_available"] is False
        assert point["system_available"] is True
        assert hs.query_sessions(active_only=True, now=1000)[0]["user_id"] == "u1"


def test_malformed_player_response_does_not_end_active_session():
    with _temporary_database():
        hs.update_player_sessions([{"userId": "u1"}], 1000)
        originals = (hs.pal.get_metrics, hs.pal.get_players, hs.system_metrics.get_system_metrics)

        async def metrics():
            return _game()

        async def malformed_players():
            return {"players": ["not-an-object"]}

        async def system():
            return _system()

        hs.pal.get_metrics = metrics
        hs.pal.get_players = malformed_players
        hs.system_metrics.get_system_metrics = system
        try:
            result = asyncio.run(hs.sample(now=1030))
        finally:
            hs.pal.get_metrics, hs.pal.get_players, hs.system_metrics.get_system_metrics = originals

        assert result["sessions_updated"] is False
        assert hs.query_sessions(active_only=True, now=1030)[0]["user_id"] == "u1"


def test_unobserved_session_becomes_stale_and_does_not_gain_outage_time():
    with _temporary_database():
        hs.update_player_sessions([{"userId": "u1", "name": "Alice"}], 1000)
        stale_at = 1000 + hs.SESSION_STALE_AFTER_SECONDS + 1

        session = hs.query_sessions(now=stale_at)[0]
        assert session["active"] is False
        assert session["stale"] is True
        assert session["duration_seconds"] == hs.SAMPLE_INTERVAL_SECONDS
        assert hs.query_sessions(active_only=True, now=stale_at) == []

        assert hs.expire_stale_sessions(now=stale_at) == 1
        ended = hs.query_sessions(now=stale_at)[0]
        assert ended["ended_at"] is not None
        assert ended["duration_seconds"] == hs.SAMPLE_INTERVAL_SECONDS


def test_reappearing_player_after_stale_gap_starts_a_new_session():
    with _temporary_database():
        hs.update_player_sessions([{"userId": "u1", "name": "Alice"}], 1000)
        resumed_at = 1000 + hs.SESSION_STALE_AFTER_SECONDS + 30
        changes = hs.update_player_sessions(
            [{"userId": "u1", "name": "Alice"}], resumed_at
        )

        assert changes["ended"] == 1
        assert changes["started"] == 1
        sessions = hs.query_sessions(now=resumed_at)
        assert len(sessions) == 2
        assert sessions[0]["active"] is True
        assert sessions[1]["duration_seconds"] == hs.SAMPLE_INTERVAL_SECONDS


def test_old_ended_sessions_are_purged_with_metrics():
    with _temporary_database():
        now = 4_000_000
        hs.update_player_sessions([{"userId": "old"}], now - 40 * 86400)
        hs.update_player_sessions([], now - 39 * 86400)
        hs.update_player_sessions([{"userId": "recent"}], now - 10)
        hs.update_player_sessions([], now)

        assert hs.purge_old_sessions(now=now) == 1
        sessions = hs.query_sessions(now=now)
        assert [session["user_id"] for session in sessions] == ["recent"]


def _run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception as exc:
            print(f" FAIL {test.__name__}: {exc}")
            failed.append(test.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())

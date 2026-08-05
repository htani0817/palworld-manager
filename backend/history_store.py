# -*- coding: utf-8 -*-
"""サーバーメトリクスとプレイヤーセッションの SQLite 履歴。

接続は操作ごとに開閉し、プロセス内ロックと SQLite の WAL / busy timeout を
併用する。単一 worker で、30 秒サンプラーと参照 API が同時に動く構成を
想定している。
"""

import asyncio
import logging
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Union

import palworld_client as pal
import system_metrics

logger = logging.getLogger("palworld_manager.history")

DB_PATH: Union[Path, str] = Path(__file__).parent / "manager_history.db"
SAMPLE_INTERVAL_SECONDS = 30
SESSION_STALE_AFTER_SECONDS = SAMPLE_INTERVAL_SECONDS * 3
RETENTION_DAYS = 30
PURGE_INTERVAL_SECONDS = 60 * 60
MAX_QUERY_LIMIT = 2000

_DB_LOCK = threading.RLock()
_SAMPLE_LOCK = asyncio.Lock()
_last_purge_monotonic: Optional[float] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at REAL NOT NULL,
    game_available INTEGER NOT NULL DEFAULT 0,
    system_available INTEGER NOT NULL DEFAULT 0,
    server_fps REAL,
    server_frame_time_ms REAL,
    current_players INTEGER,
    max_players INTEGER,
    uptime_seconds REAL,
    basecamp_count INTEGER,
    game_days INTEGER,
    system_cpu_percent REAL,
    system_memory_percent REAL,
    system_memory_used_gb REAL,
    system_memory_total_gb REAL,
    system_disk_percent REAL,
    system_disk_used_gb REAL,
    system_disk_total_gb REAL,
    palworld_cpu_percent REAL,
    palworld_memory_mb REAL
);

CREATE INDEX IF NOT EXISTS idx_metrics_sampled_at
    ON metrics(sampled_at);

CREATE TABLE IF NOT EXISTS player_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    player_name TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    ended_at REAL,
    duration_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_player_sessions_started_at
    ON player_sessions(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_player_sessions_user_id
    ON player_sessions(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_player_sessions_one_active
    ON player_sessions(user_id) WHERE ended_at IS NULL;
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """短命な SQLite 接続を返し、必ず閉じる。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def set_database_path(path: Union[Path, str]) -> None:
    """DB の保存先を差し替える（主にテスト用）。

    呼び出し中の DB 操作が終わってから切り替わるため、テストでも別 DB の
    接続が混ざらない。
    """
    global DB_PATH, _last_purge_monotonic
    with _DB_LOCK:
        DB_PATH = Path(path) if str(path) != ":memory:" else ":memory:"
        _last_purge_monotonic = None


def init_db() -> None:
    """テーブルとインデックスを冪等に作成する。"""
    with _DB_LOCK:
        location = str(DB_PATH)
        if location != ":memory:":
            Path(location).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            # WAL は読み取り API とサンプラーの短時間競合を減らす。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()


def _finite_float(value, *, minimum: Optional[float] = None) -> Optional[float]:
    """有限な float へ変換する。不正値や範囲外は None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def _finite_int(value, *, minimum: Optional[int] = None) -> Optional[int]:
    """SQLite INTEGER に安全に入る有限な整数へ変換する。"""
    number = _finite_float(value)
    if number is None:
        return None
    integer = int(number)
    if minimum is not None and integer < minimum:
        return None
    if integer < -(2**63) or integer > 2**63 - 1:
        return None
    return integer


def _coerce_timestamp(value=None) -> float:
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    timestamp = _finite_float(value)
    if timestamp is None:
        raise ValueError("時刻は有限な datetime または Unix 時刻で指定してください")
    # API 返却時に datetime へ戻せない極端な値は受け付けない。
    try:
        datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("時刻が扱える範囲を超えています") from exc
    return timestamp


def _iso_timestamp(value) -> Optional[str]:
    timestamp = _finite_float(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _safe_text(value, *, max_length: int = 512) -> str:
    if isinstance(value, str):
        return value[:max_length]
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        number = _finite_float(value)
        return str(value)[:max_length] if number is not None else ""
    return ""


def _user_id(value) -> Optional[str]:
    text = _safe_text(value, max_length=256).strip()
    return text or None


def _metric_values(game_metrics: Optional[dict], system_snapshot: Optional[dict]) -> dict:
    game = game_metrics if isinstance(game_metrics, dict) else {}
    snapshot = system_snapshot if isinstance(system_snapshot, dict) else {}
    system = snapshot.get("system")
    palworld = snapshot.get("palworld")
    system = system if isinstance(system, dict) else {}
    palworld = palworld if isinstance(palworld, dict) else {}

    return {
        "game_available": int(isinstance(game_metrics, dict)),
        "system_available": int(isinstance(system_snapshot, dict)),
        "server_fps": _finite_float(game.get("serverfps"), minimum=0.0),
        "server_frame_time_ms": _finite_float(game.get("serverframetime"), minimum=0.0),
        "current_players": _finite_int(game.get("currentplayernum"), minimum=0),
        "max_players": _finite_int(game.get("maxplayernum"), minimum=0),
        "uptime_seconds": _finite_float(game.get("uptime"), minimum=0.0),
        "basecamp_count": _finite_int(game.get("basecampnum"), minimum=0),
        "game_days": _finite_int(game.get("days"), minimum=0),
        "system_cpu_percent": _finite_float(system.get("cpu_percent"), minimum=0.0),
        "system_memory_percent": _finite_float(system.get("mem_percent"), minimum=0.0),
        "system_memory_used_gb": _finite_float(system.get("mem_used_gb"), minimum=0.0),
        "system_memory_total_gb": _finite_float(system.get("mem_total_gb"), minimum=0.0),
        "system_disk_percent": _finite_float(system.get("disk_percent"), minimum=0.0),
        "system_disk_used_gb": _finite_float(system.get("disk_used_gb"), minimum=0.0),
        "system_disk_total_gb": _finite_float(system.get("disk_total_gb"), minimum=0.0),
        "palworld_cpu_percent": _finite_float(palworld.get("cpu_percent"), minimum=0.0),
        "palworld_memory_mb": _finite_float(palworld.get("mem_mb"), minimum=0.0),
    }


def record_metrics(
    game_metrics: Optional[dict],
    system_snapshot: Optional[dict],
    sampled_at=None,
) -> int:
    """正規化した 1 サンプルを保存し、行 ID を返す。"""
    timestamp = _coerce_timestamp(sampled_at)
    values = _metric_values(game_metrics, system_snapshot)
    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO metrics (sampled_at, {', '.join(columns)}) "
        f"VALUES (?, {placeholders})"
    )
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            cursor = conn.execute(sql, (timestamp, *(values[column] for column in columns)))
            conn.commit()
            return int(cursor.lastrowid)


def _normalize_players(players: list) -> dict[str, dict]:
    """プレイヤー一覧を userId で重複排除する。

    要素の型や userId が壊れている場合は一覧全体を不正扱いにする。壊れた
    応答を「誰もいない」と解釈し、全アクティブセッションを終了する事故を
    防ぐためである。
    """
    if not isinstance(players, list):
        raise ValueError("players がリストではありません")
    normalized: dict[str, dict] = {}
    for player in players:
        if not isinstance(player, dict):
            raise ValueError("players の要素がオブジェクトではありません")
        user_id = _user_id(player.get("userId"))
        if user_id is None:
            raise ValueError("userId のないプレイヤーが含まれています")
        if user_id in normalized:
            continue
        normalized[user_id] = {
            "name": _safe_text(player.get("name")),
            "account_name": _safe_text(player.get("accountName")),
        }
    return normalized


def update_player_sessions(players: list, sampled_at=None) -> dict:
    """現在のプレイヤー一覧からセッションの開始・継続・終了を反映する。"""
    timestamp = _coerce_timestamp(sampled_at)
    current = _normalize_players(players)
    result = {"started": 0, "updated": 0, "ended": 0, "active": len(current)}

    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT id, user_id, started_at, last_seen "
                    "FROM player_sessions WHERE ended_at IS NULL"
                ).fetchall()
                active = {str(row["user_id"]): row for row in rows}

                for user_id, player in current.items():
                    row = active.get(user_id)
                    if row is None:
                        conn.execute(
                            "INSERT INTO player_sessions "
                            "(user_id, player_name, account_name, started_at, last_seen) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                user_id,
                                player["name"],
                                player["account_name"],
                                timestamp,
                                timestamp,
                            ),
                        )
                        result["started"] += 1
                        continue

                    previous_seen = _finite_float(row["last_seen"])
                    if (
                        previous_seen is not None
                        and timestamp - previous_seen > SESSION_STALE_AFTER_SECONDS
                    ):
                        started = _finite_float(row["started_at"])
                        ended_at = previous_seen + SAMPLE_INTERVAL_SECONDS
                        if started is not None:
                            ended_at = max(started, ended_at)
                        duration = max(0.0, ended_at - started) if started is not None else 0.0
                        conn.execute(
                            "UPDATE player_sessions SET ended_at = ?, duration_seconds = ? "
                            "WHERE id = ?",
                            (ended_at, duration, row["id"]),
                        )
                        conn.execute(
                            "INSERT INTO player_sessions "
                            "(user_id, player_name, account_name, started_at, last_seen) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                user_id,
                                player["name"],
                                player["account_name"],
                                timestamp,
                                timestamp,
                            ),
                        )
                        result["ended"] += 1
                        result["started"] += 1
                        continue

                    seen_at = max(timestamp, previous_seen) if previous_seen is not None else timestamp
                    conn.execute(
                        "UPDATE player_sessions SET "
                        "last_seen = ?, "
                        "player_name = CASE WHEN ? <> '' THEN ? ELSE player_name END, "
                        "account_name = CASE WHEN ? <> '' THEN ? ELSE account_name END "
                        "WHERE id = ?",
                        (
                            seen_at,
                            player["name"],
                            player["name"],
                            player["account_name"],
                            player["account_name"],
                            row["id"],
                        ),
                    )
                    result["updated"] += 1

                missing_ids = set(active) - set(current)
                for user_id in missing_ids:
                    row = active[user_id]
                    started = _finite_float(row["started_at"])
                    last_seen = _finite_float(row["last_seen"])
                    ended_at = timestamp
                    if last_seen is not None:
                        ended_at = min(timestamp, last_seen + SAMPLE_INTERVAL_SECONDS)
                    if started is not None:
                        ended_at = max(started, ended_at)
                    duration = max(0.0, ended_at - started) if started is not None else 0.0
                    conn.execute(
                        "UPDATE player_sessions "
                        "SET ended_at = ?, duration_seconds = ? WHERE id = ?",
                        (ended_at, duration, row["id"]),
                    )
                    result["ended"] += 1

                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return result


def _normalize_limit(value, default: int) -> int:
    limit = _finite_int(value, minimum=1)
    if limit is None:
        limit = default
    return min(limit, MAX_QUERY_LIMIT)


def _normalize_hours(value, default: float = 24.0) -> float:
    hours = _finite_float(value, minimum=0.0)
    if hours is None or hours <= 0:
        return default
    return hours


def _metric_row(row: sqlite3.Row) -> dict:
    timestamp = _finite_float(row["sampled_at"])
    return {
        "id": _finite_int(row["id"], minimum=0) or 0,
        "sampled_at": _iso_timestamp(timestamp),
        "sampled_at_epoch": timestamp,
        "game_available": bool(_finite_int(row["game_available"], minimum=0) or 0),
        "system_available": bool(_finite_int(row["system_available"], minimum=0) or 0),
        "server_fps": _finite_float(row["server_fps"], minimum=0.0),
        "server_frame_time_ms": _finite_float(row["server_frame_time_ms"], minimum=0.0),
        "current_players": _finite_int(row["current_players"], minimum=0),
        "max_players": _finite_int(row["max_players"], minimum=0),
        "uptime_seconds": _finite_float(row["uptime_seconds"], minimum=0.0),
        "basecamp_count": _finite_int(row["basecamp_count"], minimum=0),
        "game_days": _finite_int(row["game_days"], minimum=0),
        "system_cpu_percent": _finite_float(row["system_cpu_percent"], minimum=0.0),
        "system_memory_percent": _finite_float(row["system_memory_percent"], minimum=0.0),
        "system_memory_used_gb": _finite_float(row["system_memory_used_gb"], minimum=0.0),
        "system_memory_total_gb": _finite_float(row["system_memory_total_gb"], minimum=0.0),
        "system_disk_percent": _finite_float(row["system_disk_percent"], minimum=0.0),
        "system_disk_used_gb": _finite_float(row["system_disk_used_gb"], minimum=0.0),
        "system_disk_total_gb": _finite_float(row["system_disk_total_gb"], minimum=0.0),
        "palworld_cpu_percent": _finite_float(row["palworld_cpu_percent"], minimum=0.0),
        "palworld_memory_mb": _finite_float(row["palworld_memory_mb"], minimum=0.0),
    }


def _downsample(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    """先頭と末尾を含む等間隔インデックスで行を間引く。"""
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    last = len(rows) - 1
    return [rows[round(index * last / (limit - 1))] for index in range(limit)]


def query_metrics(hours=24, limit=500, now=None) -> list[dict]:
    """指定期間のメトリクスを時間昇順で返す。"""
    until = _coerce_timestamp(now)
    window_hours = _normalize_hours(hours)
    safe_limit = _normalize_limit(limit, 500)
    since = until - window_hours * 60 * 60
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM metrics "
                "WHERE typeof(sampled_at) IN ('integer', 'real') "
                "AND sampled_at >= ? AND sampled_at <= ? "
                "ORDER BY sampled_at ASC, id ASC",
                (since, until),
            ).fetchall()
    return [_metric_row(row) for row in _downsample(rows, safe_limit)]


def _session_row(row: sqlite3.Row, now: float) -> dict:
    started = _finite_float(row["started_at"])
    last_seen = _finite_float(row["last_seen"])
    ended_raw = row["ended_at"]
    ended = _finite_float(ended_raw)
    open_session = ended_raw is None
    stale = open_session and (
        last_seen is None or now - last_seen > SESSION_STALE_AFTER_SECONDS
    )
    active = open_session and not stale
    stored_duration = _finite_float(row["duration_seconds"], minimum=0.0)

    if open_session and started is not None:
        if stale and last_seen is not None:
            effective_end = max(started, last_seen + SAMPLE_INTERVAL_SECONDS)
        else:
            effective_end = max(value for value in (now, last_seen, started) if value is not None)
        duration = max(0.0, effective_end - started)
    elif stored_duration is not None:
        duration = stored_duration
    elif started is not None and ended is not None:
        duration = max(0.0, ended - started)
    else:
        duration = 0.0

    return {
        "id": _finite_int(row["id"], minimum=0) or 0,
        "user_id": _safe_text(row["user_id"], max_length=256),
        "name": _safe_text(row["player_name"]),
        "account_name": _safe_text(row["account_name"]),
        "started_at": _iso_timestamp(started),
        "last_seen": _iso_timestamp(last_seen),
        "ended_at": _iso_timestamp(ended),
        "duration_seconds": duration,
        "active": active,
        "stale": stale,
    }


def query_sessions(limit=100, active_only=False, now=None) -> list[dict]:
    """セッションを開始時刻の新しい順で返す。"""
    timestamp = _coerce_timestamp(now)
    safe_limit = _normalize_limit(limit, 100)
    if bool(active_only):
        where = (
            "WHERE ended_at IS NULL "
            "AND typeof(last_seen) IN ('integer', 'real') AND last_seen >= ?"
        )
        params = (timestamp - SESSION_STALE_AFTER_SECONDS, safe_limit)
    else:
        where = ""
        params = (safe_limit,)
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM player_sessions {where} "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
    return [_session_row(row, timestamp) for row in rows]


def _average(values: list[float]) -> Optional[float]:
    if not values:
        return None
    result = sum(values) / len(values)
    return result if math.isfinite(result) else None


def get_summary(hours=24, now=None) -> dict:
    """指定期間（既定 24 時間）の集計を返す。データなしでも安全。"""
    until = _coerce_timestamp(now)
    window_hours = _normalize_hours(hours)
    since = until - window_hours * 60 * 60

    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            metric_rows = conn.execute(
                "SELECT server_fps, server_frame_time_ms, current_players, "
                "system_cpu_percent, system_memory_percent, palworld_memory_mb "
                "FROM metrics "
                "WHERE typeof(sampled_at) IN ('integer', 'real') "
                "AND sampled_at >= ? AND sampled_at <= ?",
                (since, until),
            ).fetchall()
            session_rows = conn.execute(
                "SELECT user_id, started_at, last_seen, ended_at, duration_seconds "
                "FROM player_sessions "
                "WHERE started_at <= ? AND (ended_at >= ? OR ("
                "ended_at IS NULL AND typeof(last_seen) IN ('integer', 'real') "
                "AND last_seen + ? >= ?))",
                (until, since, SAMPLE_INTERVAL_SECONDS, since),
            ).fetchall()

    fps = [value for row in metric_rows if (value := _finite_float(row["server_fps"], minimum=0.0)) is not None]
    frame_times = [
        value
        for row in metric_rows
        if (value := _finite_float(row["server_frame_time_ms"], minimum=0.0)) is not None
    ]
    player_counts = [
        value
        for row in metric_rows
        if (value := _finite_int(row["current_players"], minimum=0)) is not None
    ]
    system_cpu = [
        value
        for row in metric_rows
        if (value := _finite_float(row["system_cpu_percent"], minimum=0.0)) is not None
    ]
    system_memory = [
        value
        for row in metric_rows
        if (value := _finite_float(row["system_memory_percent"], minimum=0.0)) is not None
    ]
    palworld_memory = [
        value
        for row in metric_rows
        if (value := _finite_float(row["palworld_memory_mb"], minimum=0.0)) is not None
    ]

    unique_players = {
        user_id
        for row in session_rows
        if (user_id := _user_id(row["user_id"])) is not None
    }
    durations: list[float] = []
    active_sessions = 0
    for row in session_rows:
        if row["ended_at"] is None:
            started = _finite_float(row["started_at"])
            last_seen = _finite_float(row["last_seen"])
            stale = last_seen is None or until - last_seen > SESSION_STALE_AFTER_SECONDS
            if not stale:
                active_sessions += 1
            if started is not None:
                if stale and last_seen is not None:
                    end = max(started, last_seen + SAMPLE_INTERVAL_SECONDS)
                else:
                    end = max(value for value in (until, last_seen, started) if value is not None)
                durations.append(max(0.0, end - started))
            continue
        duration = _finite_float(row["duration_seconds"], minimum=0.0)
        if duration is None:
            started = _finite_float(row["started_at"])
            ended = _finite_float(row["ended_at"])
            if started is not None and ended is not None:
                duration = max(0.0, ended - started)
        if duration is not None:
            durations.append(duration)

    return {
        "hours": window_hours,
        "since": _iso_timestamp(since),
        "until": _iso_timestamp(until),
        "metric_samples": len(metric_rows),
        "peak_fps": max(fps) if fps else None,
        "avg_fps": _average(fps),
        "avg_frame_time_ms": _average(frame_times),
        "peak_players": max(player_counts) if player_counts else None,
        "avg_players": _average([float(value) for value in player_counts]),
        "unique_players": len(unique_players),
        "session_count": len(session_rows),
        "active_sessions": active_sessions,
        "avg_session_duration_seconds": _average(durations),
        "avg_system_cpu_percent": _average(system_cpu),
        "peak_system_memory_percent": max(system_memory) if system_memory else None,
        "peak_palworld_memory_mb": max(palworld_memory) if palworld_memory else None,
    }


def purge_old_metrics(retention_days=RETENTION_DAYS, now=None) -> int:
    """保持期間を過ぎたメトリクスを削除し、削除件数を返す。"""
    days = _finite_float(retention_days, minimum=0.0)
    if days is None or days <= 0:
        raise ValueError("保持日数は 0 より大きい有限数で指定してください")
    cutoff = _coerce_timestamp(now) - days * 24 * 60 * 60
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            cursor = conn.execute(
                "DELETE FROM metrics WHERE CAST(sampled_at AS REAL) < ?",
                (cutoff,),
            )
            conn.commit()
            return max(0, int(cursor.rowcount))


def expire_stale_sessions(now=None) -> int:
    """長時間観測できない開いたセッションを最終観測付近で閉じる。"""
    timestamp = _coerce_timestamp(now)
    cutoff = timestamp - SESSION_STALE_AFTER_SECONDS
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE player_sessions SET "
                "ended_at = MAX(started_at, last_seen + ?), "
                "duration_seconds = MAX(0, last_seen + ? - started_at) "
                "WHERE ended_at IS NULL "
                "AND typeof(last_seen) IN ('integer', 'real') AND last_seen < ?",
                (SAMPLE_INTERVAL_SECONDS, SAMPLE_INTERVAL_SECONDS, cutoff),
            )
            conn.commit()
            return max(0, int(cursor.rowcount))


def purge_old_sessions(retention_days=RETENTION_DAYS, now=None) -> int:
    """保持期間を過ぎた終了済みセッションを削除する。"""
    days = _finite_float(retention_days, minimum=0.0)
    if days is None or days <= 0:
        raise ValueError("保持日数は 0 より大きい有限数で指定してください")
    cutoff = _coerce_timestamp(now) - days * 24 * 60 * 60
    with _DB_LOCK:
        init_db()
        with _connect() as conn:
            cursor = conn.execute(
                "DELETE FROM player_sessions "
                "WHERE ended_at IS NOT NULL AND CAST(ended_at AS REAL) < ?",
                (cutoff,),
            )
            conn.commit()
            return max(0, int(cursor.rowcount))


def _purge_if_due(now: float) -> Optional[dict[str, int]]:
    global _last_purge_monotonic
    monotonic_now = time.monotonic()
    with _DB_LOCK:
        if (
            _last_purge_monotonic is not None
            and monotonic_now - _last_purge_monotonic < PURGE_INTERVAL_SECONDS
        ):
            return None
        deleted = {
            "metrics": purge_old_metrics(now=now),
            "sessions": purge_old_sessions(now=now),
        }
        _last_purge_monotonic = monotonic_now
        return deleted


def _valid_mapping(result, source: str, errors: dict) -> Optional[dict]:
    if isinstance(result, asyncio.CancelledError):
        raise result
    if isinstance(result, BaseException):
        errors[source] = type(result).__name__
        logger.warning("%s の履歴取得に失敗しました: %s", source, type(result).__name__)
        return None
    if not isinstance(result, dict):
        errors[source] = "invalid_response"
        logger.warning("%s の履歴取得結果がオブジェクトではありません", source)
        return None
    return result


async def sample(now=None) -> dict:
    """各 API を独立に取得し、成功した部分だけを履歴へ反映する。"""
    timestamp = _coerce_timestamp(now)
    async with _SAMPLE_LOCK:
        results = await asyncio.gather(
            pal.get_metrics(),
            pal.get_players(),
            system_metrics.get_system_metrics(),
            return_exceptions=True,
        )
        errors: dict[str, str] = {}
        game = _valid_mapping(results[0], "game_metrics", errors)
        players_response = _valid_mapping(results[1], "players", errors)
        system = _valid_mapping(results[2], "system_metrics", errors)

        metrics_saved = False
        sessions_updated = False
        session_changes = None

        if game is not None or system is not None:
            try:
                record_metrics(game, system, timestamp)
                metrics_saved = True
            except Exception as exc:
                errors["metrics_db"] = type(exc).__name__
                logger.exception("メトリクス履歴の保存に失敗しました")

        if players_response is not None:
            try:
                players = players_response.get("players")
                session_changes = update_player_sessions(players, timestamp)
                sessions_updated = True
            except (TypeError, ValueError) as exc:
                errors["players"] = "invalid_response"
                logger.warning("players 応答をセッション履歴へ反映できません: %s", exc)
            except Exception as exc:
                errors["sessions_db"] = type(exc).__name__
                logger.exception("プレイヤーセッション履歴の保存に失敗しました")

        try:
            expired_sessions = expire_stale_sessions(timestamp)
        except Exception as exc:
            expired_sessions = None
            errors["expire_sessions_db"] = type(exc).__name__
            logger.exception("観測期限を過ぎたプレイヤーセッションを終了できません")

        try:
            purged = _purge_if_due(timestamp)
        except Exception as exc:
            purged = None
            errors["purge_db"] = type(exc).__name__
            logger.exception("古いメトリクス履歴の削除に失敗しました")

        return {
            "sampled_at": _iso_timestamp(timestamp),
            "metrics_saved": metrics_saved,
            "sessions_updated": sessions_updated,
            "session_changes": session_changes,
            "expired_sessions": expired_sessions,
            "purged_history": purged,
            "purged_metrics": purged["metrics"] if purged is not None else None,
            "purged_sessions": purged["sessions"] if purged is not None else None,
            "errors": errors,
        }

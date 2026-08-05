# -*- coding: utf-8 -*-
"""Palworld のワールド Actor スナップショットを安全に集計する。"""

import asyncio
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

import palworld_client as pal


CACHE_TTL_SECONDS = 15.0
DEFAULT_POINT_LIMIT = 5000
MAX_POINT_LIMIT = 10000
MAX_WARNINGS_PER_KIND = 200
MAX_GUILDS = 1000
MAX_TEXT_LENGTH = 200

CHARACTER_UNIT_TYPES = (
    "Player",
    "OtomoPal",
    "BaseCampPal",
    "WildPal",
    "NPC",
)
COUNT_TYPES = CHARACTER_UNIT_TYPES + ("PalBox",)

_cache_entry: Optional[Tuple[bool, Any]] = None
_cache_deadline = 0.0
_cache_lock: Optional[asyncio.Lock] = None
_cache_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def normalize_is_active(value: Any) -> Optional[bool]:
    """公式 schema の bool / 文字列 bool を同じ値へ正規化する。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def normalize_point_limit(value: Any) -> int:
    """point 件数を安全な範囲へ収める。router 外からの呼び出しにも適用する。"""
    if isinstance(value, bool):
        return DEFAULT_POINT_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_POINT_LIMIT
    return max(0, min(limit, MAX_POINT_LIMIT))


def _safe_text(value: Any) -> Optional[str]:
    return value[:MAX_TEXT_LENGTH] if isinstance(value, str) else None


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        return int(number) if math.isfinite(number) and number.is_integer() else None
    return None


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _empty_counts() -> Dict[str, int]:
    return {actor_type: 0 for actor_type in COUNT_TYPES}


def _safe_point(actor: Dict[str, Any], actor_type: str, unit_type: Optional[str]) -> Dict[str, Any]:
    """許可した表示項目だけを抽出し、識別子や接続情報を落とす。"""
    return {
        "type": actor_type,
        "unit_type": unit_type,
        "nickname": _safe_text(actor.get("NickName")),
        "guild_name": _safe_text(actor.get("GuildName")),
        "level": _safe_int(actor.get("level")),
        "hp": _safe_int(actor.get("HP")),
        "max_hp": _safe_int(actor.get("MaxHP")),
        "is_active": normalize_is_active(actor.get("IsActive")),
        "x": _safe_float(actor.get("LocationX")),
        "y": _safe_float(actor.get("LocationY")),
        "z": _safe_float(actor.get("LocationZ")),
    }


def _has_map_location(point: Dict[str, Any]) -> bool:
    return point["x"] is not None and point["y"] is not None


def _point_type(point: Dict[str, Any]) -> Optional[str]:
    return "PalBox" if point.get("type") == "PalBox" else point.get("unit_type")


def _sample_evenly(items: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """先頭偏重を避け、同じ種別の座標を全体から等間隔に選ぶ。"""
    if count <= 0:
        return []
    if count >= len(items):
        return items.copy()
    if count == 1:
        return [items[len(items) // 2]]
    last = len(items) - 1
    return [items[round(index * last / (count - 1))] for index in range(count)]


def _select_map_points(points: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """種別を欠落させず、各種別内でも座標全体を代表する点を選ぶ。"""
    if limit <= 0:
        return []
    if len(points) <= limit:
        return points.copy()

    buckets = {
        actor_type: [point for point in points if _point_type(point) == actor_type]
        for actor_type in COUNT_TYPES
    }
    available = [actor_type for actor_type in COUNT_TYPES if buckets[actor_type]]
    if limit < len(available):
        return [_sample_evenly(buckets[actor_type], 1)[0] for actor_type in available[:limit]]

    quotas = {actor_type: 1 for actor_type in available}
    remaining = limit - len(available)
    while remaining > 0:
        candidates = [
            actor_type
            for actor_type in available
            if quotas[actor_type] < len(buckets[actor_type])
        ]
        if not candidates:
            break
        # 現在の抽出率が最も低い種別へ次の枠を割り当てる。
        actor_type = max(
            candidates,
            key=lambda candidate: len(buckets[candidate]) / quotas[candidate],
        )
        quotas[actor_type] += 1
        remaining -= 1

    selected: List[Dict[str, Any]] = []
    for actor_type in available:
        selected.extend(_sample_evenly(buckets[actor_type], quotas[actor_type]))
    return selected


def _is_low_hp(point: Dict[str, Any]) -> bool:
    hp = point["hp"]
    max_hp = point["max_hp"]
    return hp is not None and max_hp is not None and max_hp > 0 and 0 < hp and hp * 4 <= max_hp


def _is_hp_zero(point: Dict[str, Any]) -> bool:
    hp = point["hp"]
    return hp is not None and hp <= 0


def _guild_key(actor: Dict[str, Any]) -> Optional[Tuple[Optional[str], Optional[str]]]:
    guild_id = _safe_text(actor.get("GuildID"))
    guild_name = _safe_text(actor.get("GuildName"))
    if not guild_id and not guild_name:
        return None
    return guild_id, guild_name


def _new_guild(guild_id: Optional[str], guild_name: Optional[str]) -> Dict[str, Any]:
    return {
        "guild_id": guild_id,
        "guild_name": guild_name,
        "player": 0,
        "otomo": 0,
        "basecamp": 0,
        "palbox": 0,
    }


def _increment_guild(guild: Dict[str, Any], count_type: str) -> None:
    field = {
        "Player": "player",
        "OtomoPal": "otomo",
        "BaseCampPal": "basecamp",
        "PalBox": "palbox",
    }.get(count_type)
    if field is not None:
        guild[field] += 1


def aggregate_world_snapshot(payload: Any, point_limit: Any = DEFAULT_POINT_LIMIT) -> Dict[str, Any]:
    """公式 /game-data 応答を副作用なしで集計し、安全な表示用データを返す。"""
    limit = normalize_point_limit(point_limit)
    root = payload if isinstance(payload, dict) else {}
    actors = root.get("ActorData")
    if not isinstance(actors, list):
        actors = []

    counts = _empty_counts()
    guild_map: Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}
    guilds_truncated = False
    points: List[Dict[str, Any]] = []
    point_representatives: Dict[str, Dict[str, Any]] = {}
    total_points = 0
    point_sampler = random.Random(0)
    warnings: Dict[str, List[Dict[str, Any]]] = {
        "low_hp": [],
        "hp_zero": [],
        "inactive": [],
    }
    warning_totals = {kind: 0 for kind in warnings}

    for actor in actors:
        if not isinstance(actor, dict):
            continue

        actor_type = actor.get("Type")
        unit_type: Optional[str]
        if actor_type == "Character":
            candidate = actor.get("UnitType")
            if candidate not in CHARACTER_UNIT_TYPES:
                continue
            unit_type = candidate
            count_type = unit_type
        elif actor_type == "PalBox":
            unit_type = None
            count_type = "PalBox"
        else:
            continue

        counts[count_type] += 1
        point = _safe_point(actor, actor_type, unit_type)

        key = _guild_key(actor)
        if key is not None and count_type in ("Player", "OtomoPal", "BaseCampPal", "PalBox"):
            guild = guild_map.get(key)
            if guild is None:
                if len(guild_map) >= MAX_GUILDS:
                    guilds_truncated = True
                else:
                    guild = _new_guild(*key)
                    guild_map[key] = guild
            if guild is not None:
                _increment_guild(guild, count_type)

        if _has_map_location(point):
            total_points += 1
            point_type = _point_type(point)
            if point_type is not None:
                point_representatives.setdefault(point_type, point)
            if len(points) < MAX_POINT_LIMIT:
                points.append(point)
            else:
                replacement = point_sampler.randrange(total_points)
                if replacement < MAX_POINT_LIMIT:
                    points[replacement] = point
        if _is_low_hp(point):
            warning_totals["low_hp"] += 1
            if len(warnings["low_hp"]) < MAX_WARNINGS_PER_KIND:
                warnings["low_hp"].append(point.copy())
        if _is_hp_zero(point):
            warning_totals["hp_zero"] += 1
            if len(warnings["hp_zero"]) < MAX_WARNINGS_PER_KIND:
                warnings["hp_zero"].append(point.copy())
        if point["is_active"] is False:
            warning_totals["inactive"] += 1
            if len(warnings["inactive"]) < MAX_WARNINGS_PER_KIND:
                warnings["inactive"].append(point.copy())

    guild_rows = sorted(
        guild_map.values(),
        key=lambda item: (item["guild_name"] or "", item["guild_id"] or ""),
    )
    guilds = [
        {key: value for key, value in guild.items() if key != "guild_id"}
        for guild in guild_rows
    ]
    present_types = {_point_type(point) for point in points}
    point_candidates = points + [
        point
        for actor_type, point in point_representatives.items()
        if actor_type not in present_types
    ]
    selected_points = _select_map_points(point_candidates, limit)

    return {
        "supported": True,
        "Time": _safe_text(root.get("Time")),
        "FPS": _safe_float(root.get("FPS")),
        "AverageFPS": _safe_float(root.get("AverageFPS")),
        "counts": counts,
        "guilds": guilds,
        "returned_guilds": len(guilds),
        "guilds_truncated": guilds_truncated,
        "warnings": warnings,
        "warning_totals": warning_totals,
        "warnings_truncated": {
            kind: warning_totals[kind] > len(items)
            for kind, items in warnings.items()
        },
        "points": selected_points,
        "total_points": total_points,
        "returned_points": len(selected_points),
        "truncated": total_points > len(selected_points),
    }


def unsupported_world_snapshot(point_limit: Any = DEFAULT_POINT_LIMIT) -> Dict[str, Any]:
    """上流が /game-data 非対応（404）の場合の安定した応答を返す。"""
    result = aggregate_world_snapshot({}, point_limit)
    result["supported"] = False
    return result


def _lock_for_current_loop() -> asyncio.Lock:
    global _cache_lock, _cache_lock_loop
    loop = asyncio.get_running_loop()
    if _cache_lock is None or _cache_lock_loop is not loop:
        _cache_lock = asyncio.Lock()
        _cache_lock_loop = loop
    return _cache_lock


async def _load_cache_entry() -> Tuple[bool, Any]:
    try:
        return True, await pal.get_game_data()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return False, None
        raise


async def get_cached_game_data() -> Tuple[bool, Any]:
    """15秒 TTL のキャッシュを返す。同時更新は1リクエストにまとめる。"""
    global _cache_entry, _cache_deadline
    now = time.monotonic()
    if _cache_entry is not None and now < _cache_deadline:
        return _cache_entry

    async with _lock_for_current_loop():
        now = time.monotonic()
        if _cache_entry is not None and now < _cache_deadline:
            return _cache_entry
        entry = await _load_cache_entry()
        _cache_entry = entry
        _cache_deadline = time.monotonic() + CACHE_TTL_SECONDS
        return entry


async def get_world_snapshot(point_limit: Any = DEFAULT_POINT_LIMIT) -> Dict[str, Any]:
    """キャッシュ済みの全 Actor から、指定件数の安全な表示用応答を作る。"""
    supported, payload = await get_cached_game_data()
    if not supported:
        return unsupported_world_snapshot(point_limit)
    if not isinstance(payload, dict) or not isinstance(payload.get("ActorData"), list):
        raise ValueError("game-data 応答に ActorData 配列がありません")
    return aggregate_world_snapshot(payload, point_limit)


def reset_snapshot_cache() -> None:
    """テストや再初期化時にキャッシュ状態を破棄する。"""
    global _cache_entry, _cache_deadline, _cache_lock, _cache_lock_loop
    _cache_entry = None
    _cache_deadline = 0.0
    _cache_lock = None
    _cache_lock_loop = None

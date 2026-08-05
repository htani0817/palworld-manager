# -*- coding: utf-8 -*-
"""
ワールド Actor スナップショット集計のテスト。

実行:
    cd backend
    python -m pytest tests/test_world_snapshot.py -v
または:
    python tests/test_world_snapshot.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import world_snapshot as ws


def _character(unit_type, *, guild_id="g1", guild_name="Guild One", **changes):
    actor = {
        "Type": "Character",
        "UnitType": unit_type,
        "NickName": unit_type + "-name",
        "GuildID": guild_id,
        "GuildName": guild_name,
        "level": 10,
        "HP": 100,
        "MaxHP": 100,
        "IsActive": "true",
        "LocationX": 1.0,
        "LocationY": 2.0,
        "LocationZ": 3.0,
        "InstanceID": "must-not-leak-instance",
        "TrainerInstanceID": "must-not-leak-trainer",
        "userid": "must-not-leak-user",
        "ip": "203.0.113.10",
    }
    actor.update(changes)
    return actor


def _payload(actors):
    return {
        "Time": "2026-07-22 12:34:56",
        "FPS": 59.5,
        "AverageFPS": 55,
        "ActorData": actors,
    }


def test_aggregate_counts_guilds_and_warnings():
    actors = [
        _character("Player"),
        _character("OtomoPal", HP=20),
        _character("BaseCampPal", HP=0),
        _character("WildPal", guild_id=None, guild_name=None, IsActive="false"),
        _character("NPC", guild_id=None, guild_name=None),
        {
            "Type": "PalBox",
            "GuildID": "g1",
            "GuildName": "Guild One",
            "LocationX": 10,
            "LocationY": 20,
            "LocationZ": 30,
        },
    ]
    view = ws.aggregate_world_snapshot(_payload(actors))

    assert view["Time"] == "2026-07-22 12:34:56"
    assert view["FPS"] == 59.5
    assert view["AverageFPS"] == 55.0
    assert view["counts"] == {
        "Player": 1,
        "OtomoPal": 1,
        "BaseCampPal": 1,
        "WildPal": 1,
        "NPC": 1,
        "PalBox": 1,
    }
    assert view["guilds"] == [{
        "guild_name": "Guild One",
        "player": 1,
        "otomo": 1,
        "basecamp": 1,
        "palbox": 1,
    }]
    assert len(view["warnings"]["low_hp"]) == 1
    assert len(view["warnings"]["hp_zero"]) == 1
    assert len(view["warnings"]["inactive"]) == 1
    assert len(view["points"]) == 6


def test_points_and_warnings_mask_sensitive_fields():
    secrets = (
        "must-not-leak-instance",
        "must-not-leak-trainer",
        "must-not-leak-user",
        "203.0.113.10",
    )
    view = ws.aggregate_world_snapshot(
        _payload([_character("Player", HP=0, IsActive="false")])
    )
    allowed = {
        "type", "unit_type", "nickname", "guild_name", "level",
        "hp", "max_hp", "is_active", "x", "y", "z",
    }
    assert set(view["points"][0]) == allowed
    assert set(view["warnings"]["hp_zero"][0]) == allowed
    encoded = json.dumps(view, ensure_ascii=False)
    for secret in secrets:
        assert secret not in encoded
    for forbidden_key in ("InstanceID", "TrainerInstanceID", "userid", "ip"):
        assert forbidden_key not in encoded
    assert "guild_id" not in encoded


def test_broken_actor_and_field_types_do_not_break_snapshot():
    actors = [
        None,
        "not-an-actor",
        {"Type": "Character", "UnitType": ["Player"]},
        {"Type": "Unknown", "UnitType": "Player"},
        _character(
            "Player",
            level={"bad": True},
            HP="not-a-number",
            MaxHP=float("inf"),
            IsActive={"bad": True},
            LocationX=float("nan"),
        ),
    ]
    view = ws.aggregate_world_snapshot({
        "Time": 123,
        "FPS": "bad",
        "AverageFPS": float("inf"),
        "ActorData": actors,
    })

    assert view["counts"]["Player"] == 1
    assert view["Time"] is None
    assert view["FPS"] is None
    assert view["AverageFPS"] is None
    assert view["points"] == []
    assert view["warnings"] == {"low_hp": [], "hp_zero": [], "inactive": []}
    json.dumps(view, allow_nan=False)


def test_xy_location_does_not_require_optional_z_coordinate():
    view = ws.aggregate_world_snapshot(
        _payload([_character("Player", LocationZ=None)])
    )
    assert len(view["points"]) == 1
    assert view["points"][0]["z"] is None


def test_warning_payload_is_bounded_but_totals_remain_accurate():
    count = ws.MAX_WARNINGS_PER_KIND + 25
    actors = [
        _character("WildPal", guild_id=None, guild_name=None, IsActive=False)
        for _ in range(count)
    ]
    view = ws.aggregate_world_snapshot(_payload(actors))
    assert len(view["warnings"]["inactive"]) == ws.MAX_WARNINGS_PER_KIND
    assert view["warning_totals"]["inactive"] == count
    assert view["warnings_truncated"]["inactive"] is True


def test_guild_payload_and_display_text_are_bounded():
    actors = [
        _character(
            "Player",
            guild_id=f"guild-{index}",
            guild_name=("G" * (ws.MAX_TEXT_LENGTH + 50)) + str(index),
            NickName="N" * (ws.MAX_TEXT_LENGTH + 50),
        )
        for index in range(ws.MAX_GUILDS + 1)
    ]
    view = ws.aggregate_world_snapshot(_payload(actors), point_limit=1)

    assert len(view["guilds"]) == ws.MAX_GUILDS
    assert view["returned_guilds"] == ws.MAX_GUILDS
    assert view["guilds_truncated"] is True
    assert len(view["guilds"][0]["guild_name"]) == ws.MAX_TEXT_LENGTH
    assert len(view["points"][0]["nickname"]) == ws.MAX_TEXT_LENGTH


def test_point_limit_is_clamped_and_does_not_change_aggregate_counts():
    actors = [_character("WildPal", guild_id=None, guild_name=None, LocationX=i)
              for i in range(5)]
    view = ws.aggregate_world_snapshot(_payload(actors), point_limit=2)
    assert view["counts"]["WildPal"] == 5
    assert view["total_points"] == 5
    assert view["returned_points"] == 2
    assert view["truncated"] is True
    assert len(view["points"]) == 2
    assert ws.normalize_point_limit(-1) == 0
    assert ws.normalize_point_limit(10001) == ws.MAX_POINT_LIMIT
    assert ws.normalize_point_limit("broken") == ws.DEFAULT_POINT_LIMIT


def test_point_limit_keeps_each_actor_type_available_for_filtering():
    actors = []
    for unit_type in ws.CHARACTER_UNIT_TYPES:
        actors.extend(
            _character(unit_type, guild_id=None, guild_name=None, LocationX=index)
            for index in range(20)
        )
    actors.extend(
        {
            "Type": "PalBox",
            "LocationX": index,
            "LocationY": index,
            "LocationZ": 0,
        }
        for index in range(20)
    )

    view = ws.aggregate_world_snapshot(_payload(actors), point_limit=len(ws.COUNT_TYPES))
    point_types = {
        "PalBox" if point["type"] == "PalBox" else point["unit_type"]
        for point in view["points"]
    }
    assert point_types == set(ws.COUNT_TYPES)


def test_point_collection_is_bounded_before_response_projection():
    count = ws.MAX_POINT_LIMIT + 25
    actors = [
        _character("WildPal", guild_id=None, guild_name=None, LocationX=index)
        for index in range(count)
    ]
    view = ws.aggregate_world_snapshot(_payload(actors), point_limit=ws.MAX_POINT_LIMIT)
    assert view["total_points"] == count
    assert view["returned_points"] == ws.MAX_POINT_LIMIT
    assert len(view["points"]) == ws.MAX_POINT_LIMIT
    assert view["truncated"] is True


def test_is_active_string_and_bool_normalization():
    assert ws.normalize_is_active("true") is True
    assert ws.normalize_is_active(" TRUE ") is True
    assert ws.normalize_is_active("false") is False
    assert ws.normalize_is_active("False") is False
    assert ws.normalize_is_active(True) is True
    assert ws.normalize_is_active(False) is False
    assert ws.normalize_is_active(0) is None
    assert ws.normalize_is_active("unknown") is None

    actors = [
        _character("Player", IsActive="false"),
        _character("OtomoPal", IsActive=False),
        _character("BaseCampPal", IsActive="true"),
    ]
    view = ws.aggregate_world_snapshot(_payload(actors))
    assert [point["is_active"] for point in view["points"]] == [False, False, True]
    assert len(view["warnings"]["inactive"]) == 2


def test_async_cache_prevents_request_stampede():
    original = ws.pal.get_game_data
    calls = {"count": 0}
    assert ws.CACHE_TTL_SECONDS == 15.0

    async def fake_game_data():
        calls["count"] += 1
        await asyncio.sleep(0.01)
        return _payload([_character("Player")])

    async def run():
        ws.reset_snapshot_cache()
        ws.pal.get_game_data = fake_game_data
        try:
            results = await asyncio.gather(*(ws.get_world_snapshot() for _ in range(20)))
            assert all(result["counts"]["Player"] == 1 for result in results)
            assert calls["count"] == 1
            await ws.get_world_snapshot()
            assert calls["count"] == 1, "TTL 内なのに上流を再取得した"
            ws._cache_deadline = 0.0
            await ws.get_world_snapshot()
            assert calls["count"] == 2, "TTL 失効後に上流を再取得していない"
        finally:
            ws.pal.get_game_data = original
            ws.reset_snapshot_cache()

    asyncio.run(run())
    assert calls["count"] == 2


def test_upstream_404_returns_supported_false():
    original = ws.pal.get_game_data

    async def not_found():
        request = httpx.Request("GET", "http://pal.invalid/game-data")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    async def run():
        ws.reset_snapshot_cache()
        ws.pal.get_game_data = not_found
        try:
            first, second = await asyncio.gather(ws.get_world_snapshot(), ws.get_world_snapshot())
            assert first["supported"] is False
            assert second["supported"] is False
            assert first["counts"] == ws._empty_counts()
        finally:
            ws.pal.get_game_data = original
            ws.reset_snapshot_cache()

    asyncio.run(run())


def test_missing_actor_data_is_rejected_instead_of_reported_as_empty_world():
    original = ws.pal.get_game_data

    async def malformed():
        return {"FPS": 60}

    async def run():
        ws.reset_snapshot_cache()
        ws.pal.get_game_data = malformed
        try:
            try:
                await ws.get_world_snapshot()
            except ValueError as error:
                assert "ActorData" in str(error)
            else:
                raise AssertionError("必須 ActorData の欠落を受理しました")
        finally:
            ws.pal.get_game_data = original
            ws.reset_snapshot_cache()

    asyncio.run(run())


def test_router_maps_connection_failure_to_http_error():
    from fastapi import HTTPException
    from routers import world as world_router

    original = world_router.snapshot_service.get_world_snapshot

    async def connection_failure(_limit):
        request = httpx.Request("GET", "http://pal.invalid/game-data")
        raise httpx.ConnectError("connection refused", request=request)

    world_router.snapshot_service.get_world_snapshot = connection_failure
    try:
        try:
            asyncio.run(world_router.snapshot(10))
            assert False, "接続失敗が HTTPException になっていない"
        except HTTPException as error:
            assert error.status_code == 502
            assert "接続できません" in error.detail
    finally:
        world_router.snapshot_service.get_world_snapshot = original


def _run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception as error:
            print(f" FAIL {test.__name__}: {error}")
            failed.append(test.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())

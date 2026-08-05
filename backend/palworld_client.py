import json
from typing import Optional

import httpx

from config import settings


class PalInvalidResponseError(Exception):
    """Palworld API が HTTP 200 だが JSON として解釈できないレスポンスを返した場合。"""

# アプリ全体で1つの AsyncClient を共有し、コネクションを再利用する。
# lifespan（main.py の startup / shutdown）で生成・破棄する。
_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.pal_base_url,
            auth=(("admin", settings.pal_admin_password)),
            timeout=httpx.Timeout(10.0, read=15.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        # lifespan 未経由（テスト等）でも動くよう遅延生成する
        init_client()
    assert _client is not None
    return _client


def _parse_json(r: httpx.Response) -> dict:
    """レスポンスの JSON をパースし、失敗時は PalInvalidResponseError を送出する。"""
    try:
        return r.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PalInvalidResponseError(
            f"Palworld API が解釈できないレスポンスを返しました: {e}"
        ) from e


async def get_info() -> dict:
    r = await _get_client().get("/info")
    r.raise_for_status()
    return _parse_json(r)


async def get_players() -> dict:
    r = await _get_client().get("/players")
    r.raise_for_status()
    return _parse_json(r)


async def get_settings_data() -> dict:
    r = await _get_client().get("/settings")
    r.raise_for_status()
    return _parse_json(r)


async def get_metrics() -> dict:
    r = await _get_client().get("/metrics")
    r.raise_for_status()
    return _parse_json(r)


async def get_game_data() -> dict:
    r = await _get_client().get("/game-data", timeout=30)
    r.raise_for_status()
    return _parse_json(r)


async def post_announce(message: str) -> None:
    r = await _get_client().post("/announce", json={"message": message})
    r.raise_for_status()


async def post_kick(user_id: str, message: str = "") -> None:
    r = await _get_client().post("/kick", json={"userid": user_id, "message": message})
    r.raise_for_status()


async def post_ban(user_id: str, message: str = "") -> None:
    r = await _get_client().post("/ban", json={"userid": user_id, "message": message})
    r.raise_for_status()


async def post_unban(user_id: str) -> None:
    # 公式 1.0 の /unban 必須キーは userid（steamid ではない）
    r = await _get_client().post("/unban", json={"userid": user_id})
    r.raise_for_status()


async def post_save() -> None:
    r = await _get_client().post("/save", timeout=30)
    r.raise_for_status()


async def post_shutdown(wait_time: int = 60, message: str = "") -> None:
    r = await _get_client().post("/shutdown", json={"waittime": wait_time, "message": message})
    r.raise_for_status()


async def post_stop() -> None:
    r = await _get_client().post("/stop")
    r.raise_for_status()

# -*- coding: utf-8 -*-
"""安全に整形したワールドスナップショット API。"""

import httpx
from fastapi import APIRouter, HTTPException, Query

import world_snapshot as snapshot_service


router = APIRouter(prefix="/api/world", tags=["world"])


@router.get("/snapshot")
async def snapshot(
    limit: int = Query(
        snapshot_service.DEFAULT_POINT_LIMIT,
        ge=0,
        le=snapshot_service.MAX_POINT_LIMIT,
        description="返すマップ point の最大件数",
    ),
):
    """全 Actor の集計と、識別情報を除いた簡易マップ point を返す。"""
    try:
        return await snapshot_service.get_world_snapshot(limit)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return snapshot_service.unsupported_world_snapshot(limit)
        raise HTTPException(
            status_code=502,
            detail=f"Palworld game-data API エラー: HTTP {error.response.status_code}",
        ) from error
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504,
            detail="Palworld game-data API がタイムアウトしました",
        ) from error
    except (httpx.RequestError, OSError) as error:
        raise HTTPException(
            status_code=502,
            detail="Palworld game-data API に接続できません",
        ) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="Palworld game-data API の応答を解釈できません",
        ) from error

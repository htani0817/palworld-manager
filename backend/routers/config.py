from fastapi import APIRouter
from config import settings

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/env")
async def env_info():
    """フロントエンドが環境識別バナーを描画するための情報を返す"""
    return {
        "env": settings.env,
        "label": settings.env_label,
        "color": settings.env_color,
        "is_production": settings.is_production,
        "pal_host": settings.pal_host,
        "pal_port": settings.pal_port,
    }

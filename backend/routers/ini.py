# -*- coding: utf-8 -*-
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import palworld_client as pal
from ini_editor import mask_secrets, read_ini, secret_key_names, write_ini
from operation_guard import exclusive_mutation

logger = logging.getLogger("palworld_manager.ini")

router = APIRouter(prefix="/api/ini", tags=["ini"], dependencies=[Depends(exclusive_mutation)])


async def _rest_fallback() -> dict:
    """REST API の稼働値を読み取り専用で返す（フォールバック共通処理）"""
    try:
        rest = await pal.get_settings_data()
    except Exception:
        rest = {}
    return {
        "values": mask_secrets(rest) if isinstance(rest, dict) else {},
        "source": "rest",
        "readonly": True,
        "secret_keys": secret_key_names(),
    }


@router.get("/settings")
async def get_ini_settings():
    """PalWorldSettings.ini の OptionSettings を返す。

    秘密キー（AdminPassword / ServerPassword）はマスクする。
    INI から読めない場合は REST API の稼働値に読み取り専用でフォールバックする。
    レスポンスは常に {values, source, readonly, secret_keys} の形。
    """
    try:
        raw = read_ini()
    except Exception as e:
        logger.warning("INI 読込に失敗したため REST にフォールバックします: %s", e)
        return await _rest_fallback()

    if raw:
        return {
            "values": mask_secrets(raw),
            "source": "ini",
            "readonly": False,
            "secret_keys": secret_key_names(),
        }
    return await _rest_fallback()


class IniPatchRequest(BaseModel):
    values: dict[str, str]


@router.patch("/settings")
async def patch_ini_settings(req: IniPatchRequest):
    """指定したキーだけ上書きして保存（バックアップ自動作成）"""
    if not req.values:
        raise HTTPException(status_code=400, detail="values が空です")
    try:
        write_ini(req.values)
        return {"result": "ok", "updated": list(req.values.keys())}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -*- coding: utf-8 -*-
"""REST管理コマンド・設定保存もバックアップ中の排他に参加させる。"""

from fastapi import HTTPException, Request

import scheduler


async def exclusive_mutation(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        yield
        return
    if scheduler.maintenance_operation_lock.locked() or scheduler.manual_maintenance_pending():
        raise HTTPException(409, "メンテナンスまたはバックアップ操作が実行中です")
    async with scheduler.maintenance_operation_lock:
        if scheduler.manual_maintenance_pending() or await scheduler.is_update_in_progress():
            raise HTTPException(409, "サーバー更新中か、更新状態を確認できません")
        yield

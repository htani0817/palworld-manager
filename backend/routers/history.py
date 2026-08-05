# -*- coding: utf-8 -*-
"""履歴参照 API。"""

from fastapi import APIRouter, Query

import history_store

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/metrics")
def metrics(
    hours: float = Query(default=24, gt=0, allow_inf_nan=False),
    limit: int = Query(default=500, ge=1, le=history_store.MAX_QUERY_LIMIT),
):
    points = history_store.query_metrics(hours=hours, limit=limit)
    return {
        "hours": hours,
        "count": len(points),
        "metrics": points,
    }


@router.get("/sessions")
def sessions(
    limit: int = Query(default=100, ge=1, le=history_store.MAX_QUERY_LIMIT),
    active_only: bool = Query(default=False),
):
    items = []
    for session in history_store.query_sessions(limit=limit, active_only=active_only):
        public_session = dict(session)
        public_session.pop("user_id", None)
        items.append(public_session)
    return {
        "count": len(items),
        "active_only": active_only,
        "sessions": items,
    }


@router.get("/summary")
def summary(hours: float = Query(default=24, gt=0, allow_inf_nan=False)):
    return history_store.get_summary(hours=hours)

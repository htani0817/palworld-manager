# -*- coding: utf-8 -*-
"""ランキング参照・CSV 出力 API。"""

import csv
import io
from typing import Literal

from fastapi import APIRouter, Query, Response

import ranking_tracker

router = APIRouter(prefix="/api/ranking", tags=["ranking"])

RankingMetric = Literal["total_seconds", "max_level", "login_days"]

CSV_HEADERS = (
    "順位",
    "名前",
    "アカウント",
    "累計プレイ時間（秒）",
    "最高Lv",
    "ログイン日数",
    "最終ログイン（ISO 8601）",
    "状態",
)


def _safe_csv_cell(value):
    """Excel が数式として解釈しうる文字列を、文字列セルとして固定する。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value

    # 表計算ソフトは先頭の空白を無視し、ロケールによっては全角記号も
    # 数式の開始文字として扱う場合がある。
    stripped = value.lstrip()
    formula_prefixes = ("=", "+", "-", "@", "＝", "＋", "－", "＠")
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(formula_prefixes):
        return "'" + value
    return value


def _ranking_sort_key(player: dict, metric: RankingMetric) -> tuple:
    """選択指標、累計時間の降順と userId の昇順で決定的に並べる。"""
    return (
        -(player.get(metric) or 0),
        -(player.get("total_seconds") or 0),
        str(player.get("userId") or ""),
        str(player.get("name") or ""),
        str(player.get("accountName") or ""),
    )


def _build_csv(view: dict, metric: RankingMetric) -> bytes:
    """ランキングの読み取りスナップショットから BOM 付き CSV を組み立てる。"""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)

    players = sorted(
        view.get("players", ()),
        key=lambda player: _ranking_sort_key(player, metric),
    )
    for rank, player in enumerate(players, start=1):
        row = (
            rank,
            player.get("name") or "(不明)",
            player.get("accountName") or "",
            player.get("total_seconds") or 0,
            player.get("max_level") or 0,
            player.get("login_days") or 0,
            player.get("last_seen") or "",
            "オンライン" if player.get("online") else "オフライン",
        )
        writer.writerow(_safe_csv_cell(value) for value in row)

    return output.getvalue().encode("utf-8-sig")


@router.get("/")
async def ranking():
    """収集済みランキング（累計プレイ時間・最高レベル・ログイン日数）を返す"""
    return ranking_tracker.get_ranking_view()


@router.get("/export.csv", response_class=Response)
async def export_ranking_csv(
    metric: RankingMetric = Query(
        ...,
        description="順位付けに使う指標",
    ),
) -> Response:
    """収集済みスナップショットを、選択指標順の CSV として返す。"""
    view = ranking_tracker.get_ranking_view()
    payload = _build_csv(view, metric)
    return Response(
        content=payload,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="ranking-{metric}.csv"',
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )

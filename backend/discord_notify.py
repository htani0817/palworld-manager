"""
Discord Webhook 通知
メモリ使用率を定期監視し、しきい値超過時に Discord へ embed 形式で通知する。

- 80% 以上: 🟡 Warning（継続監視）
- 90% 以上: 🔴 Critical（再起動を推奨）
- しきい値を下回ったら ✅ 回復通知

ヒステリシス: 一度 Warning/Critical に入ると、回復判定はしきい値より低い
RECOVER_MARGIN 分だけ下がるまで発火しない（80/90 付近の往復で毎分通知するのを防ぐ）。

Webhook URL は環境変数 DISCORD_WEBHOOK_URL で指定する（コンソール登録方式）。
状態（_state, _last_sent）はモジュールレベルで持つため、uvicorn 単一ワーカー前提。
複数ワーカーにする場合は状態を外部化すること（README に明記）。
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings
from system_metrics import get_system_metrics

logger = logging.getLogger("palworld_manager.discord")

WARNING_PERCENT = 80.0
CRITICAL_PERCENT = 90.0
RECOVER_MARGIN = 5.0     # ヒステリシス幅（この分だけ下回るまで回復扱いにしない）
REPEAT_MINUTES = 30      # 同一状態が続く場合の再通知間隔

# 監視状態（プロセス内で保持）
_state: str = "normal"  # normal | warning | critical
_last_sent: Optional[datetime] = None
_last_success_at: Optional[datetime] = None
_last_failure_at: Optional[datetime] = None

_COLORS = {
    "warning": 0xF5A623,    # 黄
    "critical": 0xF04D4D,   # 赤
    "recovered": 0x3FB950,  # 緑
}


def is_enabled() -> bool:
    """メモリ監視 Webhook が有効かどうか"""
    return bool(settings.discord_webhook_url)


def is_lifecycle_enabled() -> bool:
    """起動/停止通知 Webhook が有効かどうか"""
    return bool(settings.discord_lifecycle_webhook_url)


def _webhook_url() -> str:
    # wait=true を付けると Discord がメッセージ保存を確認してから応答する
    url = settings.discord_webhook_url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wait=true"


def _lifecycle_webhook_url() -> str:
    url = settings.discord_lifecycle_webhook_url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wait=true"


def _bar(percent: float) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def _build_embed(kind: str, metrics: dict) -> dict:
    sys_m = metrics["system"]
    pal_m = metrics["palworld"]
    mem_pct = sys_m["mem_percent"]

    titles = {
        "warning": "🟡 メモリ使用率 Warning",
        "critical": "🔴 メモリ使用率 Critical",
        "recovered": "✅ メモリ使用率が正常に戻りました",
    }
    descriptions = {
        "warning": f"メモリ使用率が **{WARNING_PERCENT:.0f}%** を超えました。継続監視してください。",
        "critical": f"メモリ使用率が **{CRITICAL_PERCENT:.0f}%** を超えました。**サーバーの再起動を推奨します。**",
        "recovered": "しきい値を下回りました。",
    }

    if pal_m["running"] and pal_m["mem_mb"] is not None:
        pal_text = f"{pal_m['mem_mb'] / 1024:.1f} GB (PID {pal_m['pid']})"
    else:
        pal_text = "停止中"

    return {
        "title": titles[kind],
        "description": descriptions[kind],
        "color": _COLORS[kind],
        "fields": [
            {"name": "メモリ使用率", "value": f"`{_bar(mem_pct)}` **{mem_pct:.1f}%**", "inline": False},
            {"name": "使用量", "value": f"{sys_m['mem_used_gb']} / {sys_m['mem_total_gb']} GB", "inline": True},
            {"name": "Palworld プロセス", "value": pal_text, "inline": True},
            {"name": "CPU", "value": f"{sys_m['cpu_percent']:.1f}%", "inline": True},
        ],
        "footer": {"text": f"Palworld Manager {settings.env_label}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class DiscordSendError(Exception):
    """Discord 送信失敗。トークンを含まない安全なメッセージのみ持つ"""


async def _send(embed: dict) -> None:
    """Discord に送信（メモリ監視用）。失敗時は DiscordSendError を投げる。"""
    global _last_success_at, _last_failure_at
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                _webhook_url(),
                json={"username": "Palworld Manager", "embeds": [embed]},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        _last_failure_at = datetime.now(timezone.utc)
        logger.warning("Discord 通知が失敗しました: HTTP %s", e.response.status_code)
        raise DiscordSendError(f"Discord からエラー応答（HTTP {e.response.status_code}）") from None
    except httpx.RequestError:
        _last_failure_at = datetime.now(timezone.utc)
        logger.warning("Discord 通知の送信に失敗しました（接続エラー）")
        raise DiscordSendError("Discord への接続に失敗しました") from None
    _last_success_at = datetime.now(timezone.utc)


async def _send_lifecycle(embed: dict) -> None:
    """Discord に送信（起動/停止通知用）。失敗時は DiscordSendError を投げる。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                _lifecycle_webhook_url(),
                json={"username": "Palworld Manager", "embeds": [embed]},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.warning("Discord ライフサイクル通知が失敗しました: HTTP %s", e.response.status_code)
        raise DiscordSendError(f"Discord からエラー応答（HTTP {e.response.status_code}）") from None
    except httpx.RequestError:
        logger.warning("Discord ライフサイクル通知の送信に失敗しました（接続エラー）")
        raise DiscordSendError("Discord への接続に失敗しました") from None


async def check_memory_and_notify() -> None:
    """定期実行: メモリ使用率を確認し、状態遷移があれば Discord に通知する"""
    global _state, _last_sent
    if not is_enabled():
        return

    try:
        metrics = await get_system_metrics()
    except Exception:
        return

    mem_pct = metrics["system"]["mem_percent"]

    # ヒステリシス付きの状態判定
    if _state == "critical":
        new_state = "critical" if mem_pct >= (CRITICAL_PERCENT - RECOVER_MARGIN) else (
            "warning" if mem_pct >= (WARNING_PERCENT - RECOVER_MARGIN) else "normal"
        )
    elif _state == "warning":
        if mem_pct >= CRITICAL_PERCENT:
            new_state = "critical"
        elif mem_pct >= (WARNING_PERCENT - RECOVER_MARGIN):
            new_state = "warning"
        else:
            new_state = "normal"
    else:  # normal
        new_state = "critical" if mem_pct >= CRITICAL_PERCENT else (
            "warning" if mem_pct >= WARNING_PERCENT else "normal"
        )

    now = datetime.now(timezone.utc)
    try:
        if new_state != _state:
            if new_state == "normal":
                await _send(_build_embed("recovered", metrics))
            else:
                await _send(_build_embed(new_state, metrics))
            _state = new_state
            _last_sent = now
        elif new_state in ("warning", "critical"):
            if _last_sent is None or (now - _last_sent).total_seconds() >= REPEAT_MINUTES * 60:
                await _send(_build_embed(new_state, metrics))
                _last_sent = now
    except Exception:
        # 通知失敗で監視自体を止めない（_send がログ済み。次回の実行で再試行される）
        pass


async def send_test() -> None:
    """テスト通知を送信する（設定確認用）。失敗は例外を投げる"""
    if not is_enabled():
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    metrics = await get_system_metrics()
    embed = _build_embed("recovered", metrics)
    embed["title"] = "🔔 テスト通知"
    embed["description"] = "Discord Webhook の設定は正常です。"
    await _send(embed)


_palserver_state: Optional[bool] = None  # True=起動中, False=停止中, None=未取得


def _build_lifecycle_embed(title: str, color: int) -> dict:
    return {
        "title": title,
        "color": color,
        "footer": {"text": "Palworld Manager"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def check_palserver_and_notify() -> None:
    """定期実行: palserver の起動・停止を検知して Discord に通知する"""
    global _palserver_state
    if not is_lifecycle_enabled():
        return

    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", settings.pal_service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        running = out.decode().strip() == "active"
    except Exception:
        return  # 取得失敗は無視（次回再試行）

    if _palserver_state == running:
        return  # 状態変化なし

    prev = _palserver_state
    _palserver_state = running

    if prev is None:
        return  # 初回取得時は通知しない（起動前の状態が不明なため）

    if running:
        embed = _build_lifecycle_embed(f"🟢 {settings.pal_service_name} が起動しました", 0x3FB950)
    else:
        embed = _build_lifecycle_embed(f"🔴 {settings.pal_service_name} が停止しました", 0xF04D4D)

    try:
        await _send_lifecycle(embed)
    except Exception:
        pass

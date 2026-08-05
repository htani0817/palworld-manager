import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from config import settings


async def log_stream(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("[接続成功] ログストリームに接続しました")

    # journalctl でサービスのログをフォロー
    proc = await asyncio.create_subprocess_exec(
        "journalctl",
        "-f",
        "-u", settings.pal_service_name,
        "-u", "palworld-update",
        "--output=cat",
        "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            await websocket.send_text(line.decode("utf-8", errors="replace").rstrip())
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass

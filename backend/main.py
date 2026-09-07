# -*- coding: utf-8 -*-
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import palworld_client
import history_store
import ranking_tracker
import scheduler as scheduler_service
import shutdown_scheduler
import system_metrics
from config import settings
from discord_notify import (
    check_memory_and_notify,
    check_palserver_and_notify,
    is_enabled as discord_enabled,
    is_lifecycle_enabled,
)
from logging_config import configure_logging
from maintenance import coordinator as maintenance_coordinator
from routers import config as config_router
from routers import history as history_router
from routers import ini as ini_router
from routers import maintenance as maintenance_router
from routers import ranking as ranking_router
from routers import schedule as schedule_router
from routers import server as server_router
from routers import shutdown_schedule as shutdown_schedule_router
from routers import system as system_router
from routers import world as world_router
from routers import workspace as workspace_router
from scheduler import restore_schedules, scheduler
from sensitive_requests import sensitive_request_validation_exception_handler
from websocket_log import log_stream

logger = logging.getLogger("palworld_manager")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    palworld_client.init_client()
    history_store.init_db()
    system_metrics.start_sampler()
    scheduler.start()
    restore_schedules()
    workspace_router.restore_schedule()
    shutdown_scheduler.restore_shutdown_schedule()
    ranking_tracker.load_data()
    scheduler.add_job(
        ranking_tracker.sample, "interval",
        seconds=ranking_tracker.SAMPLE_INTERVAL_SECONDS,
        id="ranking-sampler", replace_existing=True,
        coalesce=True, max_instances=1,
    )
    logger.info("プレイヤーランキング収集を有効化しました（%d秒間隔）",
                ranking_tracker.SAMPLE_INTERVAL_SECONDS)
    scheduler.add_job(
        history_store.sample, "interval",
        seconds=history_store.SAMPLE_INTERVAL_SECONDS,
        id="history-sampler", replace_existing=True,
        coalesce=True, max_instances=1,
    )
    logger.info("サーバー履歴収集を有効化しました（%d秒間隔）",
                history_store.SAMPLE_INTERVAL_SECONDS)
    if discord_enabled():
        scheduler.add_job(
            check_memory_and_notify, "interval", seconds=60,
            id="discord-memory-monitor", replace_existing=True,
        )
        logger.info("Discord メモリ監視を有効化しました（単一ワーカー前提）")
    else:
        logger.info("DISCORD_WEBHOOK_URL 未設定のため Discord メモリ監視は無効です")
    if is_lifecycle_enabled():
        scheduler.add_job(
            check_palserver_and_notify, "interval", seconds=10,
            id="discord-palserver-monitor", replace_existing=True,
        )
        logger.info("Discord palserver 起動/停止監視を有効化しました（10秒間隔）")
    else:
        logger.info("DISCORD_LIFECYCLE_WEBHOOK_URL 未設定のため palserver 起動/停止通知は無効です")
    try:
        yield
    finally:
        await workspace_router.shutdown()
        await maintenance_coordinator.shutdown()
        await scheduler_service.shutdown_active_restarts()
        scheduler.shutdown(wait=False)
        system_metrics.stop_sampler()
        await palworld_client.close_client()


app = FastAPI(title="Palworld Server Manager", version="1.4.0", lifespan=lifespan)
app.add_exception_handler(
    RequestValidationError,
    sensitive_request_validation_exception_handler,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(server_router.router)
app.include_router(config_router.router)
app.include_router(system_router.router)
app.include_router(ini_router.router)
app.include_router(schedule_router.router)
app.include_router(shutdown_schedule_router.router)
app.include_router(ranking_router.router)
app.include_router(world_router.router)
app.include_router(history_router.router)
app.include_router(maintenance_router.router)
app.include_router(workspace_router.router)


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await log_stream(websocket)


# マップ画像などの静的アセット配信（check_dir=False で assets/ 未作成でも起動できる）
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "assets", check_dir=False), name="static")
app.mount("/ui", StaticFiles(directory=FRONTEND_DIR / "ui"), name="ui")


@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.app_port,
        reload=False,
        log_config=None,
    )

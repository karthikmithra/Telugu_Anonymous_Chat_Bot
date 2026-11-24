"""
main.py - FastAPI Application Entry Point, Lifespan Handler, Webhook & Uptime Keep-Alive.
Features:
- Fast startup with Async SQLAlchemy engine initialization
- UptimeRobot keep-alive endpoint: GET /health
- Production Telegram Webhook (POST /webhook) with strict X-Telegram-Bot-Api-Secret-Token verification
- Automatic fallback to Telegram Long Polling when WEBHOOK_URL is blank (ideal for local development)
- Full Admin Console mounted under /admin
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# Load environment variables from .env
load_dotenv()

from admin import admin_router
from bot import setup_dispatcher
from database import engine, init_db, utcnow
from security import verify_webhook_secret

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")

# Configuration from environment
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

START_TIME = utcnow()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages application startup and graceful shutdown:
    1. Creates database tables if needed.
    2. Initializes Aiogram Bot & Dispatcher.
    3. If WEBHOOK_URL is set: registers webhook with secret token.
       If WEBHOOK_URL is empty: automatically launches background long polling.
    4. Cleans up bot session, polling tasks, and database connections on shutdown.
    """
    logger.info("Initializing AnonChat Application...")

    # 1. Initialize Database Tables
    await init_db()
    logger.info("Database schema verified and initialized.")

    # 2. Check Bot Token
    if not BOT_TOKEN:
        logger.warning(
            "BOT_TOKEN is not configured! Please provide a valid Telegram Bot token in .env."
        )

    bot = Bot(
        token=BOT_TOKEN or "dummy:token",
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp: Dispatcher = setup_dispatcher()

    app.state.bot = bot
    app.state.dp = dp
    app.state.polling_task = None

    # 3. Webhook vs Long Polling Selection
    if BOT_TOKEN and BOT_TOKEN != "dummy:token":
        if WEBHOOK_URL:
            webhook_endpoint = f"{WEBHOOK_URL}/webhook"
            logger.info(f"Setting Telegram Webhook to: {webhook_endpoint}")
            try:
                await bot.set_webhook(
                    url=webhook_endpoint,
                    secret_token=WEBHOOK_SECRET or None,
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"],
                )
                logger.info("Telegram Webhook set successfully with secret verification.")
            except Exception as e:
                logger.error(f"Failed to set Telegram Webhook: {e}")
        else:
            logger.info("WEBHOOK_URL is blank. Starting in local Long Polling mode...")
            try:
                await bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                logger.debug(f"Delete webhook info: {e}")

            # Start polling in a background task
            polling_task = asyncio.create_task(
                dp.start_polling(
                    bot,
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"],
                )
            )
            app.state.polling_task = polling_task
            logger.info("Long Polling background task active.")

    yield

    # Graceful Shutdown
    logger.info("Shutting down AnonChat Application...")

    # Notify users in active sessions about server update/restart
    bot: Optional[Bot] = getattr(app.state, "bot", None)
    if bot:
        try:
            from bot import matchmaker, get_main_menu_keyboard
            active_uids = list(matchmaker.active_sessions.keys())
            if active_uids:
                logger.info(f"Notifying {len(active_uids)} active user(s) about server restart...")
                restart_msg = (
                    "🔄 **Server Restart Notice**\n\n"
                    "The server is restarting for a quick system update!\n"
                    "Your current session has ended. Tap **🔍 Find Partner** or send /find in a moment to connect with someone new."
                )
                for uid in active_uids:
                    try:
                        await bot.send_message(
                            uid, restart_msg, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Failed to notify active chat users during shutdown: {e}")

    polling_task = getattr(app.state, "polling_task", None)
    if polling_task and not polling_task.done():
        logger.info("Cancelling background polling task...")
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    if hasattr(app.state, "bot") and app.state.bot.session:
        try:
            await app.state.bot.session.close()
        except Exception:
            pass

    await engine.dispose()
    logger.info("Database connections closed. Shutdown complete.")


app = FastAPI(
    title="AnonChat Bot & Admin Console",
    description="Production-Ready Anonymous 1-on-1 Telegram Chat Bot and Web Admin Console",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for admin dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Admin Router under /admin
app.include_router(admin_router)


# ==============================================================================
# 1. Keep-Alive / Health Endpoint (For UptimeRobot & Cloud Monitors)
# ==============================================================================

@app.get("/health", tags=["monitoring"])
async def health_check() -> Dict[str, Any]:
    """
    Returns 200 OK with server uptime and operational state.
    Keep Render / Neon free-tier instances awake by pinging this endpoint via UptimeRobot every 5-10 minutes.
    """
    now = utcnow()
    uptime_seconds = int((now - START_TIME).total_seconds())

    bot: Optional[Bot] = getattr(app.state, "bot", None)
    is_polling = bool(
        getattr(app.state, "polling_task", None)
        and not app.state.polling_task.done()
    )

    return {
        "status": "ok",
        "service": "telegram-anon-chat",
        "timestamp": now.isoformat(),
        "uptime_seconds": uptime_seconds,
        "mode": "polling" if is_polling else ("webhook" if WEBHOOK_URL else "unconfigured"),
        "webhook_configured": bool(WEBHOOK_URL),
        "bot_initialized": bot is not None,
    }


# ==============================================================================
# 2. Telegram Webhook Endpoint (POST /webhook)
# ==============================================================================

@app.post("/webhook", tags=["telegram"])
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
) -> Dict[str, bool]:
    """
    Receives incoming webhook updates from Telegram Servers.
    Strictly verifies X-Telegram-Bot-Api-Secret-Token against WEBHOOK_SECRET.
    Rejects unauthorized requests with HTTP 403 Forbidden.
    """
    # 1. Verify Secret Token Header
    if not verify_webhook_secret(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        logger.warning("Rejected webhook request with invalid or missing secret token header.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing X-Telegram-Bot-Api-Secret-Token",
        )

    bot: Optional[Bot] = getattr(app.state, "bot", None)
    dp: Optional[Dispatcher] = getattr(app.state, "dp", None)

    if not bot or not dp:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bot or Dispatcher not initialized",
        )

    # 2. Parse and feed update
    try:
        raw_body = await request.json()
        update = Update.model_validate(raw_body, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)

    return {"ok": True}


# ==============================================================================
# 3. Root Endpoint
# ==============================================================================

@app.get("/", response_class=HTMLResponse, tags=["root"])
async def root_index() -> HTMLResponse:
    """Friendly welcome and redirect to /admin or documentation."""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>AnonChat Bot API & Console</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-950 text-gray-200 min-h-screen flex items-center justify-center font-sans p-6">
        <div class="max-w-md w-full bg-gray-900 border border-gray-800 rounded-3xl p-8 shadow-2xl text-center space-y-6">
            <div class="w-16 h-16 bg-indigo-600 rounded-2xl mx-auto flex items-center justify-center text-3xl shadow-lg shadow-indigo-500/20">
                💬
            </div>
            <div>
                <h1 class="text-2xl font-bold text-white">AnonChat Service</h1>
                <p class="text-sm text-gray-400 mt-1">Anonymous 1-on-1 Telegram Bot & Admin Console</p>
            </div>
            <div class="space-y-3">
                <a href="/admin" class="block w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl font-semibold shadow transition">
                    Open Web Admin Console 🛡️
                </a>
                <a href="/health" class="block w-full py-2.5 px-4 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-xl text-sm font-medium transition">
                    Health Check Endpoint (/health) 🩺
                </a>
                <a href="/docs" class="block w-full py-2.5 px-4 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-xl text-sm font-medium transition">
                    FastAPI OpenAPI Docs (/docs) 📑
                </a>
            </div>
            <div class="text-xs text-gray-500">
                Production-Ready • Free-Tier Optimized • UptimeRobot Ready
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)

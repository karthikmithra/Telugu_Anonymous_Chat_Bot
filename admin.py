"""
admin.py - HTTP Basic Auth Protected Web Admin Console, Chat Replay UI & Media Proxy.
Features:
- Dark-mode, responsive Single Page Application (Tailwind CSS CDN + Alpine.js)
- Real-time KPI Cards (Total Users, Active Chats, Normal & Shadow Queues, Open Reports)
- User Management: search, sort, Ban/Unban, Shadowban/Unshadow, Reset Cooldown/Abuse
- Chat Replay: 2-sided chat bubble modal, timestamps, media viewer
- Secure Media Proxy: Streams Telegram media without leaking BOT_TOKEN
- Reports Moderation: inspect reported chats, 1-click ban, dismiss reports
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict, List, Optional

import httpx
from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from bot import matchmaker
from database import (
    get_admin_kpis,
    get_admin_reports,
    get_admin_sessions,
    get_admin_users,
    get_session_replay,
    reset_user_cooldown_and_abuse,
    set_user_ban_status,
    set_user_premium_status,
    set_user_shadowban_status,
    update_report_status,
)

admin_router = APIRouter(prefix="/admin", tags=["admin"])

security_basic = HTTPBasic()

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")


def get_current_admin(credentials: HTTPBasicCredentials = Depends(security_basic)) -> str:
    """Verifies HTTP Basic Auth credentials with constant-time comparison."""
    correct_username = secrets.compare_digest(credentials.username.encode("utf8"), ADMIN_USER.encode("utf8"))
    correct_password = secrets.compare_digest(credentials.password.encode("utf8"), ADMIN_PASS.encode("utf8"))

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# Pydantic Request Models
class BanRequest(BaseModel):
    is_banned: bool


class ShadowbanRequest(BaseModel):
    is_shadowbanned: bool


class PremiumRequest(BaseModel):
    is_premium: bool


class ReportStatusRequest(BaseModel):
    status: str


# ==============================================================================
# 1. API Endpoints for Admin Dashboard
# ==============================================================================

@admin_router.get("/api/kpis")
async def api_get_kpis(_: str = Depends(get_current_admin)) -> Dict[str, Any]:
    active_pairs, normal_q, shadow_q = matchmaker.get_stats()
    return await get_admin_kpis(active_pairs, normal_q, shadow_q)


@admin_router.get("/api/users")
async def api_get_users(
    query: Optional[str] = None,
    sort_by: str = "last_active",
    order: str = "desc",
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    offset = (page - 1) * limit
    users, total = await get_admin_users(
        query=query, sort_by=sort_by, order=order, limit=limit, offset=offset
    )
    return {
        "users": users,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
    }


@admin_router.post("/api/users/{telegram_id}/ban")
async def api_ban_user(
    telegram_id: int,
    req: BanRequest,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    success = await set_user_ban_status(telegram_id, req.is_banned)
    if req.is_banned:
        # If active in a chat, terminate immediately
        await matchmaker.end_session(telegram_id)
        await matchmaker.leave_queues(telegram_id)
    return {"success": success, "telegram_id": telegram_id, "is_banned": req.is_banned}


@admin_router.post("/api/users/{telegram_id}/shadowban")
async def api_shadowban_user(
    telegram_id: int,
    req: ShadowbanRequest,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    success = await set_user_shadowban_status(telegram_id, req.is_shadowbanned)
    return {"success": success, "telegram_id": telegram_id, "is_shadowbanned": req.is_shadowbanned}


@admin_router.post("/api/users/{telegram_id}/premium")
async def api_premium_user(
    telegram_id: int,
    req: PremiumRequest,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    success = await set_user_premium_status(telegram_id, req.is_premium)
    return {"success": success, "telegram_id": telegram_id, "is_premium": req.is_premium}


@admin_router.post("/api/users/{telegram_id}/reset")
async def api_reset_user(
    telegram_id: int,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    success = await reset_user_cooldown_and_abuse(telegram_id)
    return {"success": success, "telegram_id": telegram_id}


@admin_router.get("/api/sessions")
async def api_get_sessions(
    user_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    offset = (page - 1) * limit
    sessions, total = await get_admin_sessions(limit=limit, offset=offset, user_id=user_id)
    return {
        "sessions": sessions,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
    }


@admin_router.get("/api/sessions/{session_id}/replay")
async def api_get_session_replay(
    session_id: int,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    data = await get_session_replay(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return data


@admin_router.get("/api/reports")
async def api_get_reports(
    status_filter: Optional[str] = "open",
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    reports = await get_admin_reports(status_filter=status_filter)
    return {"reports": reports}


@admin_router.post("/api/reports/{report_id}/status")
async def api_update_report_status(
    report_id: int,
    req: ReportStatusRequest,
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    success = await update_report_status(report_id, req.status)
    return {"success": success, "report_id": report_id, "status": req.status}


# ==============================================================================
# 2. Secure Media Proxy (Hides BOT_TOKEN from Client)
# ==============================================================================

@admin_router.get("/media/{file_id}")
async def proxy_telegram_media(
    file_id: str,
    request: Request,
    _: str = Depends(get_current_admin),
) -> StreamingResponse:
    """
    Fetches the file URL from Telegram Bot API via bot.get_file(file_id)
    and streams the binary data to the browser securely.
    """
    bot: Optional[Bot] = getattr(request.app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=500, detail="Telegram bot client not initialized")

    try:
        file_info = await bot.get_file(file_id)
        if not file_info.file_path:
            raise HTTPException(status_code=404, detail="File path not found on Telegram servers")

        tg_url = f"https://api.telegram.org/file/bot{bot.token}/{file_info.file_path}"
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to resolve file_id: {e}")

    client = httpx.AsyncClient(timeout=30.0)

    async def stream_media():
        try:
            async with client.stream("GET", tg_url) as resp:
                if resp.status_code != 200:
                    yield b""
                    return
                async for chunk in resp.aiter_bytes(chunk_size=16384):
                    yield chunk
        finally:
            await client.aclose()

    # Guess content type based on extension
    file_path_lower = file_info.file_path.lower()
    content_type = "application/octet-stream"
    if file_path_lower.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_path_lower.endswith(".png"):
        content_type = "image/png"
    elif file_path_lower.endswith(".webp"):
        content_type = "image/webp"
    elif file_path_lower.endswith(".mp4"):
        content_type = "video/mp4"
    elif file_path_lower.endswith((".oga", ".ogg")):
        content_type = "audio/ogg"
    elif file_path_lower.endswith(".mp3"):
        content_type = "audio/mpeg"

    return StreamingResponse(stream_media(), media_type=content_type)


# ==============================================================================
# 3. Single-Page Admin Console HTML
# ==============================================================================

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AnonChat Admin Console</title>
    <!-- Tailwind CSS CDN -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        darkbg: '#0B0F19',
                        cardbg: '#111827',
                        bordercol: '#1F2937',
                        accent: '#6366F1',
                        accenthover: '#4F46E5',
                    }
                }
            }
        }
    </script>
    <!-- Alpine.js CDN -->
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.3/dist/cdn.min.js"></script>
    <style>
        [x-cloak] { display: none !important; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #111827; }
        ::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #4B5563; }
    </style>
</head>
<body class="bg-darkbg text-gray-200 min-h-screen antialiased flex flex-col font-sans" x-data="adminApp()" x-init="initDashboard()">

    <!-- Top Navigation Bar -->
    <header class="bg-cardbg border-b border-bordercol px-6 py-4 flex items-center justify-between sticky top-0 z-40">
        <div class="flex items-center space-x-3">
            <div class="w-9 h-9 rounded-lg bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
                💬
            </div>
            <div>
                <h1 class="text-lg font-bold text-white tracking-tight">AnonChat <span class="text-xs px-2 py-0.5 rounded bg-indigo-900/60 text-indigo-400 border border-indigo-700/50">Admin Console</span></h1>
                <p class="text-xs text-gray-400">1-on-1 Anonymous Telegram Bot Manager</p>
            </div>
        </div>

        <!-- Navigation Tabs -->
        <nav class="flex items-center space-x-1 bg-gray-900/80 p-1 rounded-xl border border-bordercol">
            <button @click="currentTab = 'kpis'" :class="currentTab === 'kpis' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:text-white'" class="px-4 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2">
                <span>📊</span><span>Overview</span>
            </button>
            <button @click="currentTab = 'users'; loadUsers()" :class="currentTab === 'users' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:text-white'" class="px-4 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2">
                <span>👥</span><span>Users</span>
            </button>
            <button @click="currentTab = 'sessions'; loadSessions()" :class="currentTab === 'sessions' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:text-white'" class="px-4 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2">
                <span>🔄</span><span>Chat Sessions</span>
            </button>
            <button @click="currentTab = 'reports'; loadReports()" :class="currentTab === 'reports' ? 'bg-indigo-600 text-white' : 'text-gray-400 hover:text-white'" class="px-4 py-2 rounded-lg text-sm font-medium transition flex items-center space-x-2 relative">
                <span>🚨</span><span>Reports</span>
                <span x-show="kpis.open_reports > 0" x-text="kpis.open_reports" class="ml-1 px-1.5 py-0.2 rounded-full bg-red-600 text-[10px] text-white font-bold animate-pulse"></span>
            </button>
        </nav>

        <!-- Right Action Button -->
        <div class="flex items-center space-x-3">
            <button @click="refreshAll()" class="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition text-sm flex items-center space-x-1 border border-bordercol">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                <span>Refresh</span>
            </button>
        </div>
    </header>

    <!-- Toast Notification Banner -->
    <div x-show="toast.visible" x-cloak class="fixed bottom-5 right-5 z-50 flex items-center p-4 rounded-xl shadow-2xl border text-sm transition-all"
         :class="toast.type === 'error' ? 'bg-red-950/90 text-red-200 border-red-700/60' : 'bg-emerald-950/90 text-emerald-200 border-emerald-700/60'">
        <span class="mr-2" x-text="toast.type === 'error' ? '❌' : '✅'"></span>
        <span x-text="toast.message"></span>
    </div>

    <!-- Main Content Area -->
    <main class="flex-1 p-6 max-w-7xl w-full mx-auto space-y-6">

        <!-- ============================================================== -->
        <!-- TAB 1: OVERVIEW & TOP KPIS                                    -->
        <!-- ============================================================== -->
        <div x-show="currentTab === 'kpis'" class="space-y-6">
            <div class="flex items-center justify-between">
                <div>
                    <h2 class="text-xl font-bold text-white">System Metrics & Vital KPIs</h2>
                    <p class="text-sm text-gray-400">Live operational overview of queues, active sessions, and community moderation.</p>
                </div>
            </div>

            <!-- Top KPI Cards Grid -->
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
                <!-- Total Users -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">Total Users</span>
                        <span class="text-lg">👥</span>
                    </div>
                    <div class="text-3xl font-extrabold text-white" x-text="kpis.total_users || 0"></div>
                    <div class="text-xs text-gray-400 mt-2 flex items-center space-x-1">
                        <span class="text-red-400 font-medium" x-text="(kpis.banned_users || 0) + ' banned'"></span>
                    </div>
                </div>

                <!-- VIP Premium Users -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">VIP Members</span>
                        <span class="text-lg">⭐</span>
                    </div>
                    <div class="text-3xl font-extrabold text-yellow-400" x-text="kpis.premium_users || 0"></div>
                    <div class="text-xs text-gray-400 mt-2">Paid VIP Subscribers</div>
                </div>

                <!-- Active Chats Now -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">Active Chats</span>
                        <span class="w-3 h-3 rounded-full bg-emerald-500 animate-ping"></span>
                    </div>
                    <div class="text-3xl font-extrabold text-emerald-400" x-text="kpis.active_chats_now || 0"></div>
                    <div class="text-xs text-gray-400 mt-2">Pairs currently connected</div>
                </div>

                <!-- Normal Queue Size -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">Normal Queue</span>
                        <span class="text-lg">⚡</span>
                    </div>
                    <div class="text-3xl font-extrabold text-indigo-400" x-text="kpis.normal_queue_size || 0"></div>
                    <div class="text-xs text-gray-400 mt-2">Verified users searching</div>
                </div>

                <!-- Shadow Queue Size -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">Shadow Purgatory</span>
                        <span class="text-lg">👻</span>
                    </div>
                    <div class="text-3xl font-extrabold text-amber-400" x-text="kpis.shadow_queue_size || 0"></div>
                    <div class="text-xs text-gray-400 mt-2">Spammers/bots isolated</div>
                </div>

                <!-- Open Reports -->
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col justify-between">
                    <div class="flex items-center justify-between text-gray-400 mb-2">
                        <span class="text-xs uppercase tracking-wider font-semibold">Open Reports</span>
                        <span class="text-lg">🚨</span>
                    </div>
                    <div class="text-3xl font-extrabold text-red-500" x-text="kpis.open_reports || 0"></div>
                    <div class="text-xs text-gray-400 mt-2">Pending moderation</div>
                </div>
            </div>

            <!-- Secondary Metrics Row -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol flex items-center justify-between">
                    <div>
                        <div class="text-xs text-gray-400 uppercase font-semibold">Lifetime Chat Sessions</div>
                        <div class="text-2xl font-bold text-white mt-1" x-text="kpis.total_sessions || 0"></div>
                    </div>
                    <div class="text-3xl text-gray-600">💬</div>
                </div>
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol flex items-center justify-between">
                    <div>
                        <div class="text-xs text-gray-400 uppercase font-semibold">Total Messages Relayed</div>
                        <div class="text-2xl font-bold text-white mt-1" x-text="kpis.total_messages || 0"></div>
                    </div>
                    <div class="text-3xl text-gray-600">📨</div>
                </div>
                <div class="bg-cardbg p-5 rounded-2xl border border-bordercol flex items-center justify-between">
                    <div>
                        <div class="text-xs text-gray-400 uppercase font-semibold">Shadowbanned In Purgatory</div>
                        <div class="text-2xl font-bold text-amber-400 mt-1" x-text="kpis.shadowbanned_users || 0"></div>
                    </div>
                    <div class="text-3xl text-gray-600">🛡️</div>
                </div>
            </div>
        </div>

        <!-- ============================================================== -->
        <!-- TAB 2: USERS MANAGEMENT                                       -->
        <!-- ============================================================== -->
        <div x-show="currentTab === 'users'" class="space-y-4">
            <!-- Filter & Search Bar -->
            <div class="bg-cardbg p-4 rounded-2xl border border-bordercol flex flex-wrap items-center justify-between gap-4">
                <div class="flex items-center space-x-3 flex-1 min-w-[280px]">
                    <div class="relative w-full max-w-md">
                        <input type="text" x-model="usersFilter.query" @keydown.enter="loadUsers()" placeholder="Search by Telegram ID or @username..."
                               class="w-full bg-gray-900 border border-bordercol rounded-xl px-4 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500">
                    </div>
                    <button @click="loadUsers()" class="px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-sm font-medium transition">Search</button>
                </div>

                <div class="flex items-center space-x-3 text-sm">
                    <span class="text-gray-400">Sort By:</span>
                    <select x-model="usersFilter.sort_by" @change="loadUsers()" class="bg-gray-900 border border-bordercol rounded-xl px-3 py-2 text-white text-sm focus:outline-none">
                        <option value="last_active">Last Active</option>
                        <option value="abuse_score">Abuse Score</option>
                        <option value="report_count">Report Count</option>
                        <option value="next_press_count">Next Skips</option>
                        <option value="total_chats">Total Chats</option>
                        <option value="created_at">Joined Date</option>
                    </select>
                    <select x-model="usersFilter.order" @change="loadUsers()" class="bg-gray-900 border border-bordercol rounded-xl px-3 py-2 text-white text-sm focus:outline-none">
                        <option value="desc">Desc</option>
                        <option value="asc">Asc</option>
                    </select>
                </div>
            </div>

            <!-- Users Table -->
            <div class="bg-cardbg rounded-2xl border border-bordercol overflow-hidden shadow-lg">
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-gray-900/60 text-gray-400 text-xs uppercase tracking-wider border-b border-bordercol">
                            <tr>
                                <th class="px-6 py-4">User</th>
                                <th class="px-4 py-4">Profile (Age • Gender • City)</th>
                                <th class="px-4 py-4">Status</th>
                                <th class="px-4 py-4 text-center">Chats / Msgs</th>
                                <th class="px-4 py-4 text-center">Skips</th>
                                <th class="px-4 py-4 text-center">Reports</th>
                                <th class="px-4 py-4 text-center">Abuse</th>
                                <th class="px-4 py-4">Last Active</th>
                                <th class="px-6 py-4 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-bordercol">
                            <template x-for="u in usersList" :key="u.telegram_id">
                                <tr class="hover:bg-gray-900/40 transition">
                                    <td class="px-6 py-4 font-mono text-xs">
                                        <div class="font-bold text-white text-sm font-sans flex items-center space-x-1.5">
                                            <span x-text="u.first_name || 'Anonymous'"></span>
                                            <span x-show="u.is_premium" class="text-xs text-yellow-400 font-bold" title="VIP Premium Member">⭐ VIP</span>
                                        </div>
                                        <div class="text-gray-400" x-text="u.username ? '@' + u.username : 'ID: ' + u.telegram_id"></div>
                                        <div class="text-[10px] text-gray-600" x-text="'ID: ' + u.telegram_id"></div>
                                    </td>
                                    <td class="px-4 py-4 text-xs">
                                        <div class="font-medium text-gray-200" x-text="u.age ? (u.age + ' yrs • ' + (u.gender || '-') + (u.age < 18 ? ' ⚠️' : '')) : 'Profile Incomplete'"></div>
                                        <div class="text-gray-400 text-[11px]" x-text="u.location || 'No City'"></div>
                                        <div x-show="u.age && u.age < 18" class="text-[10px] text-amber-400 font-bold">Under 18 (Minor)</div>
                                    </td>
                                    <td class="px-4 py-4">
                                        <div class="flex flex-col space-y-1">
                                            <span x-show="u.is_premium" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-yellow-950 text-yellow-400 border border-yellow-800/50 w-max">⭐ VIP</span>
                                            <span x-show="u.is_banned" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-red-950 text-red-400 border border-red-800/50 w-max">BANNED</span>
                                            <span x-show="u.is_shadowbanned" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-950 text-amber-400 border border-amber-800/50 w-max">SHADOWBAN</span>
                                            <span x-show="u.in_cooldown" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-950 text-purple-400 border border-purple-800/50 w-max">COOLDOWN</span>
                                            <span x-show="!u.is_banned && !u.is_shadowbanned && !u.in_cooldown" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950 text-emerald-400 border border-emerald-800/50 w-max">ACTIVE</span>
                                            <span x-show="u.is_verified" class="text-[10px] text-blue-400 font-semibold">✓ Verified</span>
                                        </div>
                                    </td>
                                    <td class="px-4 py-4 text-center">
                                        <span class="font-bold text-white" x-text="u.total_chats"></span>
                                        <span class="text-xs text-gray-500"> / </span>
                                        <span class="text-gray-400 text-xs" x-text="u.total_messages"></span>
                                    </td>
                                    <td class="px-4 py-4 text-center font-semibold text-gray-300" x-text="u.next_press_count"></td>
                                    <td class="px-4 py-4 text-center font-bold" :class="u.report_count > 0 ? 'text-red-400' : 'text-gray-400'" x-text="u.report_count"></td>
                                    <td class="px-4 py-4 text-center">
                                        <span class="px-2 py-1 rounded-lg text-xs font-bold"
                                              :class="u.abuse_score >= 10 ? 'bg-red-900/50 text-red-300' : (u.abuse_score >= 3 ? 'bg-amber-900/50 text-amber-300' : 'bg-gray-800 text-gray-400')"
                                              x-text="u.abuse_score">
                                        </span>
                                    </td>
                                    <td class="px-4 py-4 text-xs text-gray-400" x-text="formatDate(u.last_active)"></td>
                                    <td class="px-6 py-4 text-right space-x-1 whitespace-nowrap">
                                        <!-- VIP Toggle -->
                                        <button @click="togglePremium(u)"
                                                :class="u.is_premium ? 'bg-yellow-800/60 text-yellow-300 hover:bg-yellow-700' : 'bg-gray-800 hover:bg-yellow-950 text-gray-300 hover:text-yellow-400'"
                                                class="px-2.5 py-1 rounded-lg text-xs font-semibold transition"
                                                x-text="u.is_premium ? 'Revoke VIP' : 'Grant VIP'">
                                        </button>
                                        <!-- Ban/Unban -->
                                        <button @click="toggleBan(u)"
                                                :class="u.is_banned ? 'bg-emerald-800/60 text-emerald-300 hover:bg-emerald-700' : 'bg-red-800/60 text-red-300 hover:bg-red-700'"
                                                class="px-2.5 py-1 rounded-lg text-xs font-semibold transition"
                                                x-text="u.is_banned ? 'Unban' : 'Ban'">
                                        </button>
                                        <!-- Shadowban -->
                                        <button @click="toggleShadow(u)"
                                                :class="u.is_shadowbanned ? 'bg-purple-800/60 text-purple-300 hover:bg-purple-700' : 'bg-amber-800/60 text-amber-300 hover:bg-amber-700'"
                                                class="px-2.5 py-1 rounded-lg text-xs font-semibold transition"
                                                x-text="u.is_shadowbanned ? 'Unshadow' : 'Shadow'">
                                        </button>
                                        <!-- Reset -->
                                        <button @click="resetUserAbuse(u)" title="Reset Abuse & Cooldown" class="px-2 py-1 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-xs transition">
                                            Reset
                                        </button>
                                        <!-- View User Chats -->
                                        <button @click="filterSessionsByUser(u.telegram_id)" title="View Chats" class="px-2 py-1 bg-indigo-900/60 text-indigo-300 hover:bg-indigo-800 rounded-lg text-xs transition">
                                            Chats
                                        </button>
                                    </td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>

                <!-- Pagination -->
                <div class="px-6 py-4 border-t border-bordercol flex items-center justify-between text-xs text-gray-400">
                    <div>Showing <span x-text="usersList.length"></span> of <span x-text="usersTotal"></span> users</div>
                    <div class="flex items-center space-x-2">
                        <button :disabled="usersPage <= 1" @click="usersPage--; loadUsers()" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 disabled:opacity-40 transition">Previous</button>
                        <span class="px-2 text-white font-semibold" x-text="usersPage"></span>
                        <button :disabled="usersPage >= usersMaxPages" @click="usersPage++; loadUsers()" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 disabled:opacity-40 transition">Next</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============================================================== -->
        <!-- TAB 3: CHAT SESSIONS & REPLAY                                 -->
        <!-- ============================================================== -->
        <div x-show="currentTab === 'sessions'" class="space-y-4">
            <div class="bg-cardbg p-4 rounded-2xl border border-bordercol flex items-center justify-between">
                <div class="flex items-center space-x-3">
                    <h3 class="font-bold text-white text-base">Recorded Chat Sessions</h3>
                    <template x-if="sessionsFilterUser">
                        <div class="flex items-center space-x-2 bg-indigo-950/80 border border-indigo-700 text-indigo-300 px-3 py-1 rounded-xl text-xs">
                            <span x-text="'Filtering for User: ' + sessionsFilterUser"></span>
                            <button @click="sessionsFilterUser = null; loadSessions()" class="text-white hover:text-red-400 font-bold ml-1">✕</button>
                        </div>
                    </template>
                </div>
                <button @click="loadSessions()" class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-white rounded-lg text-xs">Refresh</button>
            </div>

            <!-- Sessions Table -->
            <div class="bg-cardbg rounded-2xl border border-bordercol overflow-hidden shadow-lg">
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-gray-900/60 text-gray-400 text-xs uppercase tracking-wider border-b border-bordercol">
                            <tr>
                                <th class="px-6 py-4">Session ID</th>
                                <th class="px-4 py-4">User A</th>
                                <th class="px-4 py-4">User B</th>
                                <th class="px-4 py-4 text-center">Type</th>
                                <th class="px-4 py-4 text-center">Messages</th>
                                <th class="px-4 py-4 text-center">Duration</th>
                                <th class="px-4 py-4">Started At</th>
                                <th class="px-6 py-4 text-right">Replay</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-bordercol">
                            <template x-for="s in sessionsList" :key="s.id">
                                <tr class="hover:bg-gray-900/40 transition" :class="s.is_reported ? 'bg-red-950/20' : ''">
                                    <td class="px-6 py-4 font-mono text-xs font-bold text-white">
                                        <span x-text="'#' + s.id"></span>
                                        <span x-show="s.is_reported" class="ml-2 px-1.5 py-0.5 rounded bg-red-900 text-red-300 text-[10px]">REPORTED</span>
                                    </td>
                                    <td class="px-4 py-4 font-mono text-xs text-indigo-400 hover:underline cursor-pointer" @click="filterSessionsByUser(s.user_a_id)" x-text="s.user_a_id"></td>
                                    <td class="px-4 py-4 font-mono text-xs text-indigo-400 hover:underline cursor-pointer" @click="filterSessionsByUser(s.user_b_id)" x-text="s.user_b_id"></td>
                                    <td class="px-4 py-4 text-center">
                                        <span x-show="s.is_shadow_session" class="px-2 py-0.5 rounded-full text-[10px] bg-amber-950 text-amber-400 border border-amber-800">SHADOW</span>
                                        <span x-show="!s.is_shadow_session" class="px-2 py-0.5 rounded-full text-[10px] bg-gray-800 text-gray-300">NORMAL</span>
                                    </td>
                                    <td class="px-4 py-4 text-center font-bold text-white" x-text="s.message_count"></td>
                                    <td class="px-4 py-4 text-center text-xs text-gray-400" x-text="s.duration_seconds ? s.duration_seconds + 's' : 'Active'"></td>
                                    <td class="px-4 py-4 text-xs text-gray-400" x-text="formatDate(s.started_at)"></td>
                                    <td class="px-6 py-4 text-right">
                                        <button @click="openReplay(s.id)" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold shadow transition">
                                            Replay 🎥
                                        </button>
                                    </td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>

                <!-- Pagination -->
                <div class="px-6 py-4 border-t border-bordercol flex items-center justify-between text-xs text-gray-400">
                    <div>Showing <span x-text="sessionsList.length"></span> of <span x-text="sessionsTotal"></span> sessions</div>
                    <div class="flex items-center space-x-2">
                        <button :disabled="sessionsPage <= 1" @click="sessionsPage--; loadSessions()" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 disabled:opacity-40 transition">Previous</button>
                        <span class="px-2 text-white font-semibold" x-text="sessionsPage"></span>
                        <button :disabled="sessionsPage >= sessionsMaxPages" @click="sessionsPage++; loadSessions()" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 disabled:opacity-40 transition">Next</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============================================================== -->
        <!-- TAB 4: REPORTS MODERATION                                      -->
        <!-- ============================================================== -->
        <div x-show="currentTab === 'reports'" class="space-y-4">
            <div class="bg-cardbg p-4 rounded-2xl border border-bordercol flex items-center justify-between">
                <div>
                    <h3 class="font-bold text-white text-base">User Abuse & Incident Reports</h3>
                    <p class="text-xs text-gray-400">Brigade-protected report stream for rapid moderation.</p>
                </div>
                <div class="flex items-center space-x-2 text-sm">
                    <button @click="reportsStatusFilter = 'open'; loadReports()" :class="reportsStatusFilter === 'open' ? 'bg-red-600 text-white' : 'bg-gray-800 text-gray-400'" class="px-3 py-1.5 rounded-xl text-xs font-semibold">Open</button>
                    <button @click="reportsStatusFilter = 'dismissed'; loadReports()" :class="reportsStatusFilter === 'dismissed' ? 'bg-gray-700 text-white' : 'bg-gray-800 text-gray-400'" class="px-3 py-1.5 rounded-xl text-xs font-semibold">Dismissed</button>
                    <button @click="reportsStatusFilter = 'all'; loadReports()" :class="reportsStatusFilter === 'all' ? 'bg-gray-700 text-white' : 'bg-gray-800 text-gray-400'" class="px-3 py-1.5 rounded-xl text-xs font-semibold">All</button>
                </div>
            </div>

            <!-- Reports List Grid -->
            <div class="grid grid-cols-1 gap-4">
                <template x-for="r in reportsList" :key="r.id">
                    <div class="bg-cardbg p-5 rounded-2xl border border-bordercol shadow-lg flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
                        <div class="space-y-1">
                            <div class="flex items-center space-x-2">
                                <span class="px-2 py-0.5 rounded text-[10px] font-bold"
                                      :class="r.status === 'open' ? 'bg-red-950 text-red-400 border border-red-800' : 'bg-gray-800 text-gray-400'"
                                      x-text="r.status.toUpperCase()"></span>
                                <span class="text-xs text-gray-500" x-text="formatDate(r.created_at)"></span>
                            </div>
                            <div class="text-sm font-semibold text-white">
                                Reporter <span class="font-mono text-indigo-400" x-text="r.reporter_username ? '@' + r.reporter_username : r.reporter_id"></span>
                                reported <span class="font-mono text-red-400 font-bold" x-text="r.reported_username ? '@' + r.reported_username : r.reported_id"></span>
                            </div>
                            <div class="text-xs text-gray-400">
                                Reason: <span class="text-gray-300 font-medium" x-text="r.reason"></span> |
                                Reported User Abuse Score: <span class="text-amber-400 font-bold" x-text="r.reported_abuse_score"></span>
                            </div>
                        </div>

                        <div class="flex items-center space-x-2">
                            <!-- Replay Session -->
                            <button @click="openReplay(r.session_id)" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-xs font-semibold transition">
                                Replay Session 🎥
                            </button>
                            <!-- Ban Reported User -->
                            <button x-show="!r.reported_is_banned" @click="banReportedUser(r.reported_id, r.id)" class="px-3 py-1.5 bg-red-800 hover:bg-red-700 text-red-100 rounded-xl text-xs font-semibold transition">
                                Ban Offender 🚫
                            </button>
                            <!-- Dismiss -->
                            <button x-show="r.status === 'open'" @click="dismissReport(r.id)" class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-xl text-xs transition">
                                Dismiss
                            </button>
                        </div>
                    </div>
                </template>
                <div x-show="reportsList.length === 0" class="text-center py-12 text-gray-500 text-sm">
                    No reports found for this status.
                </div>
            </div>
        </div>

    </main>

    <!-- ============================================================== -->
    <!-- CHAT REPLAY MODAL (Two-sided bubble alignment & media proxy)    -->
    <!-- ============================================================== -->
    <div x-show="replayModal.open" x-cloak class="fixed inset-0 z-50 overflow-hidden bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
        <div class="bg-cardbg border border-bordercol rounded-3xl w-full max-w-4xl h-[90vh] flex flex-col shadow-2xl overflow-hidden" @click.outside="closeReplay()">

            <!-- Modal Header -->
            <div class="bg-gray-900 px-6 py-4 border-b border-bordercol flex items-center justify-between">
                <div>
                    <div class="flex items-center space-x-3">
                        <h3 class="font-bold text-white text-base" x-text="'Chat Replay #' + (replayModal.session ? replayModal.session.id : '')"></h3>
                        <span x-show="replayModal.session && replayModal.session.is_shadow_session" class="px-2 py-0.5 rounded text-[10px] bg-amber-950 text-amber-400 border border-amber-800 font-bold">SHADOW SESSION</span>
                    </div>
                    <div class="text-xs text-gray-400 mt-0.5" x-text="replayModal.session ? 'Started: ' + formatDate(replayModal.session.started_at) : ''"></div>
                </div>

                <!-- 1-Click Ban Buttons inside Replay -->
                <div class="flex items-center space-x-2">
                    <template x-if="replayModal.session">
                        <div class="flex items-center space-x-2">
                            <button @click="banModalUser(replayModal.session.user_a_id, 'User A')"
                                    :class="replayModal.session.user_a_banned ? 'bg-emerald-900 text-emerald-300' : 'bg-red-900/80 hover:bg-red-800 text-red-200'"
                                    class="px-3 py-1.5 rounded-xl text-xs font-semibold transition"
                                    x-text="replayModal.session.user_a_banned ? 'Unban User A' : 'Ban User A'">
                            </button>
                            <button @click="banModalUser(replayModal.session.user_b_id, 'User B')"
                                    :class="replayModal.session.user_b_banned ? 'bg-emerald-900 text-emerald-300' : 'bg-red-900/80 hover:bg-red-800 text-red-200'"
                                    class="px-3 py-1.5 rounded-xl text-xs font-semibold transition"
                                    x-text="replayModal.session.user_b_banned ? 'Unban User B' : 'Ban User B'">
                            </button>
                        </div>
                    </template>
                    <button @click="closeReplay()" class="p-2 text-gray-400 hover:text-white rounded-lg">✕</button>
                </div>
            </div>

            <!-- Participants Legend Bar -->
            <div class="bg-gray-900/40 px-6 py-2.5 border-b border-bordercol flex items-center justify-between text-xs">
                <div class="flex items-center space-x-2 text-emerald-400 font-semibold">
                    <span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span>
                    <span x-text="'User A: ' + (replayModal.session ? replayModal.session.user_a_id : '')"></span>
                    <template x-if="replayModal.session && replayModal.session.user_a_age">
                        <span class="text-gray-400 font-normal" x-text="'(' + replayModal.session.user_a_age + 'y, ' + (replayModal.session.user_a_gender || '-') + ', ' + (replayModal.session.user_a_location || '-') + ')'"></span>
                    </template>
                    <span x-show="replayModal.session && replayModal.session.user_a_premium" class="px-1.5 py-0.2 rounded bg-yellow-950 text-yellow-400 text-[10px] font-bold border border-yellow-800">⭐ VIP</span>
                </div>
                <div class="text-gray-500 font-mono text-[10px]" x-text="replayModal.messages.length + ' total messages'"></div>
                <div class="flex items-center space-x-2 text-indigo-400 font-semibold">
                    <span x-show="replayModal.session && replayModal.session.user_b_premium" class="px-1.5 py-0.2 rounded bg-yellow-950 text-yellow-400 text-[10px] font-bold border border-yellow-800">⭐ VIP</span>
                    <template x-if="replayModal.session && replayModal.session.user_b_age">
                        <span class="text-gray-400 font-normal" x-text="'(' + replayModal.session.user_b_age + 'y, ' + (replayModal.session.user_b_gender || '-') + ', ' + (replayModal.session.user_b_location || '-') + ')'"></span>
                    </template>
                    <span x-text="'User B: ' + (replayModal.session ? replayModal.session.user_b_id : '')"></span>
                    <span class="w-2.5 h-2.5 rounded-full bg-indigo-500"></span>
                </div>
            </div>

            <!-- Two-Sided Chat Bubble Scrollable Body -->
            <div class="flex-1 overflow-y-auto p-6 space-y-4 bg-darkbg/50">
                <template x-for="msg in replayModal.messages" :key="msg.id">
                    <div class="flex w-full" :class="msg.sender_tag === 'user_a' ? 'justify-start' : 'justify-end'">
                        <div class="max-w-[75%] rounded-2xl p-4 shadow-md space-y-2"
                             :class="msg.sender_tag === 'user_a'
                                ? 'bg-slate-900 border border-emerald-900/40 text-gray-200 rounded-bl-sm'
                                : 'bg-indigo-950/60 border border-indigo-800/40 text-gray-100 rounded-br-sm'">

                            <!-- Bubble Header -->
                            <div class="flex items-center justify-between text-[11px] text-gray-400 border-b border-white/5 pb-1 gap-4">
                                <span class="font-bold uppercase tracking-wider"
                                      :class="msg.sender_tag === 'user_a' ? 'text-emerald-400' : 'text-indigo-400'"
                                      x-text="msg.sender_tag === 'user_a' ? 'User A' : 'User B'">
                                </span>
                                <span class="text-[10px] text-gray-500" x-text="formatTime(msg.timestamp)"></span>
                            </div>

                            <!-- Text Content -->
                            <div x-show="msg.text_content" class="text-sm break-words whitespace-pre-wrap leading-relaxed" x-text="msg.text_content"></div>

                            <!-- Inline Media Display via Secure Backend Proxy -->
                            <template x-if="msg.file_id">
                                <div class="mt-2 rounded-xl overflow-hidden border border-bordercol bg-black/40 p-1">
                                    <!-- Photos & Webp Stickers -->
                                    <template x-if="msg.msg_type === 'photo' || msg.msg_type === 'sticker'">
                                        <img :src="'/admin/media/' + msg.file_id" class="max-h-72 rounded-lg object-contain mx-auto" loading="lazy" alt="Image">
                                    </template>
                                    <!-- Voice / Audio -->
                                    <template x-if="msg.msg_type === 'voice' || msg.msg_type === 'audio'">
                                        <audio controls class="w-full mt-1">
                                            <source :src="'/admin/media/' + msg.file_id">
                                            Your browser does not support audio element.
                                        </audio>
                                    </template>
                                    <!-- Video / Animation / Video Note -->
                                    <template x-if="msg.msg_type === 'video' || msg.msg_type === 'animation' || msg.msg_type === 'video_note'">
                                        <video controls class="max-h-72 rounded-lg mx-auto w-full" preload="metadata">
                                            <source :src="'/admin/media/' + msg.file_id" type="video/mp4">
                                            Your browser does not support video tag.
                                        </video>
                                    </template>
                                    <!-- Document / Others -->
                                    <template x-if="msg.msg_type === 'document'">
                                        <div class="flex items-center space-x-2 p-2 text-xs text-indigo-300">
                                            <span>📄 Document Attachment:</span>
                                            <a :href="'/admin/media/' + msg.file_id" target="_blank" class="underline hover:text-white font-mono">Download File</a>
                                        </div>
                                    </template>
                                </div>
                            </template>

                            <div class="text-[9px] text-gray-500 uppercase font-mono" x-text="'Type: ' + msg.msg_type"></div>
                        </div>
                    </div>
                </template>

                <div x-show="replayModal.messages.length === 0" class="text-center py-16 text-gray-500 text-sm">
                    No messages recorded in this chat session.
                </div>
            </div>

            <!-- Modal Footer -->
            <div class="bg-gray-900 px-6 py-3 border-t border-bordercol flex justify-end">
                <button @click="closeReplay()" class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-white rounded-xl text-xs font-semibold">Close</button>
            </div>
        </div>
    </div>

    <!-- Alpine.js Application Logic -->
    <script>
        function adminApp() {
            return {
                currentTab: 'kpis',
                kpis: {},
                toast: { visible: false, message: '', type: 'success' },

                // Users tab
                usersList: [],
                usersTotal: 0,
                usersPage: 1,
                usersMaxPages: 1,
                usersFilter: { query: '', sort_by: 'last_active', order: 'desc' },

                // Sessions tab
                sessionsList: [],
                sessionsTotal: 0,
                sessionsPage: 1,
                sessionsMaxPages: 1,
                sessionsFilterUser: null,

                // Reports tab
                reportsList: [],
                reportsStatusFilter: 'open',

                // Replay modal
                replayModal: { open: false, session: null, messages: [], reports: [] },

                async initDashboard() {
                    await this.loadKpis();
                },

                showToast(msg, type = 'success') {
                    this.toast.message = msg;
                    this.toast.type = type;
                    this.toast.visible = true;
                    setTimeout(() => { this.toast.visible = false; }, 3500);
                },

                async loadKpis() {
                    try {
                        const res = await fetch('/admin/api/kpis');
                        if (res.ok) this.kpis = await res.json();
                    } catch (e) { console.error('Failed to load KPIs:', e); }
                },

                async refreshAll() {
                    await this.loadKpis();
                    if (this.currentTab === 'users') await this.loadUsers();
                    if (this.currentTab === 'sessions') await this.loadSessions();
                    if (this.currentTab === 'reports') await this.loadReports();
                    this.showToast('Dashboard refreshed');
                },

                async loadUsers() {
                    try {
                        const params = new URLSearchParams({
                            page: this.usersPage,
                            limit: 25,
                            sort_by: this.usersFilter.sort_by,
                            order: this.usersFilter.order,
                        });
                        if (this.usersFilter.query) params.append('query', this.usersFilter.query);

                        const res = await fetch('/admin/api/users?' + params.toString());
                        if (res.ok) {
                            const data = await res.json();
                            this.usersList = data.users;
                            this.usersTotal = data.total;
                            this.usersMaxPages = data.pages || 1;
                        }
                    } catch (e) { console.error('Error loading users:', e); }
                },

                async toggleBan(user) {
                    const nextState = !user.is_banned;
                    try {
                        const res = await fetch(`/admin/api/users/${user.telegram_id}/ban`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ is_banned: nextState })
                        });
                        if (res.ok) {
                            user.is_banned = nextState;
                            this.showToast(nextState ? `User ${user.telegram_id} banned` : `User ${user.telegram_id} unbanned`);
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Failed to update ban status', 'error'); }
                },

                async toggleShadow(user) {
                    const nextState = !user.is_shadowbanned;
                    try {
                        const res = await fetch(`/admin/api/users/${user.telegram_id}/shadowban`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ is_shadowbanned: nextState })
                        });
                        if (res.ok) {
                            user.is_shadowbanned = nextState;
                            this.showToast(nextState ? `User ${user.telegram_id} shadowbanned to purgatory` : `User ${user.telegram_id} unshadowed`);
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Failed to update shadowban status', 'error'); }
                },

                async togglePremium(user) {
                    const nextState = !user.is_premium;
                    try {
                        const res = await fetch(`/admin/api/users/${user.telegram_id}/premium`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ is_premium: nextState })
                        });
                        if (res.ok) {
                            user.is_premium = nextState;
                            this.showToast(nextState ? `User ${user.telegram_id} granted VIP status ⭐` : `User ${user.telegram_id} revoked VIP`);
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Failed to update VIP status', 'error'); }
                },

                async resetUserAbuse(user) {
                    try {
                        const res = await fetch(`/admin/api/users/${user.telegram_id}/reset`, { method: 'POST' });
                        if (res.ok) {
                            user.abuse_score = 0;
                            user.in_cooldown = false;
                            this.showToast(`Cooldown & Abuse score reset for ${user.telegram_id}`);
                        }
                    } catch (e) { this.showToast('Failed to reset user', 'error'); }
                },

                filterSessionsByUser(uid) {
                    this.sessionsFilterUser = uid;
                    this.sessionsPage = 1;
                    this.currentTab = 'sessions';
                    this.loadSessions();
                },

                async loadSessions() {
                    try {
                        const params = new URLSearchParams({
                            page: this.sessionsPage,
                            limit: 25,
                        });
                        if (this.sessionsFilterUser) params.append('user_id', this.sessionsFilterUser);

                        const res = await fetch('/admin/api/sessions?' + params.toString());
                        if (res.ok) {
                            const data = await res.json();
                            this.sessionsList = data.sessions;
                            this.sessionsTotal = data.total;
                            this.sessionsMaxPages = data.pages || 1;
                        }
                    } catch (e) { console.error('Error loading sessions:', e); }
                },

                async openReplay(sessionId) {
                    try {
                        const res = await fetch(`/admin/api/sessions/${sessionId}/replay`);
                        if (res.ok) {
                            const data = await res.json();
                            this.replayModal.session = data.session;
                            this.replayModal.messages = data.messages;
                            this.replayModal.reports = data.reports;
                            this.replayModal.open = true;
                        } else {
                            this.showToast('Chat replay session not found', 'error');
                        }
                    } catch (e) { this.showToast('Failed to load replay', 'error'); }
                },

                closeReplay() {
                    this.replayModal.open = false;
                },

                async banModalUser(userId, userLabel) {
                    const isUserA = this.replayModal.session.user_a_id === userId;
                    const currentlyBanned = isUserA ? this.replayModal.session.user_a_banned : this.replayModal.session.user_b_banned;
                    const nextState = !currentlyBanned;

                    try {
                        const res = await fetch(`/admin/api/users/${userId}/ban`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ is_banned: nextState })
                        });
                        if (res.ok) {
                            if (isUserA) this.replayModal.session.user_a_banned = nextState;
                            else this.replayModal.session.user_b_banned = nextState;
                            this.showToast(`${userLabel} (${userId}) ${nextState ? 'Banned' : 'Unbanned'}`);
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Ban action failed', 'error'); }
                },

                async loadReports() {
                    try {
                        const res = await fetch(`/admin/api/reports?status_filter=${this.reportsStatusFilter}`);
                        if (res.ok) {
                            const data = await res.json();
                            this.reportsList = data.reports;
                        }
                    } catch (e) { console.error('Error loading reports:', e); }
                },

                async banReportedUser(reportedId, reportId) {
                    try {
                        const res = await fetch(`/admin/api/users/${reportedId}/ban`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ is_banned: true })
                        });
                        if (res.ok) {
                            await fetch(`/admin/api/reports/${reportId}/status`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ status: 'resolved' })
                            });
                            this.showToast(`User ${reportedId} banned & report resolved`);
                            await this.loadReports();
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Failed to ban reported user', 'error'); }
                },

                async dismissReport(reportId) {
                    try {
                        const res = await fetch(`/admin/api/reports/${reportId}/status`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ status: 'dismissed' })
                        });
                        if (res.ok) {
                            this.showToast(`Report #${reportId} dismissed`);
                            await this.loadReports();
                            await this.loadKpis();
                        }
                    } catch (e) { this.showToast('Failed to dismiss report', 'error'); }
                },

                formatDate(isoStr) {
                    if (!isoStr) return '-';
                    const d = new Date(isoStr);
                    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                },

                formatTime(isoStr) {
                    if (!isoStr) return '';
                    const d = new Date(isoStr);
                    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                }
            }
        }
    </script>
</body>
</html>
"""


@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
async def serve_admin_console(_: str = Depends(get_current_admin)) -> HTMLResponse:
    """Renders the single-page Admin Console UI."""
    return HTMLResponse(content=ADMIN_HTML)

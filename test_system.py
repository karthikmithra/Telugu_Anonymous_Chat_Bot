"""
test_system.py - Comprehensive End-to-End Test Suite for AnonChat Bot & Admin Console.
"""

import asyncio
import os
import sys

import httpx
from fastapi.testclient import TestClient

# Ensure environment is configured for test
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_anonchat.db"
os.environ["BOT_TOKEN"] = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"
os.environ["WEBHOOK_URL"] = "https://test-anonchat.onrender.com"
os.environ["WEBHOOK_SECRET"] = "test_webhook_secret_key_999"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = "secret123"

from database import (
    init_db,
    upsert_user,
    get_user_by_id,
    mark_user_verified,
    update_user_profile,
    set_user_premium_status,
    create_chat_session_record,
    end_chat_session_record,
    log_chat_message,
    create_report_record,
    get_admin_kpis,
    get_admin_users,
    set_user_ban_status,
    set_user_shadowban_status,
    reset_user_cooldown_and_abuse,
    get_admin_sessions,
    get_session_replay,
    get_admin_reports,
    update_report_status,
)
from security import (
    verify_webhook_secret,
    captcha_manager,
    check_quarantine_violation,
    rate_limiter,
)
from bot import matchmaker
from main import app


async def test_database_and_models():
    print("[1/5] Testing Database Layer & Models...")
    await init_db()

    # Test user upsert
    user_a = await upsert_user(telegram_id=1001, username="alice", first_name="Alice")
    user_b = await upsert_user(telegram_id=1002, username="bob", first_name="Bob")
    user_c = await upsert_user(telegram_id=1003, username="spammer", first_name="SpamBot")

    assert user_a.telegram_id == 1001
    assert user_a.username == "alice"
    assert user_b.telegram_id == 1002

    # Verify CAPTCHA passing & Demographics
    await mark_user_verified(1001)
    await mark_user_verified(1002)
    await update_user_profile(1001, age=22, gender="Female", location="Hyderabad")
    await update_user_profile(1002, age=24, gender="Male", location="Visakhapatnam")
    await set_user_premium_status(1001, is_premium=True)

    u_ref = await get_user_by_id(1001)
    assert u_ref.is_verified is True
    assert u_ref.age == 22
    assert u_ref.gender == "Female"
    assert u_ref.location == "Hyderabad"
    assert u_ref.is_premium is True

    # Test Chat Session Creation
    session_id = await create_chat_session_record(user_a_id=1001, user_b_id=1002, is_shadow_session=False)
    assert session_id > 0

    # Test Message Logging
    await log_chat_message(session_id=session_id, sender_id=1001, msg_type="text", text_content="Hello Bob!")
    await log_chat_message(session_id=session_id, sender_id=1002, msg_type="photo", text_content="Hi Alice!", file_id="AgACAgIAAxkBA...")
    await log_chat_message(session_id=session_id, sender_id=1001, msg_type="voice", file_id="AwACAgIAAxkBA...")

    # Test Reports
    rep_id, inc = await create_report_record(
        session_id=session_id,
        reporter_id=1001,
        reported_id=1002,
        reason="Inappropriate message",
    )
    assert rep_id > 0

    # Test Session Ending
    await end_chat_session_record(session_id=session_id, ended_by=1001)

    # Test Session Replay Query
    replay = await get_session_replay(session_id)
    assert replay is not None
    assert replay["session"]["id"] == session_id
    assert len(replay["messages"]) == 3
    assert replay["messages"][0]["text_content"] == "Hello Bob!"
    assert replay["messages"][0]["sender_tag"] == "user_a"
    assert replay["messages"][1]["sender_tag"] == "user_b"
    assert replay["messages"][1]["msg_type"] == "photo"

    print("  -> Database layer passed with flying colors!")


async def test_security_engine():
    print("[2/5] Testing Security & Anti-Bot Engine...")

    # Webhook Secret
    assert verify_webhook_secret("test_webhook_secret_key_999", "test_webhook_secret_key_999") is True
    assert verify_webhook_secret("wrong_token", "test_webhook_secret_key_999") is False

    # Emoji CAPTCHA
    target_emoji, target_name, options = captcha_manager.generate_challenge(9999)
    assert len(options) == 4
    assert target_emoji in options
    # Verify wrong answer
    assert captcha_manager.verify_answer(9999, "❌") is False
    # Verify correct answer
    assert captcha_manager.verify_answer(9999, target_emoji) is True

    # Quarantine Filter
    # New user (<5 chats) sending link -> blocked
    v1 = check_quarantine_violation("check out https://freemoney.com", False, total_chats=1, abuse_score=0)
    assert v1 is not None and "Links" in v1
    # New user sending @username -> blocked
    v2 = check_quarantine_violation("follow me @mychannel", False, total_chats=2, abuse_score=0)
    assert v2 is not None and "usernames" in v2
    # New user sending clean message -> allowed
    v3 = check_quarantine_violation("hello how are you doing today?", False, total_chats=2, abuse_score=0)
    assert v3 is None
    # Trusted user (>=5 chats) sending link -> allowed
    v4 = check_quarantine_violation("check out https://goodsite.com", False, total_chats=8, abuse_score=0)
    assert v4 is None

    # Sliding-Window Rate Limiter
    # 3-second cooldown
    allowed, wait = rate_limiter.check_command_cooldown(7777)
    assert allowed is True
    allowed, wait = rate_limiter.check_command_cooldown(7777)
    assert allowed is False and wait > 0

    # Skip cap: max 12 presses / 10 min
    for i in range(12):
        ok, count, max_p = rate_limiter.record_and_check_next_press(8888)
        assert ok is True
    # 13th press -> breach!
    ok, count, max_p = rate_limiter.record_and_check_next_press(8888)
    assert ok is False and count == 13

    # Message flood: max 8 msgs / 10s
    for i in range(8):
        ok, c = rate_limiter.check_message_flood(6666)
        assert ok is True
    # 9th message -> flood breach!
    ok, c = rate_limiter.check_message_flood(6666)
    assert ok is False

    print("  -> Security & rate limiting engine passed with flying colors!")


async def test_matchmaker_logic():
    print("[3/5] Testing Dual-Queue Matchmaker Logic...")

    # Test Normal Queue pairing
    p1 = await matchmaker.enqueue(2001, is_shadow=False)
    assert p1 is None  # Waiting
    assert 2001 in matchmaker.normal_queue

    p2 = await matchmaker.enqueue(2002, is_shadow=False)
    assert p2 == 2001  # Matched with 2001!
    assert 2001 not in matchmaker.normal_queue

    # Register session
    await matchmaker.register_session(user_a_id=2001, user_b_id=2002, session_id=10, is_shadow=False)
    chat_a = await matchmaker.get_active_chat(2001)
    chat_b = await matchmaker.get_active_chat(2002)
    assert chat_a.partner_id == 2002
    assert chat_b.partner_id == 2001

    # End session
    ended = await matchmaker.end_session(2001)
    assert ended == (10, 2002)
    assert await matchmaker.get_active_chat(2001) is None
    assert await matchmaker.get_active_chat(2002) is None

    # Test Shadow Queue Isolation
    sp1 = await matchmaker.enqueue(3001, is_shadow=True)
    assert sp1 is None
    assert 3001 in matchmaker.shadow_queue

    # Normal user should NOT match with shadow user
    np = await matchmaker.enqueue(4001, is_shadow=False)
    assert np is None
    assert 4001 in matchmaker.normal_queue
    assert 3001 in matchmaker.shadow_queue

    # Second shadow user matches with first shadow user
    sp2 = await matchmaker.enqueue(3002, is_shadow=True)
    assert sp2 == 3001
    assert 3001 not in matchmaker.shadow_queue

    # Clean up queues
    await matchmaker.leave_queues(4001)
    assert 4001 not in matchmaker.normal_queue

    print("  -> Dual-queue matchmaker & shadow purgatory logic passed!")


def test_fastapi_and_admin_endpoints():
    print("[4/5] Testing FastAPI Endpoints & Webhooks via TestClient...")
    with TestClient(app) as client:
        # 1. Health check
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert data["service"] == "telegram-anon-chat"

        # 2. Root landing page
        res_root = client.get("/")
        assert res_root.status_code == 200
        assert "AnonChat Service" in res_root.text

        # 3. Webhook security
        # No header -> 403 Forbidden
        res_bad = client.post("/webhook", json={"update_id": 1})
        assert res_bad.status_code == 403

        # Wrong header -> 403 Forbidden
        res_wrong = client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "invalid_secret"},
        )
        assert res_wrong.status_code == 403

        # Valid secret header -> 200 OK
        res_ok = client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_webhook_secret_key_999"},
        )
        assert res_ok.status_code == 200
        assert res_ok.json() == {"ok": True}

        print("  -> FastAPI health check & secure webhook endpoints passed!")

        print("[5/5] Testing HTTP Basic Auth Admin Console API...")
        # 1. Unauthorized access -> 401
        res_unauth = client.get("/admin/api/kpis")
        assert res_unauth.status_code == 401
        assert "WWW-Authenticate" in res_unauth.headers

        # 2. Authorized access with admin / secret123
        auth = ("admin", "secret123")

        # Serve Admin Console HTML
        res_html = client.get("/admin", auth=auth)
        assert res_html.status_code == 200
        assert "AnonChat Admin Console" in res_html.text

        # KPI Endpoint
        res_kpis = client.get("/admin/api/kpis", auth=auth)
        assert res_kpis.status_code == 200
        kpis = res_kpis.json()
        assert "total_users" in kpis
        assert "active_chats_now" in kpis
        assert "normal_queue_size" in kpis
        assert "shadow_queue_size" in kpis
        assert "open_reports" in kpis

        # Users List Endpoint
        res_users = client.get("/admin/api/users?limit=10", auth=auth)
        assert res_users.status_code == 200
        u_data = res_users.json()
        assert "users" in u_data
        assert "total" in u_data

        # Ban & Unban user
        res_ban = client.post(
            "/admin/api/users/1001/ban",
            json={"is_banned": True},
            auth=auth,
        )
        assert res_ban.status_code == 200
        assert res_ban.json()["is_banned"] is True

        res_unban = client.post(
            "/admin/api/users/1001/ban",
            json={"is_banned": False},
            auth=auth,
        )
        assert res_unban.status_code == 200
        assert res_unban.json()["is_banned"] is False

        # Shadowban user
        res_shadow = client.post(
            "/admin/api/users/1003/shadowban",
            json={"is_shadowbanned": True},
            auth=auth,
        )
        assert res_shadow.status_code == 200
        assert res_shadow.json()["is_shadowbanned"] is True

        # Reset user abuse & cooldown
        res_reset = client.post(
            "/admin/api/users/1001/reset",
            auth=auth,
        )
        assert res_reset.status_code == 200

        # Toggle Premium VIP status
        res_vip = client.post(
            "/admin/api/users/1002/premium",
            json={"is_premium": True},
            auth=auth,
        )
        assert res_vip.status_code == 200
        assert res_vip.json()["is_premium"] is True

        # Sessions List Endpoint
        res_sess = client.get("/admin/api/sessions?limit=10", auth=auth)
        assert res_sess.status_code == 200
        s_data = res_sess.json()
        assert "sessions" in s_data

        # Replay Endpoint for Session #1
        res_replay = client.get("/admin/api/sessions/1/replay", auth=auth)
        assert res_replay.status_code == 200
        rep_data = res_replay.json()
        assert "session" in rep_data
        assert "messages" in rep_data
        assert len(rep_data["messages"]) > 0

        # Reports Endpoint
        res_reports = client.get("/admin/api/reports", auth=auth)
        assert res_reports.status_code == 200
        r_data = res_reports.json()
        assert "reports" in r_data
        assert len(r_data["reports"]) > 0
        first_rep_id = r_data["reports"][0]["id"]

        # Dismiss Report
        res_rep_stat = client.post(
            f"/admin/api/reports/{first_rep_id}/status",
            json={"status": "dismissed"},
            auth=auth,
        )
        assert res_rep_stat.status_code == 200
        assert res_rep_stat.json()["status"] == "dismissed"

        print("  -> Admin Console HTML, KPIs, Users, Sessions, Replay & Reports API passed!")


async def main():
    print("=================================================================")
    print("Running Full System Verification for Telegram Anon Chat...")
    print("=================================================================")
    await test_database_and_models()
    await test_security_engine()
    await test_matchmaker_logic()
    test_fastapi_and_admin_endpoints()
    print("=================================================================")
    print("🎉 ALL TESTS PASSED SUCCESSFULLY! ZERO ERRORS DETECTED.")
    print("=================================================================")


if __name__ == "__main__":
    asyncio.run(main())

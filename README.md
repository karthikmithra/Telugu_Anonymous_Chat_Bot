# 💬 Anonymous 1-on-1 Telegram Chat Bot & Web Admin Console

A production-ready, free-tier-optimized 1-on-1 Anonymous Telegram Chat Bot and sleek Dark-Mode Web Admin Console built with **Python 3.11+**, **FastAPI**, **Aiogram 3**, and **Async SQLAlchemy 2.0**.

Designed specifically to run 24/7 at **$0.00/month** on cloud free tiers (**Neon PostgreSQL** + **Render** + **UptimeRobot**) while handling up to 5,000 active users with zero-cost, in-memory anti-spam defenses.

---

## 🌟 Key Features

### 🛡️ Production Security & Anti-Bot Defense
- **Webhook Secret Verification:** Strict `X-Telegram-Bot-Api-Secret-Token` validation (HTTP 403 on tampering).
- **Global Guard Middleware:** Silent drop for hard-banned users (zero CPU/API waste) and remaining cooldown alert.
- **Emoji CAPTCHA Human Verification:** Unverified users must tap a randomly requested emoji before unlocking matchmaking.
- **New User Quarantine Filter:** Accounts with `< 5 completed chats` or `abuse_score >= 3` are prohibited from sending URLs, domains, Telegram `@usernames`, and forwarded messages. Violations add `+2 abuse_score` and drop the message.
- **In-Memory Sliding-Window Rate Limiter (`collections.deque`):**
  - **3-second cooldown** between `/find` or `/next` commands.
  - **12 skips per 10 minutes cap:** Breaching immediately triggers an automatic **30-minute cooldown** and `+5 abuse_score`.
  - **Flood protector:** Caps chat messages at **8 messages per 10 seconds** to stop sticker/script flooding.
- **Dual-Queue Matchmaking & Shadowban Purgatory:**
  - `normal_queue`: Verified, trustworthy users.
  - `shadow_queue`: Users with `is_shadowbanned == True` or `abuse_score >= 10`. They match **only** with other shadowbanned bots/spammers without ever realizing they are isolated.
- **Anti-Brigade Reporting:** `/report` only increments the offender's `report_count` if the reporter has at least 2 completed chats.
- **Anonymous Relay:** Relays messages using `bot.copy_message()`, stripping all forwarded headers. Supports photos, video, audio, voice notes, stickers, animations, and documents.
- **Permanent Visual Dump Channel:** Optional silent forwarding to a private Telegram channel for instant media moderation.

### 🖥️ Web Admin Console (`/admin`)
- **Single-Page Application:** Built with Tailwind CSS CDN and Alpine.js (zero node_modules build step required).
- **HTTP Basic Authentication:** Protected by `ADMIN_USER` and `ADMIN_PASS` with timing-attack resistant verification.
- **Real-Time KPI Cards:** Total Users, Active Chats Now, Normal Queue Size, Shadow Queue Size, and Open Reports.
- **User Management Tab:** Search by Telegram ID or `@username`, sort by abuse score, skips, or activity. 1-click **Ban / Unban**, **Shadowban / Unshadow**, and **Reset Cooldown & Abuse Score**.
- **Chat Replay Tab & Modal:**
  - Paginated list of sessions with duration, message count, and shadow status.
  - Two-sided chat bubble replay (User A on left in emerald, User B on right in indigo).
  - Exact timestamps for every message.
  - **Secure Media Proxy (`/admin/media/{file_id}`):** Streams Telegram photos, voice notes, audio, stickers, and videos directly in the admin modal without leaking `BOT_TOKEN`.
  - 1-click **Ban User A** / **Ban User B** buttons directly inside the replay modal.
- **Reports Moderation Tab:** Real-time stream of reported incidents with quick session replay, 1-click offender ban, and report dismissal.

---

## 📁 Project Structure

```
├── .env.example       # Sample configuration template
├── .env               # Local configuration file (git-ignored)
├── requirements.txt   # Pinned Python dependencies
├── database.py        # Async SQLAlchemy engine, session maker, and 4 models
├── security.py        # Sliding-window rate limiter, Emoji CAPTCHA, quarantine filter
├── bot.py             # Aiogram 3 router, middleware, dual-queue matchmaker & media relay
├── admin.py           # FastAPI Web Admin Console, Chat Replay UI & secure media proxy
├── main.py            # FastAPI entry point, lifespan, health check, webhook & polling
└── README.md          # 100% Free deployment and configuration guide
```

---

## 🗄️ Database Schema

The database consists of 4 normalized tables supporting PostgreSQL (via `asyncpg`) and SQLite (via `aiosqlite`):

1. **`users`**:
   - `telegram_id` (BigInteger, Primary Key)
   - `username`, `first_name` (Strings, nullable)
   - `is_verified` (Boolean) — passed Emoji CAPTCHA
   - `is_banned` (Boolean) — hard ban (silent drop)
   - `is_shadowbanned` (Boolean) — routed to shadow queue
   - `cooldown_until` (DateTime, nullable)
   - `total_chats`, `total_messages`, `next_press_count`, `report_count`, `abuse_score` (Integers)
   - `created_at`, `last_active` (DateTimes)

2. **`chat_sessions`**:
   - `id` (Integer, Primary Key, autoincrement)
   - `user_a_id`, `user_b_id` (ForeignKeys to `users.telegram_id`)
   - `is_shadow_session` (Boolean)
   - `started_at`, `ended_at` (DateTimes)
   - `ended_by` (BigInteger, nullable)

3. **`messages`**:
   - `id` (Integer, Primary Key, autoincrement)
   - `session_id` (ForeignKey to `chat_sessions.id`)
   - `sender_id` (BigInteger)
   - `msg_type` (text, photo, video, sticker, voice, document, animation)
   - `text_content` (Text, nullable)
   - `file_id` (String, nullable) — Telegram permanent file ID
   - `timestamp` (DateTime)

4. **`reports`**:
   - `id` (Integer, Primary Key, autoincrement)
   - `session_id` (ForeignKey to `chat_sessions.id`)
   - `reporter_id`, `reported_id` (BigIntegers)
   - `reason`, `status` (Strings)
   - `created_at` (DateTime)

---

## 🚀 100% Free-Tier Cloud Deployment Guide

Follow these steps to deploy your bot and admin console permanently for **$0.00/month**.

### Step 1: Create a Free Serverless PostgreSQL Database on Neon
1. Go to [neon.tech](https://neon.tech) and sign up for a free account.
2. Click **Create Project** (e.g., `anonchat-db`).
3. Neon will display your **Connection Details**.
4. Copy the connection string. It looks like:
   ```
   postgresql://user:password@ep-xyz.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
   *(Note: The application automatically translates `postgresql://` and `sslmode=require` to `postgresql+asyncpg://` and `ssl=require`).*

---

### Step 2: Create a Telegram Bot with @BotFather
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, name your bot (e.g., `MyAnonChatBot`), and choose a username ending in `bot`.
3. Copy the **HTTP API Token** (e.g., `7123456789:ABCdefGHIjkl...`).
4. Set bot commands via BotFather:
   ```
   find - Find an anonymous chat partner
   next - Skip to next partner
   stop - End current chat
   report - Report partner for violations
   ```

---

### Step 3: Deploy to Render.com Web Service
1. Push this project to your GitHub repository.
2. Sign in to [render.com](https://render.com) and click **New +** -> **Web Service**.
3. Connect your GitHub repository.
4. Fill in the service configuration:
   - **Name:** `anonchat-bot` (or any custom name)
   - **Region:** Closest to your database (e.g., Oregon / Ohio / Frankfurt)
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:**
     ```bash
     pip install -r requirements.txt
     ```
   - **Start Command:**
     ```bash
     uvicorn main:app --host 0.0.0.0 --port $PORT
     ```
   - **Plan:** Select **Free** (0.1 CPU, 512 MB RAM, 750 free hours/month).

5. In the **Environment Variables** section, add:
   | Variable | Value | Description |
   | :--- | :--- | :--- |
   | `BOT_TOKEN` | `7123456789:ABC...` | From BotFather |
   | `DATABASE_URL` | `postgresql://user:pass@ep-...neon.tech/neondb?sslmode=require` | From Neon |
   | `WEBHOOK_URL` | `https://anonchat-bot.onrender.com` | Your Render app URL (no trailing slash) |
   | `WEBHOOK_SECRET` | `A_long_random_secret_token_123` | Secret for header verification |
   | `ADMIN_USER` | `admin` | Web Admin login username |
   | `ADMIN_PASS` | `YourStrongAdminPassword!` | Web Admin login password |
   | `MEDIA_DUMP_CHANNEL_ID` | `-1001234567890` | *(Optional)* Channel ID for media dump |

6. Click **Deploy Web Service**.
7. Once deployed, Render will show `Your service is live 🎉`.
8. The bot will automatically configure Telegram's webhook to:
   `https://anonchat-bot.onrender.com/webhook` with secret verification!

---

### Step 4: Prevent Sleeping with UptimeRobot (Free 24/7 Keep-Alive)
Render's free tier spins down web services after 15 minutes of inactivity. Pinging `/health` keeps it active 24/7:

1. Sign up for free at [uptimerobot.com](https://uptimerobot.com).
2. Click **+ Add New Monitor**.
3. Set:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `AnonChat Render Health`
   - **URL (or IP):** `https://anonchat-bot.onrender.com/health`
   - **Monitoring Interval:** `5 minutes` (or 10 minutes)
4. Click **Create Monitor**.
5. UptimeRobot will now ping `/health` continuously, keeping your Render free service and Neon DB warm and responsive 24/7 without delays!

---

## 💻 Local Development & Testing

You can run and test the bot locally with zero external setup using the built-in SQLite fallback and automatic Long Polling mode (no ngrok required):

1. **Clone and enter project directory:**
   ```bash
   cd /path/to/telegram-anon-chat
   ```

2. **Create a virtual environment and install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure `.env` for local testing:**
   ```env
   BOT_TOKEN=your_test_bot_token_from_botfather
   DATABASE_URL=sqlite+aiosqlite:///./anonchat.db
   WEBHOOK_URL=
   WEBHOOK_SECRET=local_dev_secret_token
   ADMIN_USER=admin
   ADMIN_PASS=admin123
   ```
   *(Leaving `WEBHOOK_URL` empty automatically switches the bot to Telegram Long Polling mode!)*

4. **Start the application:**
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

5. **Access the services:**
   - **Web Admin Console:** [http://localhost:8000/admin](http://localhost:8000/admin) (Login: `admin` / `admin123`)
   - **Health Keep-Alive:** [http://localhost:8000/health](http://localhost:8000/health)
   - **Interactive API Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
   - **Telegram Bot:** Open your bot on Telegram and send `/start`!

---

## 🛡️ Anti-Spam & Shadowban Mechanics Explained

```
                     Incoming User
                          │
                   Is User Banned?
                    /           \
                 [Yes]          [No]
                  │               │
            Silent Drop       In Cooldown?
          (0 CPU waste)        /        \
                            [Yes]       [No]
                             │            │
                         Show alert   Passed CAPTCHA?
                          & return     /          \
                                    [No]         [Yes]
                                     │             │
                                Send Emoji   Abuse >= 10 OR
                                 CAPTCHA      Shadowbanned?
                                               /         \
                                            [Yes]        [No]
                                             │             │
                                        Shadow Queue   Normal Queue
                                        (Purgatory)    (Real Users)
```

1. **Silent Drops:** When a banned user sends an update, the bot middleware stops execution immediately without making any API response call, preventing API quota exhaustion.
2. **Emoji CAPTCHA:** Upon sending `/start` or `/find`, unverified users are given an inline keyboard with 4 random emojis. Only clicking the requested target emoji unlocks matchmaking.
3. **Quarantine Restriction:** New accounts (`total_chats < 5`) are blocked from sending links or usernames. Spammers who post promotional links are penalized with `+2 abuse_score` and their messages are not relayed to partners.
4. **Shadowban Purgatory:** Once `abuse_score >= 10` or an admin sets `is_shadowbanned = True`, the user is invisibly routed to the `shadow_queue`. Spammers chat only with other spammers and automated bots, leaving normal users completely undisturbed.
5. **Skips Protection:** Rapidly pressing `/next` more than 12 times in 10 minutes automatically triggers a 30-minute cooldown and `+5 abuse_score`.

---

## 📄 License
This project is open-source under the MIT License. Feel free to customize and extend it for your community!
# Telugu_Anonymous_Chat_Bot
# Telugu_Anonymous_Chat_Bot

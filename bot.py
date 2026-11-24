"""
bot.py - Aiogram 3 Telegram Bot Router, Middleware, Dual-Queue Matchmaker & Media Relay.
Features:
- Global Guard Middleware: auto-upsert, silent ban drop, cooldown enforcement
- Emoji CAPTCHA verification
- Demographic Registration: Age, Gender, Location + Under-18 Safety Advisory
- Persistent Menu: Find, Next, Stop, Report, Share Link, Premium (VIP)
- Anti-Ad / Anti-Spam quarantine filter
- Sliding-window rate limiting on commands and message flood
- Dual-queue matchmaking (Normal Queue vs. Shadowban Purgatory)
- Anonymous media relay via bot.copy_message()
- Mutual profile reveal sharing with partner consent
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
    Update,
)

from database import (
    User,
    create_chat_session_record,
    create_report_record,
    end_chat_session_record,
    increment_next_press,
    increment_user_abuse,
    log_chat_message,
    mark_user_verified,
    set_user_cooldown,
    update_user_profile,
    upsert_user,
    utcnow,
)
from security import (
    captcha_manager,
    check_quarantine_violation,
    rate_limiter,
)

logger = logging.getLogger("anonbot")

# Optional Dump Channel ID for permanent visual media archiving
MEDIA_DUMP_CHANNEL_ID_RAW = os.getenv("MEDIA_DUMP_CHANNEL_ID", "").strip()
MEDIA_DUMP_CHANNEL_ID: Optional[int] = (
    int(MEDIA_DUMP_CHANNEL_ID_RAW) if MEDIA_DUMP_CHANNEL_ID_RAW.lstrip("-").isdigit() else None
)


# ==============================================================================
# FSM Registration States
# ==============================================================================

class RegistrationStates(StatesGroup):
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_location = State()


# ==============================================================================
# Persistent Main Menu Keyboard
# ==============================================================================

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Returns the persistent 6-button main menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔍 Find Partner"),
                KeyboardButton(text="🔄 Next"),
            ],
            [
                KeyboardButton(text="🛑 Stop Chat"),
                KeyboardButton(text="🚨 Report Partner"),
            ],
            [
                KeyboardButton(text="🔗 Share Link"),
                KeyboardButton(text="⭐ Premium (VIP)"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def is_profile_complete(user: User) -> bool:
    """Checks if user has completed age, gender, and location registration."""
    return user.age is not None and bool(user.gender) and bool(user.location)


# ==============================================================================
# Dual-Queue Matchmaking State
# ==============================================================================

@dataclass
class ActiveChat:
    session_id: int
    partner_id: int
    started_at: datetime
    is_shadow: bool
    reported: bool = False


class Matchmaker:
    """
    Manages in-memory queues and active conversations.
    Thread-safe / asyncio-safe via asyncio.Lock.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self.normal_queue: List[int] = []
        self.shadow_queue: List[int] = []
        self.active_sessions: Dict[int, ActiveChat] = {}

    async def get_active_chat(self, user_id: int) -> Optional[ActiveChat]:
        async with self._lock:
            return self.active_sessions.get(user_id)

    async def is_queued(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self.normal_queue or user_id in self.shadow_queue

    async def leave_queues(self, user_id: int) -> bool:
        async with self._lock:
            removed = False
            if user_id in self.normal_queue:
                self.normal_queue.remove(user_id)
                removed = True
            if user_id in self.shadow_queue:
                self.shadow_queue.remove(user_id)
                removed = True
            return removed

    async def enqueue(self, user_id: int, is_shadow: bool) -> Optional[int]:
        """
        Adds user to appropriate queue or matches them with an existing waiting partner.
        Returns partner_id if matched, else None.
        """
        async with self._lock:
            if user_id in self.normal_queue:
                self.normal_queue.remove(user_id)
            if user_id in self.shadow_queue:
                self.shadow_queue.remove(user_id)

            target_queue = self.shadow_queue if is_shadow else self.normal_queue

            while target_queue:
                candidate = target_queue.pop(0)
                if candidate != user_id:
                    return candidate

            target_queue.append(user_id)
            return None

    async def register_session(
        self, user_a_id: int, user_b_id: int, session_id: int, is_shadow: bool
    ) -> None:
        """Registers a matched active session for both participants."""
        now = utcnow()
        async with self._lock:
            chat_a = ActiveChat(
                session_id=session_id,
                partner_id=user_b_id,
                started_at=now,
                is_shadow=is_shadow,
            )
            chat_b = ActiveChat(
                session_id=session_id,
                partner_id=user_a_id,
                started_at=now,
                is_shadow=is_shadow,
            )
            self.active_sessions[user_a_id] = chat_a
            self.active_sessions[user_b_id] = chat_b

    async def end_session(self, user_id: int) -> Optional[Tuple[int, int]]:
        """
        Ends an active session for the user and their partner.
        Returns: (session_id, partner_id) if an active session existed, else None.
        """
        async with self._lock:
            chat = self.active_sessions.pop(user_id, None)
            if not chat:
                return None
            partner_id = chat.partner_id
            self.active_sessions.pop(partner_id, None)
            return chat.session_id, partner_id

    async def mark_reported(self, user_id: int) -> bool:
        """Flags the active chat as reported. Returns True if successfully marked."""
        async with self._lock:
            chat = self.active_sessions.get(user_id)
            if not chat:
                return False
            if chat.reported:
                return False
            chat.reported = True
            partner_chat = self.active_sessions.get(chat.partner_id)
            if partner_chat:
                partner_chat.reported = True
            return True

    def get_stats(self) -> Tuple[int, int, int]:
        """Returns (active_chats_count, normal_q_size, shadow_q_size)."""
        active_pairs = len(self.active_sessions) // 2
        return active_pairs, len(self.normal_queue), len(self.shadow_queue)


matchmaker = Matchmaker()


# ==============================================================================
# Global Guard Middleware
# ==============================================================================

class GlobalGuardMiddleware(BaseMiddleware):
    """
    1. Auto-upserts user record on any interaction.
    2. Silently drops all updates from hard-banned users (zero CPU/API waste).
    3. Blocks users currently in cooldown with remaining time alert.
    4. Injects db_user into event data.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = None
        if isinstance(event, Message):
            from_user = event.from_user
        elif isinstance(event, CallbackQuery):
            from_user = event.from_user
        elif isinstance(event, Update):
            if event.message:
                from_user = event.message.from_user
            elif event.callback_query:
                from_user = event.callback_query.from_user

        if not from_user or from_user.is_bot:
            return await handler(event, data)

        db_user = await upsert_user(
            telegram_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
        )
        data["db_user"] = db_user

        if db_user.is_banned:
            logger.debug(f"Silently dropped update from banned user {from_user.id}")
            return None

        now = utcnow()
        if db_user.cooldown_until and db_user.cooldown_until > now:
            remaining_seconds = (db_user.cooldown_until - now).total_seconds()
            remaining_minutes = max(1, int(remaining_seconds // 60) + 1)
            msg_text = (
                f"⏳ **Account in Cooldown**\n\n"
                f"Due to spam or skipping violations, your account is temporarily restricted.\n"
                f"Please try again in **{remaining_minutes} minute(s)**."
            )
            if isinstance(event, Message):
                await event.answer(msg_text, parse_mode="Markdown")
            elif isinstance(event, CallbackQuery):
                await event.answer(f"⏳ Cooldown active: {remaining_minutes}m left.", show_alert=True)
            return None

        return await handler(event, data)


# ==============================================================================
# Router & Handlers
# ==============================================================================

router = Router(name="anon_chat_router")


def build_captcha_keyboard(options: List[str]) -> InlineKeyboardMarkup:
    """Builds a 4-button inline keyboard for Emoji CAPTCHA."""
    buttons = [
        InlineKeyboardButton(text=emoji, callback_data=f"captcha:{emoji}") for emoji in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


async def send_captcha_prompt(message: Message, user_id: int) -> None:
    """Sends Emoji CAPTCHA verification prompt."""
    target_emoji, target_name, options = captcha_manager.generate_challenge(user_id)
    text = (
        f"🤖 **Human Verification Required**\n\n"
        f"To keep the community safe and free of spam, please tap the **{target_name}** ({target_emoji}) below:"
    )
    keyboard = build_captcha_keyboard(options)
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


# ------------------------------------------------------------------------------
# CAPTCHA Callback Query Handler
# ------------------------------------------------------------------------------

@router.callback_query(F.data.startswith("captcha:"))
async def handle_captcha_callback(
    callback: CallbackQuery, db_user: User, state: FSMContext
) -> None:
    selected_emoji = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    is_correct = captcha_manager.verify_answer(user_id, selected_emoji)
    if is_correct:
        await mark_user_verified(user_id)
        await callback.answer("✅ Verification successful!", show_alert=True)

        if not is_profile_complete(db_user):
            # Prompt Step 1: Age
            await state.set_state(RegistrationStates.waiting_for_age)
            if callback.message:
                await callback.message.edit_text(
                    "✅ **Verification Complete!**\n\n"
                    "📝 **Profile Setup (Step 1 of 3)**\n\n"
                    "Please enter your **Age** (numbers only, e.g. `21`):",
                    parse_mode="Markdown",
                )
        else:
            if callback.message:
                await callback.message.edit_text(
                    "✅ **Verification Complete!**\n\n"
                    "Welcome back! Use the menu below to start chatting.",
                    parse_mode="Markdown",
                )
            await callback.message.answer(
                "💬 Choose an option:", reply_markup=get_main_menu_keyboard()
            )
    else:
        await callback.answer("❌ Incorrect emoji! Please try again.", show_alert=True)
        target_emoji, target_name, options = captcha_manager.generate_challenge(user_id)
        keyboard = build_captcha_keyboard(options)
        if callback.message:
            await callback.message.edit_text(
                f"🤖 **Human Verification Required**\n\n"
                f"Oops! That was incorrect. Please tap the **{target_name}** ({target_emoji}) below:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )


# ------------------------------------------------------------------------------
# Registration Step 1: Age (+ Under 18 Safety Warning)
# ------------------------------------------------------------------------------

@router.message(RegistrationStates.waiting_for_age)
async def process_reg_age(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        age = int(text)
        if age < 10 or age > 99:
            await message.answer("Please enter a realistic age between 10 and 99:")
            return
    except ValueError:
        await message.answer("❌ Please enter your age as a number (e.g. `21`):", parse_mode="Markdown")
        return

    await state.update_data(age=age)

    # Under-18 Safety Advisory Warning
    if age < 18:
        warning_text = (
            "⚠️ **UNDER-18 SAFETY ADVISORY** ⚠️\n\n"
            "You have registered as a minor. To ensure your protection:\n"
            "• **NEVER** share your real name, phone number, address, or school.\n"
            "• **NEVER** send private photos or agree to meet anyone in person.\n"
            "• Use **🚨 Report Partner** immediately if anyone acts inappropriately.\n"
            "• Exploitation or harassment will be reported directly to law enforcement."
        )
        await message.answer(warning_text, parse_mode="Markdown")

    # Step 2: Gender Selection
    gender_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Male", callback_data="reg_gender:Male"),
                InlineKeyboardButton(text="👩 Female", callback_data="reg_gender:Female"),
            ],
            [
                InlineKeyboardButton(text="🌈 Other / Secret", callback_data="reg_gender:Other"),
            ],
        ]
    )
    await state.set_state(RegistrationStates.waiting_for_gender)
    await message.answer(
        "👤 **Profile Setup (Step 2 of 3)**\n\nPlease select your **Gender**:",
        reply_markup=gender_keyboard,
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------------------
# Registration Step 2: Gender
# ------------------------------------------------------------------------------

@router.callback_query(RegistrationStates.waiting_for_gender, F.data.startswith("reg_gender:"))
async def process_reg_gender(callback: CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.split(":", 1)[1]
    await state.update_data(gender=gender)
    await callback.answer()

    # Step 3: Location Selection
    loc_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Hyderabad", callback_data="reg_loc:Hyderabad"),
                InlineKeyboardButton(text="Visakhapatnam", callback_data="reg_loc:Visakhapatnam"),
            ],
            [
                InlineKeyboardButton(text="Vijayawada", callback_data="reg_loc:Vijayawada"),
                InlineKeyboardButton(text="Warangal", callback_data="reg_loc:Warangal"),
            ],
            [
                InlineKeyboardButton(text="Tirupati", callback_data="reg_loc:Tirupati"),
                InlineKeyboardButton(text="Bangalore", callback_data="reg_loc:Bangalore"),
            ],
            [
                InlineKeyboardButton(text="Chennai", callback_data="reg_loc:Chennai"),
                InlineKeyboardButton(text="Other / Global", callback_data="reg_loc:Other / Global"),
            ],
        ]
    )
    await state.set_state(RegistrationStates.waiting_for_location)
    if callback.message:
        await callback.message.edit_text(
            f"✅ Gender set: **{gender}**\n\n"
            "📍 **Profile Setup (Step 3 of 3)**\n\n"
            "Please select your **City / Region** below (or type your city):",
            reply_markup=loc_keyboard,
            parse_mode="Markdown",
        )


# ------------------------------------------------------------------------------
# Registration Step 3: Location (Callback or Text)
# ------------------------------------------------------------------------------

async def finish_registration(user_id: int, state: FSMContext, location: str, message: Message) -> None:
    data = await state.get_data()
    age = data.get("age", 18)
    gender = data.get("gender", "Not Specified")

    await update_user_profile(
        telegram_id=user_id,
        age=age,
        gender=gender,
        location=location,
    )
    await state.clear()

    summary = (
        "🎉 **Registration Complete!**\n\n"
        "👤 **Your Profile:**\n"
        f"• **Age:** {age}\n"
        f"• **Gender:** {gender}\n"
        f"• **Location:** {location}\n\n"
        "🚀 You are ready to chat anonymously! Use the menu below to find a partner."
    )
    await message.answer(summary, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")


@router.callback_query(RegistrationStates.waiting_for_location, F.data.startswith("reg_loc:"))
async def process_reg_loc_callback(callback: CallbackQuery, state: FSMContext) -> None:
    location = callback.data.split(":", 1)[1]
    await callback.answer()
    if callback.message:
        await callback.message.delete()
        await finish_registration(callback.from_user.id, state, location, callback.message)


@router.message(RegistrationStates.waiting_for_location)
async def process_reg_loc_text(message: Message, state: FSMContext) -> None:
    location = (message.text or "Other").strip()
    await finish_registration(message.from_user.id, state, location, message)


# ------------------------------------------------------------------------------
# /start Command
# ------------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, db_user: User, state: FSMContext) -> None:
    user_id = message.from_user.id

    if not db_user.is_verified:
        await send_captcha_prompt(message, user_id)
        return

    if not is_profile_complete(db_user):
        await state.set_state(RegistrationStates.waiting_for_age)
        await message.answer(
            "📝 **Profile Registration (Step 1 of 3)**\n\n"
            "Please enter your **Age** (e.g. `21`):",
            parse_mode="Markdown",
        )
        return

    text = (
        f"👋 **Welcome to Telugu Anonymous Chat!**\n\n"
        f"Connect and chat anonymously with random partners.\n\n"
        f"👤 **Your Profile:** {db_user.age} yrs • {db_user.gender} • {db_user.location}\n\n"
        f"✨ **Use the menu below to navigate:**\n"
        f"• 🔍 **Find Partner** — Search for someone to chat with\n"
        f"• 🔄 **Next** — Skip to a new partner\n"
        f"• 🛑 **Stop Chat** — End the conversation\n"
        f"• 🚨 **Report Partner** — Report violations\n"
        f"• 🔗 **Share Link** — Share bot or reveal profile\n"
        f"• ⭐ **Premium (VIP)** — Priority matching & features\n\n"
        f"🔒 *100% Anonymous: Forward headers and identity are stripped.*"
    )
    await message.answer(text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")


# ------------------------------------------------------------------------------
# Matchmaking Core Helper
# ------------------------------------------------------------------------------

async def start_matchmaking_for_user(bot: Bot, message: Message, db_user: User) -> None:
    user_id = message.from_user.id
    is_shadow = db_user.is_shadowbanned or (db_user.abuse_score >= 10)

    partner_id = await matchmaker.enqueue(user_id, is_shadow=is_shadow)

    if partner_id is not None:
        session_id = await create_chat_session_record(
            user_a_id=partner_id,
            user_b_id=user_id,
            is_shadow_session=is_shadow,
        )
        await matchmaker.register_session(
            user_a_id=partner_id,
            user_b_id=user_id,
            session_id=session_id,
            is_shadow=is_shadow,
        )

        match_banner = (
            "🎉 **Partner found! Say hello.**\n\n"
            "• Use the buttons below or send /next to switch partner\n"
            "• Send /stop to end conversation\n"
            "• Send /report to report violation"
        )
        try:
            await bot.send_message(
                partner_id, match_banner, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify partner {partner_id}: {e}")

        await message.answer(
            match_banner, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
        )
    else:
        await message.answer(
            "🔎 **Searching for an anonymous partner...**\nPlease wait a moment.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown",
        )


# ------------------------------------------------------------------------------
# /find or "🔍 Find Partner"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["🔍 Find Partner", "/find"]))
async def cmd_find(message: Message, bot: Bot, db_user: User, state: FSMContext) -> None:
    user_id = message.from_user.id

    if not db_user.is_verified:
        await send_captcha_prompt(message, user_id)
        return

    if not is_profile_complete(db_user):
        await state.set_state(RegistrationStates.waiting_for_age)
        await message.answer(
            "📝 Please complete your profile first:\nEnter your **Age** (e.g. `21`):",
            parse_mode="Markdown",
        )
        return

    allowed, wait_sec = rate_limiter.check_command_cooldown(user_id)
    if not allowed:
        await message.answer(f"⏳ Please wait {wait_sec}s before searching again.")
        return

    active_chat = await matchmaker.get_active_chat(user_id)
    if active_chat:
        await message.answer(
            "💬 You are already in an active chat!\nUse 🔄 **Next** to switch or 🛑 **Stop** to end.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    if await matchmaker.is_queued(user_id):
        await message.answer(
            "🔎 You are already in the queue. Looking for a partner...",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    await start_matchmaking_for_user(bot, message, db_user)


# ------------------------------------------------------------------------------
# /next or "🔄 Next"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["🔄 Next", "/next"]))
async def cmd_next(message: Message, bot: Bot, db_user: User, state: FSMContext) -> None:
    user_id = message.from_user.id

    if not db_user.is_verified:
        await send_captcha_prompt(message, user_id)
        return

    if not is_profile_complete(db_user):
        await state.set_state(RegistrationStates.waiting_for_age)
        await message.answer(
            "📝 Please complete your profile first:\nEnter your **Age** (e.g. `21`):",
            parse_mode="Markdown",
        )
        return

    allowed, wait_sec = rate_limiter.check_command_cooldown(user_id)
    if not allowed:
        await message.answer(f"⏳ Please wait {wait_sec}s before requesting next partner.")
        return

    next_allowed, press_count, max_presses = rate_limiter.record_and_check_next_press(user_id)
    if not next_allowed:
        cooldown_until = utcnow() + timedelta(minutes=30)
        await set_user_cooldown(user_id, cooldown_until=cooldown_until, abuse_delta=5)

        ended = await matchmaker.end_session(user_id)
        if ended:
            session_id, partner_id = ended
            await end_chat_session_record(session_id, ended_by=user_id)
            try:
                await bot.send_message(
                    partner_id,
                    "👋 Your partner skipped the chat. Tap 🔍 Find Partner to find another.",
                    reply_markup=get_main_menu_keyboard(),
                )
            except Exception:
                pass

        await message.answer(
            f"⚠️ **Rate Limit Exceeded**\n\n"
            f"You have skipped {press_count} times in 10 minutes (maximum {max_presses}).\n"
            f"Your account is placed on **30 minutes cooldown**.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    await increment_next_press(user_id)

    ended = await matchmaker.end_session(user_id)
    if ended:
        session_id, partner_id = ended
        await end_chat_session_record(session_id, ended_by=user_id)
        try:
            await bot.send_message(
                partner_id,
                "👋 Your partner skipped to the next chat. Tap 🔍 Find Partner to find another.",
                reply_markup=get_main_menu_keyboard(),
            )
        except Exception:
            pass

    await matchmaker.leave_queues(user_id)
    await message.answer("🔄 **Skipping to next partner...**", parse_mode="Markdown")
    await start_matchmaking_for_user(bot, message, db_user)


# ------------------------------------------------------------------------------
# /stop or "🛑 Stop Chat"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["🛑 Stop Chat", "/stop"]))
async def cmd_stop(message: Message, bot: Bot, db_user: User) -> None:
    user_id = message.from_user.id

    ended = await matchmaker.end_session(user_id)
    if ended:
        session_id, partner_id = ended
        await end_chat_session_record(session_id, ended_by=user_id)
        try:
            await bot.send_message(
                partner_id,
                "👋 Your chat partner ended the conversation. Tap 🔍 Find Partner to chat again.",
                reply_markup=get_main_menu_keyboard(),
            )
        except Exception:
            pass
        await message.answer(
            "🛑 **Conversation ended.**\nTap 🔍 Find Partner whenever you want to chat again.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    removed = await matchmaker.leave_queues(user_id)
    if removed:
        await message.answer(
            "🛑 Matchmaking stopped. Tap 🔍 Find Partner to search again.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    await message.answer(
        "You are not currently in a chat. Tap 🔍 Find Partner to find someone to talk to!",
        reply_markup=get_main_menu_keyboard(),
    )


# ------------------------------------------------------------------------------
# /report or "🚨 Report Partner"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["🚨 Report Partner", "/report"]))
async def cmd_report(message: Message, bot: Bot, db_user: User) -> None:
    user_id = message.from_user.id

    active_chat = await matchmaker.get_active_chat(user_id)
    if not active_chat:
        await message.answer(
            "⚠️ You can only report during an active conversation.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    marked = await matchmaker.mark_reported(user_id)
    if not marked:
        await message.answer(
            "⚠️ This session has already been reported.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    partner_id = active_chat.partner_id
    session_id = active_chat.session_id

    report_id, inc = await create_report_record(
        session_id=session_id,
        reporter_id=user_id,
        reported_id=partner_id,
        reason="Reported via menu/command",
    )

    await matchmaker.end_session(user_id)
    await end_chat_session_record(session_id, ended_by=user_id)

    try:
        await bot.send_message(
            partner_id,
            "🛑 Your chat partner has ended the chat.",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception:
        pass

    await message.answer(
        "🚨 **Report Submitted**\n\n"
        "Thank you for keeping our community safe. Our admin team will review the chat logs.\n"
        "Your chat has been ended. Tap 🔍 Find Partner to search for someone new.",
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------------------
# /link or "🔗 Share Link"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["🔗 Share Link", "/link"]))
async def cmd_link(message: Message, bot: Bot, db_user: User) -> None:
    user_id = message.from_user.id
    me = await bot.get_me()
    bot_link = f"https://t.me/{me.username}?start=ref_{user_id}"

    active_chat = await matchmaker.get_active_chat(user_id)

    text = (
        f"🔗 **Your Anonymous Bot Invite Link:**\n\n"
        f"`{bot_link}`\n\n"
        f"Share this link with your friends to invite them to chat!"
    )

    if active_chat:
        text += (
            f"\n\n🤝 **Reveal Profile in Chat:**\n"
            f"If you and your partner want to reveal your identities and stay in touch outside the bot, "
            f"tap below to send your profile link to your partner."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤝 Share My Profile With Partner",
                        callback_data="reveal_profile_partner",
                    )
                ]
            ]
        )
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")


@router.callback_query(F.data == "reveal_profile_partner")
async def handle_reveal_profile(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    active_chat = await matchmaker.get_active_chat(user_id)

    if not active_chat:
        await callback.answer("⚠️ You are no longer in an active chat.", show_alert=True)
        return

    partner_id = active_chat.partner_id
    user = callback.from_user
    username_text = f"@{user.username}" if user.username else f"Telegram ID: `{user.id}`"

    # Notify partner
    try:
        await bot.send_message(
            partner_id,
            f"🤝 **Your chat partner revealed their identity:**\n\n"
            f"• **Name:** {user.first_name}\n"
            f"• **Handle:** {username_text}\n"
            f"• **Direct Link:** [Open Profile](tg://user?id={user.id})\n\n"
            f"*(Feel free to tap 🔗 Share Link to share yours back!)*",
            parse_mode="Markdown",
        )
        await callback.answer("✅ Profile shared with your partner!", show_alert=True)
    except Exception as e:
        logger.error(f"Error revealing profile: {e}")
        await callback.answer("❌ Could not deliver profile link.", show_alert=True)


# ------------------------------------------------------------------------------
# /premium or "⭐ Premium (VIP)"
# ------------------------------------------------------------------------------

@router.message(F.text.in_(["⭐ Premium (VIP)", "/premium"]))
async def cmd_premium(message: Message, bot: Bot, db_user: User) -> None:
    me = await bot.get_me()
    status_badge = "👑 **ACTIVE VIP MEMBER**" if db_user.is_premium else "⭐ **Standard Member**"

    text = (
        f"⭐ **Telugu AnonChat VIP Premium** ⭐\n\n"
        f"Current Status: {status_badge}\n\n"
        f"Upgrade your anonymous chat experience with exclusive VIP features:\n\n"
        f"⚡ **Priority Matchmaking Queue**\n"
        f"Skip all waiting queues and match with partners instantly!\n\n"
        f"🚻 **Gender Preference Filter**\n"
        f"Choose to match specifically with Girls or Boys.\n\n"
        f"📍 **Location / City Matching**\n"
        f"Find partners specifically from Hyderabad, Vizag, Vijayawada, etc.\n\n"
        f"🛡️ **Unrestricted Links & Media**\n"
        f"Send Instagram, Telegram, and web links without quarantine restrictions.\n\n"
        f"👑 **Golden VIP Badge**\n"
        f"Distinguished profile badge in chat replays and community.\n\n"
        f"💰 **Subscription Plans:**\n"
        f"• 1 Month: **₹99** / $1.99\n"
        f"• 3 Months: **₹249** / $4.99\n"
        f"• Lifetime: **₹599** / $9.99\n\n"
        f"👇 Tap below to contact admin and activate VIP instantly:"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👑 Contact Admin to Activate VIP",
                    url=f"https://t.me/{me.username}",
                )
            ]
        ]
    )
    await message.answer(text, reply_markup=markup, parse_mode="Markdown")


# ------------------------------------------------------------------------------
# Message Relay & Media Logger
# ------------------------------------------------------------------------------

@router.message()
async def relay_message_handler(message: Message, bot: Bot, db_user: User) -> None:
    user_id = message.from_user.id

    active_chat = await matchmaker.get_active_chat(user_id)
    if not active_chat:
        if await matchmaker.is_queued(user_id):
            await message.answer("🔎 Still searching for a partner... Please wait a moment.")
        else:
            await message.answer(
                "You are not currently in a chat.\nTap 🔍 Find Partner to find someone to talk to!",
                reply_markup=get_main_menu_keyboard(),
                parse_mode="Markdown",
            )
        return

    partner_id = active_chat.partner_id
    session_id = active_chat.session_id

    # Flood limiter
    flood_allowed, count = rate_limiter.check_message_flood(user_id)
    if not flood_allowed:
        await message.answer("⚠️ **Slow down!** You are sending messages too fast.")
        return

    # Quarantine Filter (skipped for premium users)
    if not db_user.is_premium:
        has_forward = bool(
            message.forward_origin
            or message.forward_date
            or message.forward_from
            or message.forward_from_chat
        )
        msg_text_content = message.text or message.caption or ""

        quarantine_violation = check_quarantine_violation(
            text=msg_text_content,
            has_forward=has_forward,
            total_chats=db_user.total_chats,
            abuse_score=db_user.abuse_score,
        )
        if quarantine_violation:
            await increment_user_abuse(user_id, score_delta=2)
            await message.answer(
                f"🚫 **Message Blocked**\n\n"
                f"{quarantine_violation}\n"
                f"*(Abuse penalty +2)*\n"
                f"*(Upgrade to ⭐ Premium to bypass link restrictions)*",
                parse_mode="Markdown",
            )
            return

    msg_type = "text"
    file_id = None
    msg_text_content = message.text or message.caption or ""

    if message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        msg_type = "video"
        file_id = message.video.file_id
    elif message.sticker:
        msg_type = "sticker"
        file_id = message.sticker.file_id
    elif message.voice:
        msg_type = "voice"
        file_id = message.voice.file_id
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
    elif message.animation:
        msg_type = "animation"
        file_id = message.animation.file_id
    elif message.audio:
        msg_type = "audio"
        file_id = message.audio.file_id
    elif message.video_note:
        msg_type = "video_note"
        file_id = message.video_note.file_id

    await log_chat_message(
        session_id=session_id,
        sender_id=user_id,
        msg_type=msg_type,
        text_content=msg_text_content if msg_text_content else None,
        file_id=file_id,
    )

    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=user_id,
            message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"Failed to relay message to partner {partner_id}: {e}")
        await message.answer("⚠️ Failed to deliver message to partner. They may have stopped the chat.")

    if MEDIA_DUMP_CHANNEL_ID is not None:
        try:
            await bot.copy_message(
                chat_id=MEDIA_DUMP_CHANNEL_ID,
                from_chat_id=user_id,
                message_id=message.message_id,
            )
        except Exception as e:
            logger.debug(f"Media dump channel copy failed (silent): {e}")


def setup_dispatcher() -> Dispatcher:
    """Creates and configures Aiogram Dispatcher with middleware and routers."""
    dp = Dispatcher()
    dp.update.middleware(GlobalGuardMiddleware())
    if router.parent_router is not None:
        router.parent_router = None
    dp.include_router(router)
    return dp

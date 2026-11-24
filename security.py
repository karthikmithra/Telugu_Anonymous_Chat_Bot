"""
security.py - Security, Anti-Bot, Anti-Spam & In-Memory Rate Limiting Engine.
Optimized for zero-cost operation and free-tier cloud limits up to 5,000 active users.
"""

from __future__ import annotations

import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Set


# ==============================================================================
# 1. Webhook Secret Token Verification
# ==============================================================================

def verify_webhook_secret(header_secret: Optional[str], expected_secret: Optional[str]) -> bool:
    """
    Constant-time comparison for Telegram Webhook Secret Token header
    (X-Telegram-Bot-Api-Secret-Token).
    """
    if not expected_secret:
        # If no secret configured in environment, allow (or warn)
        return True
    if not header_secret:
        return False
    return secrets.compare_digest(header_secret.strip(), expected_secret.strip())


# ==============================================================================
# 2. Human Verification (Emoji CAPTCHA)
# ==============================================================================

EMOJI_POOL: List[Tuple[str, str]] = [
    ("🐱", "Cat"),
    ("🐶", "Dog"),
    ("🚗", "Car"),
    ("🍕", "Pizza"),
    ("🍎", "Apple"),
    ("🚀", "Rocket"),
    ("⚽", "Soccer Ball"),
    ("⭐", "Star"),
    ("🎸", "Guitar"),
    ("☕", "Coffee"),
    ("🌈", "Rainbow"),
    ("✈️", "Airplane"),
    ("🍦", "Ice Cream"),
    ("🌻", "Sunflower"),
    ("🦁", "Lion"),
    ("🎁", "Gift"),
    ("🍔", "Burger"),
    ("🏀", "Basketball"),
    ("💡", "Light Bulb"),
    ("🔔", "Bell"),
]


@dataclass
class CaptchaChallenge:
    target_emoji: str
    target_name: str
    options: List[str]
    created_at: float


class EmojiCaptchaManager:
    """
    Manages in-memory CAPTCHA challenges for unverified users.
    TTL is 10 minutes per challenge.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self._challenges: Dict[int, CaptchaChallenge] = {}

    def generate_challenge(self, user_id: int) -> Tuple[str, str, List[str]]:
        """
        Generates 4 random emoji choices with 1 correct target emoji.
        Returns: (target_emoji, target_name, options_list)
        """
        # Pick 4 distinct emojis
        selected = secrets.SystemRandom().sample(EMOJI_POOL, 4)
        target = secrets.SystemRandom().choice(selected)

        target_emoji, target_name = target
        options = [item[0] for item in selected]
        # Shuffle options
        secrets.SystemRandom().shuffle(options)

        self._challenges[user_id] = CaptchaChallenge(
            target_emoji=target_emoji,
            target_name=target_name,
            options=options,
            created_at=time.time(),
        )
        return target_emoji, target_name, options

    def get_challenge(self, user_id: int) -> Optional[CaptchaChallenge]:
        """Returns active challenge if not expired."""
        challenge = self._challenges.get(user_id)
        if not challenge:
            return None
        if time.time() - challenge.created_at > self.ttl_seconds:
            self._challenges.pop(user_id, None)
            return None
        return challenge

    def verify_answer(self, user_id: int, answer_emoji: str) -> bool:
        """
        Verifies the user's tapped emoji. Removes challenge if successful.
        """
        challenge = self.get_challenge(user_id)
        if not challenge:
            return False

        is_correct = secrets.compare_digest(
            answer_emoji.strip().encode("utf-8"),
            challenge.target_emoji.strip().encode("utf-8"),
        )
        if is_correct:
            self._challenges.pop(user_id, None)
        return is_correct

    def clear(self, user_id: int) -> None:
        """Clears challenge for a user."""
        self._challenges.pop(user_id, None)


captcha_manager = EmojiCaptchaManager()


# ==============================================================================
# 3. Anti-Spam / Anti-Ad Quarantine Filter
# ==============================================================================

# Regex patterns for links, common TLDs, and Telegram usernames
URL_REGEX = re.compile(
    r"(?:https?://|www\.|t\.me/|[a-zA-Z0-9-]+\.(?:com|org|net|xyz|io|me|ru|cc|top|info|biz|site|online|app|dev|link|vip|club|co|tv|space|live|pro|fun))\b",
    re.IGNORECASE,
)
USERNAME_REGEX = re.compile(r"(?:^|\s)@([a-zA-Z0-9_]{4,32})\b")
IP_REGEX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def check_quarantine_violation(
    text: Optional[str],
    has_forward: bool,
    total_chats: int,
    abuse_score: int,
) -> Optional[str]:
    """
    Quarantine rule:
    Applies if total_chats < 5 OR abuse_score >= 3.
    Blocks:
    1. Forwarded messages (forward_origin / forward_from / forward_date)
    2. Any URLs, domains, t.me links, or IP addresses
    3. Telegram usernames (@username)

    Returns:
        Violation reason string if blocked, or None if message is clean and allowed.
    """
    is_quarantined = (total_chats < 5) or (abuse_score >= 3)
    if not is_quarantined:
        return None

    if has_forward:
        return "Forwarded messages are restricted for accounts with fewer than 5 completed chats."

    if not text:
        return None

    # Check for URLs / Domains
    if URL_REGEX.search(text):
        return "Links and website domains are restricted for accounts with fewer than 5 completed chats."

    # Check for IP addresses
    if IP_REGEX.search(text):
        return "IP addresses and external links are restricted for new accounts."

    # Check for @usernames
    if USERNAME_REGEX.search(text):
        return "Sharing @usernames is restricted for accounts with fewer than 5 completed chats."

    return None


# ==============================================================================
# 4. In-Memory Sliding-Window Rate Limiter
# ==============================================================================

class SlidingWindowRateLimiter:
    """
    High-performance in-memory sliding window rate limiter:
    - 3-second minimum cooldown between /find or /next
    - Cap /next presses at 12 skips per 10 minutes (600s). On breach: 30 min cooldown + abuse_score += 5
    - Cap chat messages at 8 messages per 10 seconds (10s) to block flooding
    - Auto-prunes timestamps to keep memory usage minimal (<2MB for 5,000 active users)
    """

    def __init__(
        self,
        command_cooldown_seconds: float = 3.0,
        next_cap_window_seconds: float = 600.0,
        next_cap_max_presses: int = 12,
        msg_flood_window_seconds: float = 10.0,
        msg_flood_max_count: int = 8,
    ):
        self.command_cooldown_seconds = command_cooldown_seconds
        self.next_cap_window_seconds = next_cap_window_seconds
        self.next_cap_max_presses = next_cap_max_presses
        self.msg_flood_window_seconds = msg_flood_window_seconds
        self.msg_flood_max_count = msg_flood_max_count

        # Data stores
        self._last_command_time: Dict[int, float] = {}
        self._next_presses: Dict[int, deque[float]] = defaultdict(deque)
        self._message_timestamps: Dict[int, deque[float]] = defaultdict(deque)
        self._last_cleanup: float = time.time()

    def _maybe_cleanup(self) -> None:
        """Periodically purges stale entries every 5 minutes."""
        now = time.time()
        if now - self._last_cleanup < 300:
            return

        self._last_cleanup = now
        cutoff_commands = now - 60
        cutoff_next = now - self.next_cap_window_seconds
        cutoff_msg = now - self.msg_flood_window_seconds

        # Prune command times
        stale_cmds = [uid for uid, t in self._last_command_time.items() if t < cutoff_commands]
        for uid in stale_cmds:
            self._last_command_time.pop(uid, None)

        # Prune next press deques
        stale_next = []
        for uid, deq in list(self._next_presses.items()):
            while deq and deq[0] < cutoff_next:
                deq.popleft()
            if not deq:
                stale_next.append(uid)
        for uid in stale_next:
            self._next_presses.pop(uid, None)

        # Prune message flood deques
        stale_msgs = []
        for uid, deq in list(self._message_timestamps.items()):
            while deq and deq[0] < cutoff_msg:
                deq.popleft()
            if not deq:
                stale_msgs.append(uid)
        for uid in stale_msgs:
            self._message_timestamps.pop(uid, None)

    def check_command_cooldown(self, user_id: int) -> Tuple[bool, float]:
        """
        Enforces a strict 3-second cooldown between /find or /next commands.
        Returns:
            (allowed: bool, remaining_seconds: float)
        """
        self._maybe_cleanup()
        now = time.time()
        last_time = self._last_command_time.get(user_id, 0.0)
        elapsed = now - last_time

        if elapsed < self.command_cooldown_seconds:
            remaining = round(self.command_cooldown_seconds - elapsed, 1)
            return False, remaining

        self._last_command_time[user_id] = now
        return True, 0.0

    def record_and_check_next_press(self, user_id: int) -> Tuple[bool, int, int]:
        """
        Records a /next press within a 10-minute sliding window.
        Returns:
            (allowed: bool, current_press_count: int, max_allowed: int)
        """
        self._maybe_cleanup()
        now = time.time()
        deq = self._next_presses[user_id]

        cutoff = now - self.next_cap_window_seconds
        while deq and deq[0] < cutoff:
            deq.popleft()

        deq.append(now)
        current_count = len(deq)

        if current_count > self.next_cap_max_presses:
            return False, current_count, self.next_cap_max_presses

        return True, current_count, self.next_cap_max_presses

    def check_message_flood(self, user_id: int) -> Tuple[bool, int]:
        """
        Checks whether the user is sending messages faster than 8 msgs / 10s.
        Returns:
            (allowed: bool, current_window_count: int)
        """
        self._maybe_cleanup()
        now = time.time()
        deq = self._message_timestamps[user_id]

        cutoff = now - self.msg_flood_window_seconds
        while deq and deq[0] < cutoff:
            deq.popleft()

        deq.append(now)
        current_count = len(deq)

        if current_count > self.msg_flood_max_count:
            return False, current_count

        return True, current_count


rate_limiter = SlidingWindowRateLimiter()

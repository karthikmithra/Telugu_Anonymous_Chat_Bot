"""
database.py - Async SQLAlchemy engine, session maker, models, and DB access layer.
Supports PostgreSQL (asyncpg) for cloud hosting (Neon, Supabase, Render) and SQLite (aiosqlite) for local development.
"""

from __future__ import annotations

import os
import socket
import json
import urllib.request
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Graceful DNS fallback for local environments where ISP DNS restricts *.neon.tech
_orig_getaddrinfo = socket.getaddrinfo

def _resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as e:
        if isinstance(host, str) and "neon.tech" in host:
            try:
                req = urllib.request.Request(
                    f"https://dns.google/resolve?name={host}&type=A",
                    headers={"User-Agent": "Python-DoH-Resolver"}
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    data = json.loads(resp.read().decode())
                    for ans in data.get("Answer", []):
                        if ans.get("type") == 1:
                            return _orig_getaddrinfo(ans["data"], port, family, type, proto, flags)
            except Exception:
                pass
        raise e

socket.getaddrinfo = _resilient_getaddrinfo

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    inspect,
    select,
    text,
    update,
    desc,
    and_,
    or_,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Return timezone-naive UTC datetime for consistent database storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_database_url(raw_url: str) -> Tuple[str, bool]:
    """
    Normalizes standard database URLs to async SQLAlchemy compatible drivers:
    - postgres:// or postgresql:// -> postgresql+asyncpg://
    - sqlite:// -> sqlite+aiosqlite://
    - Strips surrounding quotes (" or ') from environment variables
    - Strips unsupported query parameters (sslmode, channel_binding, target_session_attrs) for asyncpg
    - Detects if SSL should be enabled (e.g. Neon, Supabase, or sslmode=require)
    Returns: (cleaned_database_url, needs_ssl)
    """
    if not raw_url:
        return "sqlite+aiosqlite:///./anonchat.db", False

    url = raw_url.strip().strip('"').strip("'")
    needs_ssl = False

    # Rewrite postgres prefix to postgresql+asyncpg
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        url = "sqlite+aiosqlite://" + url[len("sqlite://") :]

    # Handle query params for asyncpg (Neon, Supabase, Render Postgres)
    if url.startswith("postgresql+asyncpg://"):
        if "neon.tech" in url or "supabase" in url:
            needs_ssl = True

        parsed = urlparse(url)
        if parsed.query:
            query_params = parse_qs(parsed.query, keep_blank_values=True)

            if "sslmode" in query_params:
                ssl_val = query_params.pop("sslmode")[0]
                if ssl_val in ("require", "verify-ca", "verify-full"):
                    needs_ssl = True
            if "ssl" in query_params:
                ssl_arg = query_params.pop("ssl")[0]
                if ssl_arg.lower() in ("require", "true", "1"):
                    needs_ssl = True

            # Strip unsupported libpq parameters for asyncpg
            query_params.pop("channel_binding", None)
            query_params.pop("target_session_attrs", None)

            new_query = urlencode(query_params, doseq=True)
            url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    new_query,
                    parsed.fragment,
                )
            )

    return url, needs_ssl


# Fetch DATABASE_URL from environment or fallback to local SQLite
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./anonchat.db")
DATABASE_URL, NEEDS_SSL = normalize_database_url(RAW_DATABASE_URL)

# Configure engine arguments depending on DB dialect
engine_kwargs: Dict[str, Any] = {
    "echo": False,
}

if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    connect_args: Dict[str, Any] = {}
    if NEEDS_SSL:
        connect_args["ssl"] = "require"

    engine_kwargs.update(
        {
            "connect_args": connect_args,
            "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
            "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
            "pool_pre_ping": True,
            "pool_recycle": 300,
        }
    )

engine = create_async_engine(DATABASE_URL, **engine_kwargs)
async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_shadowbanned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_chats: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_press_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    abuse_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_active: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False, index=True
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    user_a_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_b_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_shadow_session: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    ended_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Relationships
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan", order_by="Message.timestamp"
    )
    reports: Mapped[List["Report"]] = relationship(
        "Report", back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    msg_type: Mapped[str] = mapped_column(String(50), nullable=False)  # text, photo, video, sticker, voice, document, animation
    text_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reporter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reported_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="open", nullable=False, index=True)  # open, dismissed, resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="reports")


# Database Initialization & Safe Migrations
def _sync_safe_migrations(sync_conn) -> None:
    """
    Synchronously inspects tables and adds any missing columns defined in Base.metadata
    using ALTER TABLE ADD COLUMN. Works idempotently on both PostgreSQL and SQLite.
    """
    inspector = inspect(sync_conn)

    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue

        existing_cols = {col["name"] for col in inspector.get_columns(table_name)}

        for col in table.columns:
            if col.name not in existing_cols:
                col_type = col.type.compile(sync_conn.dialect)
                default_clause = ""

                if col.server_default is not None and getattr(col.server_default, "arg", None):
                    default_clause = f" DEFAULT {col.server_default.arg}"
                elif col.default is not None and getattr(col.default, "arg", None):
                    val = col.default.arg
                    if isinstance(val, (int, float)):
                        default_clause = f" DEFAULT {val}"
                    elif isinstance(val, str):
                        default_clause = f" DEFAULT '{val}'"
                    elif isinstance(val, bool):
                        default_val = "1" if sync_conn.dialect.name == "sqlite" else ("true" if val else "false")
                        default_clause = f" DEFAULT {default_val}"

                alter_stmt = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"
                if default_clause:
                    alter_stmt += default_clause

                try:
                    sync_conn.execute(text(alter_stmt))
                except Exception:
                    # Ignore if already added or race condition
                    pass


async def run_safe_migrations(async_engine=None) -> None:
    """
    Inspects existing tables and safely executes ALTER TABLE ADD COLUMN for any newly
    defined model columns so live production data is never wiped or broken on deployment.
    """
    target_engine = async_engine or engine
    async with target_engine.begin() as conn:
        await conn.run_sync(_sync_safe_migrations)


async def init_db() -> None:
    """Create tables if they do not exist and apply safe startup migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_safe_migrations(engine)


# FastAPI Session Dependency
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for yielding an async session in FastAPI route handlers."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


# Context Manager for Bot & Internal Services
class get_session:
    """Async context manager for standalone operations."""

    def __init__(self):
        self.session: Optional[AsyncSession] = None

    async def __aenter__(self) -> AsyncSession:
        self.session = async_session_maker()
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            if exc_type is not None:
                await self.session.rollback()
            await self.session.close()


# ==============================================================================
# Database Helper Service Functions
# ==============================================================================

async def upsert_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> User:
    """
    Fetches or creates a user record, updating last_active and latest profile fields.
    """
    async with get_session() as session:
        async with session.begin():
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            now = utcnow()
            if user is None:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    is_verified=False,
                    is_banned=False,
                    is_shadowbanned=False,
                    created_at=now,
                    last_active=now,
                )
                session.add(user)
            else:
                user.last_active = now
                if username is not None:
                    user.username = username
                if first_name is not None:
                    user.first_name = first_name

        await session.refresh(user)
        return user


async def get_user_by_id(telegram_id: int) -> Optional[User]:
    """Retrieves a single user by Telegram ID."""
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


async def mark_user_verified(telegram_id: int) -> None:
    """Marks a user as having passed the Emoji CAPTCHA verification."""
    async with get_session() as session:
        async with session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(is_verified=True, last_active=utcnow())
            )


async def update_user_profile(
    telegram_id: int, age: int, gender: str, location: str
) -> None:
    """Updates user demographic registration profile."""
    async with get_session() as session:
        async with session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(
                    age=age,
                    gender=gender,
                    location=location,
                    last_active=utcnow(),
                )
            )


async def set_user_premium_status(telegram_id: int, is_premium: bool) -> bool:
    """Sets VIP Premium status for a user."""
    async with get_session() as session:
        async with session.begin():
            res = await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(is_premium=is_premium, last_active=utcnow())
            )
            return res.rowcount > 0


async def increment_user_abuse(telegram_id: int, score_delta: int) -> int:
    """Increments user abuse score and returns the updated abuse score."""
    async with get_session() as session:
        async with session.begin():
            stmt = select(User).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.abuse_score += score_delta
                user.last_active = utcnow()
                return user.abuse_score
            return 0


async def set_user_cooldown(telegram_id: int, cooldown_until: datetime, abuse_delta: int = 0) -> None:
    """Sets a cooldown timestamp and optionally increases abuse score."""
    async with get_session() as session:
        async with session.begin():
            stmt = select(User).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.cooldown_until = cooldown_until
                if abuse_delta > 0:
                    user.abuse_score += abuse_delta
                user.last_active = utcnow()


async def increment_next_press(telegram_id: int) -> int:
    """Increments the next_press_count for a user."""
    async with get_session() as session:
        async with session.begin():
            stmt = select(User).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.next_press_count += 1
                return user.next_press_count
            return 0


async def create_chat_session_record(
    user_a_id: int, user_b_id: int, is_shadow_session: bool = False
) -> int:
    """Creates a new active chat session and increments total_chats for both users."""
    async with get_session() as session:
        async with session.begin():
            now = utcnow()
            new_session = ChatSession(
                user_a_id=user_a_id,
                user_b_id=user_b_id,
                is_shadow_session=is_shadow_session,
                started_at=now,
            )
            session.add(new_session)
            await session.flush()
            session_id = new_session.id

            # Increment total_chats on both users
            await session.execute(
                update(User)
                .where(User.telegram_id.in_([user_a_id, user_b_id]))
                .values(
                    total_chats=User.total_chats + 1,
                    last_active=now,
                )
            )
            return session_id


async def end_chat_session_record(session_id: int, ended_by: Optional[int] = None) -> None:
    """Marks a chat session as ended."""
    async with get_session() as session:
        async with session.begin():
            await session.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id, ChatSession.ended_at.is_(None))
                .values(
                    ended_at=utcnow(),
                    ended_by=ended_by,
                )
            )


async def log_chat_message(
    session_id: int,
    sender_id: int,
    msg_type: str,
    text_content: Optional[str] = None,
    file_id: Optional[str] = None,
) -> None:
    """Logs an anonymized message into the messages table and increments user message count."""
    async with get_session() as session:
        async with session.begin():
            now = utcnow()
            msg = Message(
                session_id=session_id,
                sender_id=sender_id,
                msg_type=msg_type,
                text_content=text_content,
                file_id=file_id,
                timestamp=now,
            )
            session.add(msg)
            await session.execute(
                update(User)
                .where(User.telegram_id == sender_id)
                .values(
                    total_messages=User.total_messages + 1,
                    last_active=now,
                )
            )


async def create_report_record(
    session_id: int,
    reporter_id: int,
    reported_id: int,
    reason: str,
) -> Tuple[int, bool]:
    """
    Records a report for a session.
    Only increments reported user's report_count if reporter has total_chats >= 2 to prevent brigade attacks.
    Returns: (report_id, count_incremented_bool)
    """
    async with get_session() as session:
        async with session.begin():
            # Check reporter's total_chats
            rep_stmt = select(User.total_chats).where(User.telegram_id == reporter_id)
            rep_res = await session.execute(rep_stmt)
            reporter_chats = rep_res.scalar_one_or_none() or 0

            count_incremented = False
            if reporter_chats >= 2:
                count_incremented = True
                await session.execute(
                    update(User)
                    .where(User.telegram_id == reported_id)
                    .values(
                        report_count=User.report_count + 1,
                        abuse_score=User.abuse_score + 1,
                    )
                )

            report = Report(
                session_id=session_id,
                reporter_id=reporter_id,
                reported_id=reported_id,
                reason=reason,
                status="open",
                created_at=utcnow(),
            )
            session.add(report)
            await session.flush()
            return report.id, count_incremented


# ==============================================================================
# Admin Dashboard Analytics & Management Queries
# ==============================================================================

async def get_admin_kpis(active_chats_count: int, normal_q_size: int, shadow_q_size: int) -> Dict[str, Any]:
    """Calculates top KPI stats for the Admin Console."""
    async with get_session() as session:
        total_users_stmt = select(func.count(User.telegram_id))
        total_users = (await session.execute(total_users_stmt)).scalar() or 0

        open_reports_stmt = select(func.count(Report.id)).where(Report.status == "open")
        open_reports = (await session.execute(open_reports_stmt)).scalar() or 0

        total_sessions_stmt = select(func.count(ChatSession.id))
        total_sessions = (await session.execute(total_sessions_stmt)).scalar() or 0

        total_messages_stmt = select(func.count(Message.id))
        total_messages = (await session.execute(total_messages_stmt)).scalar() or 0

        banned_users_stmt = select(func.count(User.telegram_id)).where(User.is_banned.is_(True))
        banned_users = (await session.execute(banned_users_stmt)).scalar() or 0

        premium_users_stmt = select(func.count(User.telegram_id)).where(User.is_premium.is_(True))
        premium_users = (await session.execute(premium_users_stmt)).scalar() or 0

        shadowbanned_users_stmt = select(func.count(User.telegram_id)).where(User.is_shadowbanned.is_(True))
        shadowbanned_users = (await session.execute(shadowbanned_users_stmt)).scalar() or 0

        return {
            "total_users": total_users,
            "active_chats_now": active_chats_count,
            "normal_queue_size": normal_q_size,
            "shadow_queue_size": shadow_q_size,
            "open_reports": open_reports,
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "banned_users": banned_users,
            "premium_users": premium_users,
            "shadowbanned_users": shadowbanned_users,
        }


async def get_admin_users(
    query: Optional[str] = None,
    sort_by: str = "last_active",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Retrieves paginated, searchable, and sortable list of users."""
    async with get_session() as session:
        base_stmt = select(User)

        if query:
            q_clean = query.strip()
            if q_clean.isdigit():
                base_stmt = base_stmt.where(
                    or_(
                        User.telegram_id == int(q_clean),
                        User.username.ilike(f"%{q_clean}%"),
                    )
                )
            else:
                clean_name = q_clean.lstrip("@")
                base_stmt = base_stmt.where(
                    or_(
                        User.username.ilike(f"%{clean_name}%"),
                        User.first_name.ilike(f"%{clean_name}%"),
                    )
                )

        # Count total
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_count = (await session.execute(count_stmt)).scalar() or 0

        # Sort column map
        sort_columns = {
            "telegram_id": User.telegram_id,
            "total_chats": User.total_chats,
            "total_messages": User.total_messages,
            "next_press_count": User.next_press_count,
            "report_count": User.report_count,
            "abuse_score": User.abuse_score,
            "is_banned": User.is_banned,
            "created_at": User.created_at,
            "last_active": User.last_active,
        }
        sort_col = sort_columns.get(sort_by, User.last_active)
        if order.lower() == "asc":
            base_stmt = base_stmt.order_by(sort_col.asc())
        else:
            base_stmt = base_stmt.order_by(sort_col.desc())

        base_stmt = base_stmt.offset(offset).limit(limit)
        results = (await session.execute(base_stmt)).scalars().all()

        user_dicts = []
        now = utcnow()
        for u in results:
            in_cooldown = u.cooldown_until is not None and u.cooldown_until > now
            user_dicts.append(
                {
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "is_verified": u.is_verified,
                    "age": u.age,
                    "gender": u.gender,
                    "location": u.location,
                    "is_premium": u.is_premium,
                    "is_banned": u.is_banned,
                    "is_shadowbanned": u.is_shadowbanned,
                    "in_cooldown": in_cooldown,
                    "cooldown_until": u.cooldown_until.isoformat() if u.cooldown_until else None,
                    "total_chats": u.total_chats,
                    "total_messages": u.total_messages,
                    "next_press_count": u.next_press_count,
                    "report_count": u.report_count,
                    "abuse_score": u.abuse_score,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_active": u.last_active.isoformat() if u.last_active else None,
                }
            )

        return user_dicts, total_count


async def set_user_ban_status(telegram_id: int, is_banned: bool) -> bool:
    """Sets hard ban status for a user."""
    async with get_session() as session:
        async with session.begin():
            res = await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(is_banned=is_banned, last_active=utcnow())
            )
            return res.rowcount > 0


async def set_user_shadowban_status(telegram_id: int, is_shadowbanned: bool) -> bool:
    """Sets shadowban status for a user."""
    async with get_session() as session:
        async with session.begin():
            res = await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(is_shadowbanned=is_shadowbanned, last_active=utcnow())
            )
            return res.rowcount > 0


async def reset_user_cooldown_and_abuse(telegram_id: int) -> bool:
    """Resets cooldown timestamp and resets abuse_score to 0."""
    async with get_session() as session:
        async with session.begin():
            res = await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(cooldown_until=None, abuse_score=0, last_active=utcnow())
            )
            return res.rowcount > 0


async def get_admin_sessions(
    limit: int = 50,
    offset: int = 0,
    user_id: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Retrieves paginated chat sessions for the Chat Replay tab."""
    async with get_session() as session:
        base_stmt = select(ChatSession)

        if user_id:
            base_stmt = base_stmt.where(
                or_(ChatSession.user_a_id == user_id, ChatSession.user_b_id == user_id)
            )

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_count = (await session.execute(count_stmt)).scalar() or 0

        # Order by newest started first
        base_stmt = base_stmt.order_by(ChatSession.started_at.desc()).offset(offset).limit(limit)
        results = (await session.execute(base_stmt)).scalars().all()

        session_list = []
        for s in results:
            # Check message count
            msg_count_stmt = select(func.count(Message.id)).where(Message.session_id == s.id)
            msg_count = (await session.execute(msg_count_stmt)).scalar() or 0

            # Check if reported
            rep_stmt = select(func.count(Report.id)).where(Report.session_id == s.id)
            rep_count = (await session.execute(rep_stmt)).scalar() or 0

            # Calculate duration seconds
            duration_seconds = None
            if s.ended_at and s.started_at:
                duration_seconds = int((s.ended_at - s.started_at).total_seconds())

            session_list.append(
                {
                    "id": s.id,
                    "user_a_id": s.user_a_id,
                    "user_b_id": s.user_b_id,
                    "is_shadow_session": s.is_shadow_session,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "ended_by": s.ended_by,
                    "duration_seconds": duration_seconds,
                    "message_count": msg_count,
                    "is_reported": rep_count > 0,
                }
            )

        return session_list, total_count


async def get_session_replay(session_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves full session metadata and ordered messages for replay."""
    async with get_session() as session:
        session_stmt = select(ChatSession).where(ChatSession.id == session_id)
        s = (await session.execute(session_stmt)).scalar_one_or_none()
        if not s:
            return None

        # Fetch users
        user_a = await get_user_by_id(s.user_a_id)
        user_b = await get_user_by_id(s.user_b_id)

        # Fetch messages
        msgs_stmt = select(Message).where(Message.session_id == session_id).order_by(Message.timestamp.asc())
        msgs = (await session.execute(msgs_stmt)).scalars().all()

        # Fetch reports
        reps_stmt = select(Report).where(Report.session_id == session_id)
        reps = (await session.execute(reps_stmt)).scalars().all()

        message_items = []
        for m in msgs:
            sender_tag = "user_a" if m.sender_id == s.user_a_id else "user_b"
            message_items.append(
                {
                    "id": m.id,
                    "sender_id": m.sender_id,
                    "sender_tag": sender_tag,
                    "msg_type": m.msg_type,
                    "text_content": m.text_content,
                    "file_id": m.file_id,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                }
            )

        report_items = []
        for r in reps:
            report_items.append(
                {
                    "id": r.id,
                    "reporter_id": r.reporter_id,
                    "reported_id": r.reported_id,
                    "reason": r.reason,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        return {
            "session": {
                "id": s.id,
                "user_a_id": s.user_a_id,
                "user_a_username": user_a.username if user_a else None,
                "user_a_age": user_a.age if user_a else None,
                "user_a_gender": user_a.gender if user_a else None,
                "user_a_location": user_a.location if user_a else None,
                "user_a_premium": user_a.is_premium if user_a else False,
                "user_a_banned": user_a.is_banned if user_a else False,
                "user_b_id": s.user_b_id,
                "user_b_username": user_b.username if user_b else None,
                "user_b_age": user_b.age if user_b else None,
                "user_b_gender": user_b.gender if user_b else None,
                "user_b_location": user_b.location if user_b else None,
                "user_b_premium": user_b.is_premium if user_b else False,
                "user_b_banned": user_b.is_banned if user_b else False,
                "is_shadow_session": s.is_shadow_session,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "ended_by": s.ended_by,
            },
            "messages": message_items,
            "reports": report_items,
        }


async def get_admin_reports(status_filter: Optional[str] = "open") -> List[Dict[str, Any]]:
    """Retrieves list of reports with session and user metadata."""
    async with get_session() as session:
        stmt = select(Report)
        if status_filter and status_filter.lower() != "all":
            stmt = stmt.where(Report.status == status_filter.lower())
        stmt = stmt.order_by(Report.created_at.desc())

        reports = (await session.execute(stmt)).scalars().all()
        report_list = []
        for r in reports:
            reporter = await get_user_by_id(r.reporter_id)
            reported = await get_user_by_id(r.reported_id)

            report_list.append(
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "reporter_id": r.reporter_id,
                    "reporter_username": reporter.username if reporter else None,
                    "reported_id": r.reported_id,
                    "reported_username": reported.username if reported else None,
                    "reported_is_banned": reported.is_banned if reported else False,
                    "reported_abuse_score": reported.abuse_score if reported else 0,
                    "reason": r.reason,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        return report_list


async def update_report_status(report_id: int, new_status: str) -> bool:
    """Updates report status (e.g. dismissed, resolved)."""
    async with get_session() as session:
        async with session.begin():
            res = await session.execute(
                update(Report).where(Report.id == report_id).values(status=new_status)
            )
            return res.rowcount > 0

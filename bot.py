import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import aiosqlite

# ─────────────────────────────────────────────
# CONFIG — apna data yahan dal
# ─────────────────────────────────────────────
BOT_TOKEN = "8793158882:AAHbrozb3M5fYk3JIpnrIr6kCxZL6qGGNJg"
ADMIN_ID = 8481518749  # Apna Telegram ID
DB_FILE = "bot_database.db"

# Limits
FREE_VIDEOS_PER_DAY = 1
FREE_DOWNLOADS_PER_DAY = 1
PREMIUM_DURATION_HOURS = 12
PREMIUM_DOWNLOADS = 20
REFER_BONUS_VIDEOS = 10
CHANNEL_JOIN_BONUS = 20
VIDEO_DELETE_SECONDS = 120  # 2 minutes

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BOT & DISPATCHER
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})

# ─────────────────────────────────────────────
# FSM STATES
# ─────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_add_channel = State()
    waiting_broadcast = State()
    waiting_videos = State()


# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                join_date TEXT,
                referred_by INTEGER DEFAULT NULL,
                bonus_videos INTEGER DEFAULT 0,
                free_watched_today INTEGER DEFAULT 0,
                last_reset_date TEXT DEFAULT '',
                premium_expiry TEXT DEFAULT NULL,
                downloads_remaining INTEGER DEFAULT 1,
                downloads_reset_date TEXT DEFAULT '',
                current_msg_id INTEGER DEFAULT NULL,
                current_job_id TEXT DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT UNIQUE,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                uploaded_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                channel_name TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                credited_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                user_id INTEGER,
                video_id INTEGER,
                vote TEXT,
                PRIMARY KEY (user_id, video_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT,
                added_at TEXT
            )
        """)
        await db.commit()


# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────
async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def register_user(user_id: int, username: str, referred_by: int = None):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users 
            (user_id, username, join_date, referred_by, last_reset_date, downloads_reset_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username or "", datetime.now().isoformat(), referred_by, today, today))
        await db.commit()


async def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE users SET {sets} WHERE user_id = ?", vals)
        await db.commit()


async def reset_daily_if_needed(user: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False
    updates = {}
    if user["last_reset_date"] != today:
        updates["free_watched_today"] = 0
        updates["last_reset_date"] = today
        changed = True
    if user["downloads_reset_date"] != today:
        updates["downloads_remaining"] = FREE_DOWNLOADS_PER_DAY
        updates["downloads_reset_date"] = today
        changed = True
    if changed:
        await update_user(user["user_id"], **updates)
        user.update(updates)
    return user


async def is_premium(user: dict) -> bool:
    if not user.get("premium_expiry"):
        return False
    return datetime.now() < datetime.fromisoformat(user["premium_expiry"])


async def get_random_video() -> Optional[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM videos ORDER BY RANDOM() LIMIT 1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_video_count() -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM videos") as cur:
            row = await cur.fetchone()
            return row[0]


async def get_all_channels() -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM channels WHERE is_active = 1") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_user_count() -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0]


async def get_premium_count() -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE premium_expiry > ?", (now,)) as cur:
            row = await cur.fetchone()
            return row[0]


async def get_all_user_ids() -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


# ─────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Get Video", callback_data="get_video")]
    ])


def kb_video(video_id: int, likes: int, dislikes: int, downloads_left: int, is_prem: bool):
    total = likes + dislikes
    pct = int((likes / total) * 100) if total > 0 else 0
    dl_text = f"📥 Download ({downloads_left} left)" if downloads_left > 0 else "📥 Download (0 left)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Like", callback_data=f"like_{video_id}"),
            InlineKeyboardButton(text=f"{pct}%", callback_data="noop"),
            InlineKeyboardButton(text="👎 Dislike", callback_data=f"dislike_{video_id}"),
        ],
        [InlineKeyboardButton(text=dl_text, callback_data=f"download_{video_id}")],
        [InlineKeyboardButton(text="▶️ Next", callback_data="get_video")],
    ])


def kb_unlock():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Watch Ad → 12hr Premium", callback_data="watch_ad")],
        [InlineKeyboardButton(text="📢 Join Channels → 20 Videos", callback_data="join_channels")],
        [InlineKeyboardButton(text="👥 Refer & Earn → 10 Videos/Refer", callback_data="refer_earn")],
    ])


def kb_channels(channels: list):
    buttons = []
    for ch in channels:
        name = ch["channel_name"] or ch["channel_id"]
        cid = ch["channel_id"]
        link = f"https://t.me/{cid.lstrip('@')}" if cid.startswith("@") else f"https://t.me/c/{str(cid).lstrip('-100')}/1"
        buttons.append([InlineKeyboardButton(text=f"📢 {name}", url=link)])
    buttons.append([InlineKeyboardButton(text="✅ I've Joined All", callback_data="check_joined")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📹 Upload Video", callback_data="adm_upload")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="👥 Stats", callback_data="adm_stats")],
        [InlineKeyboardButton(text="⚙️ Settings", callback_data="adm_settings")],
    ])


def kb_admin_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Manage Channels", callback_data="adm_channels")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")],
    ])


def kb_approve_reject():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data="approve_videos"),
            InlineKeyboardButton(text="❌ Reject", callback_data="reject_videos"),
        ]
    ])


def kb_get_video():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Get New Video", callback_data="get_video")]
    ])


# ─────────────────────────────────────────────
# VIDEO SEND HELPER
# ─────────────────────────────────────────────
async def delete_current_video(user_id: int, job_id: str = None):
    user = await get_user(user_id)
    if not user:
        return
    # Cancel scheduler job
    if user.get("current_job_id"):
        try:
            scheduler.remove_job(user["current_job_id"])
        except Exception:
            pass
    # Delete message
    if user.get("current_msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=user["current_msg_id"])
        except Exception:
            pass
    await update_user(user_id, current_msg_id=None, current_job_id=None)


async def auto_delete_video(user_id: int):
    user = await get_user(user_id)
    if not user or not user.get("current_msg_id"):
        return
    try:
        await bot.delete_message(chat_id=user_id, message_id=user["current_msg_id"])
    except Exception:
        pass
    await update_user(user_id, current_msg_id=None, current_job_id=None)
    try:
        await bot.send_message(
            chat_id=user_id,
            text="⏰ Video expired!\n\nGet a new one 👇",
            reply_markup=kb_get_video()
        )
    except Exception:
        pass


async def send_video_to_user(user_id: int, chat_id: int):
    video = await get_random_video()
    if not video:
        await bot.send_message(chat_id, "😔 No videos available right now. Check back later!")
        return

    # Delete previous video first
    await delete_current_video(user_id)

    user = await get_user(user_id)
    prem = await is_premium(user)
    dl_left = user["downloads_remaining"] if not prem else PREMIUM_DOWNLOADS

    kb = kb_video(video["id"], video["likes"], video["dislikes"], dl_left, prem)

    prem_text = ""
    if prem:
        expiry = datetime.fromisoformat(user["premium_expiry"]).strftime("%I:%M %p")
        prem_text = f"\n💎 Premium User Benefits:\n📥 Downloads remaining: {dl_left}/{PREMIUM_DOWNLOADS}"
    else:
        prem_text = f"\n📥 Downloads remaining: {user['downloads_remaining']}/{FREE_DOWNLOADS_PER_DAY}"

    caption = (
        f"👆 Enjoy the video!\n"
        f"{prem_text}\n\n"
        f"⚠️ This video will be deleted after 2 minutes."
    )

    # Send as copy (hide forward source)
    try:
        sent = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=0,  # placeholder — real send below
        )
    except Exception:
        pass

    sent = await bot.send_video(
        chat_id=chat_id,
        video=video["file_id"],
        caption=caption,
        reply_markup=kb
    )

    job_id = f"del_{user_id}_{sent.message_id}"
    scheduler.add_job(
        auto_delete_video,
        "date",
        run_date=datetime.now() + timedelta(seconds=VIDEO_DELETE_SECONDS),
        args=[user_id],
        id=job_id,
        replace_existing=True
    )
    await update_user(user_id, current_msg_id=sent.message_id, current_job_id=job_id)


# ─────────────────────────────────────────────
# CAN WATCH CHECK
# ─────────────────────────────────────────────
async def can_watch(user_id: int) -> tuple[bool, str]:
    user = await get_user(user_id)
    user = await reset_daily_if_needed(user)

    if await is_premium(user):
        return True, "premium"
    if user["bonus_videos"] > 0:
        await update_user(user_id, bonus_videos=user["bonus_videos"] - 1)
        return True, "bonus"
    if user["free_watched_today"] < FREE_VIDEOS_PER_DAY:
        await update_user(user_id, free_watched_today=user["free_watched_today"] + 1)
        return True, "free"
    return False, "locked"


# ─────────────────────────────────────────────
# /START
# ─────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    # Check referral
    args = message.text.split()
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referred_by = int(args[1].split("_")[1])
            if referred_by == user_id:
                referred_by = None
        except Exception:
            referred_by = None

    existing = await get_user(user_id)
    await register_user(user_id, username, referred_by)

    # Credit referrer if new user
    if not existing and referred_by:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, credited_at) VALUES (?, ?, ?)",
                (referred_by, user_id, datetime.now().isoformat())
            )
            await db.commit()
        ref_user = await get_user(referred_by)
        if ref_user:
            new_bonus = ref_user["bonus_videos"] + REFER_BONUS_VIDEOS
            await update_user(referred_by, bonus_videos=new_bonus)
            try:
                await bot.send_message(
                    referred_by,
                    f"🎉 Someone joined using your referral link!\n+{REFER_BONUS_VIDEOS} bonus videos added!\nTotal bonus: {new_bonus} videos"
                )
            except Exception:
                pass

    # Check if premium already active
    user = await get_user(user_id)
    prem = await is_premium(user)

    if prem:
        expiry = datetime.fromisoformat(user["premium_expiry"]).strftime("%I:%M %p IST")
        welcome = (
            f"✅ Premium Access Activated!\n\n"
            f"Hello {username}! 👋\n\n"
            f"🎉 You have unlimited access!\n"
            f"⏰ Premium expires at: {expiry}\n\n"
            f"🎬 Click below to start watching!"
        )
    else:
        welcome = (
            f"🎬 Welcome to Videos Bot! 🎬\n\n"
            f"Hello {username}! 👋\n\n"
            f"🎥 You can watch {FREE_VIDEOS_PER_DAY} free video per day!\n"
            f"⏰ Limit resets every 24 hours at midnight IST\n\n"
            f"💾 You can download {FREE_DOWNLOADS_PER_DAY} video per day!\n\n"
            f"💎 Want unlimited access?\n"
            f"• Watch an ad → 12 hours premium!\n"
            f"• Join channels → 20 videos!\n"
            f"• Refer friends → 10 videos each!\n\n"
            f"Enjoy! 🍿"
        )

    await message.answer(welcome, reply_markup=kb_start())


# ─────────────────────────────────────────────
# GET VIDEO
# ─────────────────────────────────────────────
@router.callback_query(F.data == "get_video")
async def cb_get_video(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()

    allowed, reason = await can_watch(user_id)
    if not allowed:
        await callback.message.answer(
            "🚫 You've Watched Your Free Video!\n\n"
            "To continue watching, unlock more access below 👇",
            reply_markup=kb_unlock()
        )
        return

    await send_video_to_user(user_id, callback.message.chat.id)


# ─────────────────────────────────────────────
# LIKE / DISLIKE
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("like_") | F.data.startswith("dislike_"))
async def cb_vote(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    vote_type = "like" if data.startswith("like_") else "dislike"
    video_id = int(data.split("_")[1])

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT vote FROM votes WHERE user_id = ? AND video_id = ?", (user_id, video_id)) as cur:
            existing_vote = await cur.fetchone()

        if existing_vote:
            await callback.answer("You already voted! ❌", show_alert=False)
            return

        await db.execute("INSERT INTO votes (user_id, video_id, vote) VALUES (?, ?, ?)", (user_id, video_id, vote_type))
        if vote_type == "like":
            await db.execute("UPDATE videos SET likes = likes + 1 WHERE id = ?", (video_id,))
        else:
            await db.execute("UPDATE videos SET dislikes = dislikes + 1 WHERE id = ?", (video_id,))
        await db.commit()

        async with db.execute("SELECT likes, dislikes FROM videos WHERE id = ?", (video_id,)) as cur:
            row = await cur.fetchone()
            likes, dislikes = row[0], row[1]

    total = likes + dislikes
    pct = int((likes / total) * 100) if total > 0 else 0

    user = await get_user(user_id)
    prem = await is_premium(user)
    dl_left = user["downloads_remaining"] if not prem else PREMIUM_DOWNLOADS

    new_kb = kb_video(video_id, likes, dislikes, dl_left, prem)
    try:
        await callback.message.edit_reply_markup(reply_markup=new_kb)
    except Exception:
        pass
    await callback.answer(f"{'👍' if vote_type == 'like' else '👎'} Voted!")


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("download_"))
async def cb_download(callback: CallbackQuery):
    user_id = callback.from_user.id
    video_id = int(callback.data.split("_")[1])
    await callback.answer()

    user = await get_user(user_id)
    user = await reset_daily_if_needed(user)
    prem = await is_premium(user)

    if prem:
        # Premium: track session downloads (we use bonus_videos field won't work, need separate)
        # Simple approach: downloads_remaining for premium too
        if user["downloads_remaining"] <= 0:
            await callback.message.answer("📥 No downloads left! Unlock more access.")
            return
        await update_user(user_id, downloads_remaining=user["downloads_remaining"] - 1)
    else:
        if user["downloads_remaining"] <= 0:
            await callback.message.answer(
                "📥 Download limit reached!\n\nUnlock more 👇",
                reply_markup=kb_unlock()
            )
            return
        await update_user(user_id, downloads_remaining=user["downloads_remaining"] - 1)

    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)) as cur:
            video = await cur.fetchone()

    if not video:
        await callback.message.answer("❌ Video not found!")
        return

    user = await get_user(user_id)
    dl_left = user["downloads_remaining"]

    await bot.send_video(
        chat_id=user_id,
        video=dict(video)["file_id"],
        caption=f"✅ Downloadable copy!\n💎 Downloads remaining: {dl_left}"
    )


# ─────────────────────────────────────────────
# WATCH AD
# ─────────────────────────────────────────────
@router.callback_query(F.data == "watch_ad")
async def cb_watch_ad(callback: CallbackQuery):
    await callback.answer()
    # In real implementation, integrate Adsgram here
    # For now, simulate ad completion
    user_id = callback.from_user.id
    expiry = datetime.now() + timedelta(hours=PREMIUM_DURATION_HOURS)
    await update_user(
        user_id,
        premium_expiry=expiry.isoformat(),
        downloads_remaining=PREMIUM_DOWNLOADS
    )
    expiry_str = expiry.strftime("%I:%M %p IST")
    await callback.message.answer(
        f"✅ Premium Access Activated!\n\n"
        f"🎉 You now have unlimited access for the next {PREMIUM_DURATION_HOURS} hours!\n"
        f"⏰ Premium expires at: {expiry_str}\n\n"
        f"🎬 Click below to start watching!",
        reply_markup=kb_start()
    )


# ─────────────────────────────────────────────
# JOIN CHANNELS
# ─────────────────────────────────────────────
@router.callback_query(F.data == "join_channels")
async def cb_join_channels(callback: CallbackQuery):
    await callback.answer()
    channels = await get_all_channels()
    if not channels:
        await callback.message.answer("📢 No channels configured yet. Try other unlock methods!")
        return
    await callback.message.answer(
        "📢 Join all channels below to unlock 20 videos!\n\n"
        "After joining all, tap ✅ I've Joined All",
        reply_markup=kb_channels(channels)
    )


@router.callback_query(F.data == "check_joined")
async def cb_check_joined(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer("Checking...")
    channels = await get_all_channels()
    if not channels:
        return

    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                not_joined.append(ch["channel_name"] or ch["channel_id"])
        except Exception:
            not_joined.append(ch["channel_name"] or ch["channel_id"])

    if not_joined:
        missing = "\n".join(f"❌ {c}" for c in not_joined)
        await callback.message.answer(f"You haven't joined:\n{missing}\n\nJoin all and try again!")
    else:
        user = await get_user(user_id)
        await update_user(user_id, bonus_videos=user["bonus_videos"] + CHANNEL_JOIN_BONUS)
        await callback.message.answer(
            f"✅ All channels joined!\n+{CHANNEL_JOIN_BONUS} videos added to your account!\n\n"
            f"Total bonus videos: {user['bonus_videos'] + CHANNEL_JOIN_BONUS}",
            reply_markup=kb_get_video()
        )


# ─────────────────────────────────────────────
# REFER & EARN
# ─────────────────────────────────────────────
@router.callback_query(F.data == "refer_earn")
async def cb_refer(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)) as cur:
            total_refs = (await cur.fetchone())[0]

    user = await get_user(user_id)
    await callback.message.answer(
        f"👥 Refer & Earn!\n\n"
        f"Share your link with friends:\n"
        f"`{ref_link}`\n\n"
        f"Each friend who joins = +{REFER_BONUS_VIDEOS} videos for you!\n\n"
        f"📊 Your stats:\n"
        f"Total referrals: {total_refs}\n"
        f"Bonus videos: {user['bonus_videos']}",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
# NOOP (% button)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ─────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("👑 Admin Panel", reply_markup=kb_admin_main())


@router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("👑 Admin Panel", reply_markup=kb_admin_main())


@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    total = await get_user_count()
    premium = await get_premium_count()
    videos = await get_video_count()
    channels = await get_all_channels()
    await callback.message.answer(
        f"📊 Bot Stats\n\n"
        f"👥 Total Users: {total}\n"
        f"💎 Premium Users: {premium}\n"
        f"🎬 Total Videos: {videos}\n"
        f"📢 Active Channels: {len(channels)}"
    )


@router.callback_query(F.data == "adm_settings")
async def cb_adm_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("⚙️ Settings", reply_markup=kb_admin_settings())


# ─── Manage Channels ───
@router.callback_query(F.data == "adm_channels")
async def cb_adm_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    channels = await get_all_channels()

    buttons = []
    for ch in channels:
        name = ch["channel_name"] or ch["channel_id"]
        buttons.append([InlineKeyboardButton(
            text=f"❌ Remove {name}",
            callback_data=f"rmch_{ch['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="adm_add_channel")])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="adm_settings")])

    text = "📢 Channels:\n\n"
    if channels:
        for i, ch in enumerate(channels, 1):
            text += f"{i}. {ch['channel_name'] or ch['channel_id']} ✅\n"
    else:
        text += "No channels added yet."

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "adm_add_channel")
async def cb_adm_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_add_channel)
    await callback.message.answer(
        "📢 Send channel ID or @username\n\n"
        "⚠️ Make sure bot is admin in that channel first!\n\n"
        "Format: @channelname or -100xxxxxxxxx"
    )


@router.message(AdminStates.waiting_add_channel)
async def process_add_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    channel_id = message.text.strip()
    await state.clear()

    # Verify bot is admin
    try:
        chat = await bot.get_chat(channel_id)
        bot_member = await bot.get_chat_member(channel_id, (await bot.get_me()).id)
        if bot_member.status not in ("administrator", "creator"):
            await message.answer("❌ Bot is not admin in this channel!\nAdd bot as admin first.")
            return

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)",
                (channel_id, chat.title or channel_id)
            )
            await db.commit()

        await message.answer(f"✅ Channel added: {chat.title or channel_id}")
    except Exception as e:
        await message.answer(f"❌ Error: {e}\n\nMake sure:\n1. Bot is admin in channel\n2. Channel ID is correct")


@router.callback_query(F.data.startswith("rmch_"))
async def cb_remove_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    ch_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM channels WHERE id = ?", (ch_id,))
        await db.commit()
    await callback.answer("✅ Channel removed!")
    # Refresh list
    channels = await get_all_channels()
    buttons = []
    for ch in channels:
        name = ch["channel_name"] or ch["channel_id"]
        buttons.append([InlineKeyboardButton(text=f"❌ Remove {name}", callback_data=f"rmch_{ch['id']}")])
    buttons.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="adm_add_channel")])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="adm_settings")])
    text = "📢 Channels:\n\n" + ("\n".join(f"{i+1}. {ch['channel_name'] or ch['channel_id']} ✅" for i, ch in enumerate(channels)) or "No channels.")
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception:
        pass


# ─── Upload Video ───
@router.callback_query(F.data == "adm_upload")
async def cb_adm_upload(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_videos)
    video_count = await get_video_count()
    await callback.message.answer(
        f"📹 Send videos now!\n\n"
        f"📊 Current DB: {video_count} videos\n\n"
        f"You can send multiple videos at once.\n"
        f"When done, send /done to approve/reject."
    )
    await state.update_data(pending=[])


@router.message(AdminStates.waiting_videos, F.video)
async def process_admin_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    pending = data.get("pending", [])
    file_id = message.video.file_id

    # Check duplicate
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id FROM videos WHERE file_id = ?", (file_id,)) as cur:
            exists = await cur.fetchone()

    if exists:
        await message.reply("⚠️ Already in DB, skipped.")
        return

    pending.append(file_id)
    await state.update_data(pending=pending)
    await message.reply(f"📥 Received! ({len(pending)} new videos pending)")


@router.message(AdminStates.waiting_videos, Command("done"))
async def process_admin_done(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    pending = data.get("pending", [])

    if not pending:
        await state.clear()
        await message.answer("No new videos received.")
        return

    video_count = await get_video_count()
    await message.answer(
        f"📊 Video Report:\n\n"
        f"🆕 New videos: {len(pending)}\n"
        f"📦 Already in DB: (duplicates auto-skipped)\n"
        f"📁 Total after approve: {video_count + len(pending)}\n\n"
        f"Approve to save all?",
        reply_markup=kb_approve_reject()
    )


@router.callback_query(F.data == "approve_videos")
async def cb_approve(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    data = await state.get_data()
    pending = data.get("pending", [])

    async with aiosqlite.connect(DB_FILE) as db:
        for fid in pending:
            await db.execute(
                "INSERT OR IGNORE INTO videos (file_id, uploaded_at) VALUES (?, ?)",
                (fid, datetime.now().isoformat())
            )
        await db.commit()

    await state.clear()
    total = await get_video_count()
    await callback.message.edit_text(
        f"✅ {len(pending)} videos saved!\n📦 Total videos: {total}"
    )


@router.callback_query(F.data == "reject_videos")
async def cb_reject(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("❌ All pending videos discarded.")


# ─── Broadcast ───
@router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.answer("📢 Send the message to broadcast to all users:")


@router.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    user_ids = await get_all_user_ids()
    success = 0
    fail = 0
    status_msg = await message.answer(f"📢 Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(f"✅ Broadcast done!\n✅ Success: {success}\n❌ Failed: {fail}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    await init_db()
    scheduler.start()
    logger.info("Bot starting...")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())

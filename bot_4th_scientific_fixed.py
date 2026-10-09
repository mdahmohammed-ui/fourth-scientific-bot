import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "PUT_YOUR_BOT_TOKEN_HERE").strip()
OWNER_ID = 7683201905
MANDATORY_CHANNEL = "@Kii_8i"
DB_FILE = "fourth_scientific_bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("fourth_scientific_bot")

# ============================================================
# FIXED DATA SUPPLIED BY USER
# No extra subjects/files are added here.
# The old Math book is intentionally NOT seeded because the user
# said it is not the final Math book.
# ============================================================

SUBJECTS = [
    ("رياضيات", "📐", 1),
    ("كيمياء", "🧪", 2),
    ("فيزياء", "⚡", 3),
    ("أحياء", "🧬", 4),
    ("English", "🇬🇧", 5),
    ("التربية الإسلامية", "📖", 6),
    ("الحاسوب", "💻", 7),
    ("اللغة العربية", "📚", 9),
    ("جرائم حزب البعث", "⚖️", 10),
]

BOOKS = []
NOTES = []


# Unknown files that were supplied but whose subject/role was not safely
# inferable are intentionally NOT inserted into the database.
# Summaries were not supplied, so none are seeded.

DEFAULT_WELCOME = (
    "هلا بيك ببوت الرابع العلمي.\n"
    "اختَر المادة من القائمة بالأسفل."
)

# ============================================================
# DATABASE
# ============================================================

DB_LOCK = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = db_connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                emoji TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                visible INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('book', 'note')),
                title TEXT NOT NULL,
                teacher TEXT,
                file_id TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL DEFAULT 'document',
                visible INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('welcome', ?)",
            (DEFAULT_WELCOME,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('subscription_enabled', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('mandatory_channel', ?)",
            (MANDATORY_CHANNEL,),
        )

        for name, emoji, order in SUBJECTS:
            conn.execute(
                """
                INSERT OR IGNORE INTO subjects(name, emoji, sort_order, visible)
                VALUES(?, ?, ?, 1)
                """,
                (name, emoji, order),
            )

        # User requested removing Kurdish entirely. Delete both spelling variants
        # from the database; ON DELETE CASCADE removes their associated files.
        conn.execute("DELETE FROM subjects WHERE TRIM(name) IN ('الكردي', 'كردي')")

        conn.commit()
    finally:
        conn.close()


async def db_execute(sql: str, params=(), fetch: str = "none"):
    async with DB_LOCK:
        conn = db_connect()
        try:
            cur = conn.execute(sql, params)
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            elif fetch == "value":
                row = cur.fetchone()
                result = row[0] if row else None
            conn.commit()
            return result
        finally:
            conn.close()


async def db_script(sql: str) -> None:
    async with DB_LOCK:
        conn = db_connect()
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()


async def upsert_user(user) -> None:
    await db_execute(
        """
        INSERT INTO users(user_id, username, first_name, joined_at, last_seen, blocked)
        VALUES(?, ?, ?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen,
            blocked=0
        """,
        (user.id, user.username, user.first_name, now_iso(), now_iso()),
    )


async def get_setting(key: str, default: str = "") -> str:
    value = await db_execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
        fetch="value",
    )
    return default if value is None else value


async def set_setting(key: str, value: str) -> None:
    await db_execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

# ============================================================
# HELPERS
# ============================================================


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def subscription_enabled() -> bool:
    return (await get_setting("subscription_enabled", "1")) == "1"


async def is_subscribed(bot, user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if not await subscription_enabled():
        return True

    channel = await get_setting("mandatory_channel", MANDATORY_CHANNEL)
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        if member.status in {
            "member",
            "administrator",
            "creator",
        }:
            return True
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except TelegramError:
        # If the bot cannot inspect the channel, do not falsely grant access.
        return False


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if is_owner(user.id):
        return True

    # Cache successful membership checks for five minutes so each navigation
    # button does not wait on a fresh Telegram network request.
    now = time.monotonic()
    cached = context.user_data.get("subscription_cache")
    if cached and cached.get("ok") and now - cached.get("checked_at", 0) < 300:
        return True

    await upsert_user(user)
    ok = await is_subscribed(context.bot, user.id)
    context.user_data["subscription_cache"] = {"ok": ok, "checked_at": now}
    return ok

async def send_subscription_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    channel = await get_setting("mandatory_channel", MANDATORY_CHANNEL)
    public_link = channel
    if channel.startswith("@"):
        public_link = "https://t.me/" + channel[1:]

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("تحقق من الاشتراك")]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )
    await update.effective_message.reply_text(
        f"حتى تستخدم البوت، اشترك بالقناة أولاً.\n{public_link}\n\nبعد الاشتراك اضغط: تحقق من الاشتراك",
        reply_markup=keyboard,
    )


async def get_visible_subjects():
    return await db_execute(
        "SELECT * FROM subjects WHERE visible=1 ORDER BY sort_order, id",
        fetch="all",
    )


async def get_subject(subject_id: int):
    return await db_execute(
        "SELECT * FROM subjects WHERE id=?",
        (subject_id,),
        fetch="one",
    )


async def get_file(file_id: int):
    return await db_execute(
        """
        SELECT f.*, s.name AS subject_name, s.emoji AS subject_emoji
        FROM files f JOIN subjects s ON s.id=f.subject_id
        WHERE f.id=?
        """,
        (file_id,),
        fetch="one",
    )


async def get_files(subject_id: int, kind: str, visible_only: bool = True):
    extra = "AND visible=1" if visible_only else ""
    return await db_execute(
        f"SELECT * FROM files WHERE subject_id=? AND kind=? {extra} ORDER BY id",
        (subject_id, kind),
        fetch="all",
    )


def kind_label(kind: str) -> str:
    return {"book": "كتاب", "note": "ملزمة"}[kind]


def file_button_title(row) -> str:
    teacher = f" — {row['teacher']}" if row["teacher"] else ""
    return f"{row['title']}{teacher}"


def note_caption(row) -> str:
    teacher = row["teacher"] or ""
    title = row["title"] or "ملزمة"
    return (
        f"📙: {title}\n"
        f"👨‍🏫: {teacher}\n"
        "🏷: للصف الرابع الاعدادي\n"
        "📆: تاريخ الإصدار 2026"
    )


async def main_menu_markup() -> ReplyKeyboardMarkup:
    subjects = await get_visible_subjects()

    preferred_order = [
        "اللغة العربية",
        "التربية الإسلامية",
        "English",
        "رياضيات",
        "كيمياء",
        "فيزياء",
        "أحياء",
        "الحاسوب",
        "جرائم حزب البعث",
    ]

    # Deduplicate by normalized name so a duplicate such as "كردي"
    # cannot create a second button.
    by_name = {}
    for subject in subjects:
        name = subject["name"].strip()
        normalized = " ".join(name.split()).casefold()
        by_name.setdefault(normalized, subject)

    ordered = []
    used = set()
    for name in preferred_order:
        normalized = name.casefold()
        if normalized in by_name and normalized not in used:
            ordered.append(by_name[normalized])
            used.add(normalized)
    ordered.extend(s for normalized, s in by_name.items() if normalized not in used)

    keyboard = []
    current = []
    for subject in ordered:
        current.append(KeyboardButton(f"{subject['emoji']} {subject['name']}"))
        if len(current) == 2:
            keyboard.append(current)
            current = []
    if current:
        keyboard.append(current)

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


def reply_keyboard(rows):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(str(item)) for item in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


async def replace_user_menu(message, context, text, markup):
    # Keep previous messages visible; do not spend a network request deleting them.
    return await message.reply_text(text, reply_markup=markup)

async def show_main_menu(target_message, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    welcome = await get_setting("welcome", DEFAULT_WELCOME)
    sent = await replace_user_menu(target_message, context, welcome, await main_menu_markup())
    context.user_data["user_nav"] = {"level": "main", "menu_message_id": sent.message_id}
    return sent


async def show_subject_reply_menu(message, context, subject_id: int):
    subject = await get_subject(subject_id)
    if not subject or not subject["visible"]:
        await message.reply_text("القسم غير متاح حالياً.", reply_markup=await main_menu_markup())
        return

    markup = reply_keyboard([["📘 الكتب", "📙 الملازم"], ["رجوع"]])
    sent = await replace_user_menu(message, context, f"{subject['emoji']} {subject['name']}\nاختَر:", markup)
    context.user_data["user_nav"] = {
        "level": "subject",
        "subject_id": subject_id,
        "menu_message_id": sent.message_id,
    }


async def show_file_reply_menu(message, context, subject_id: int, kind: str):
    subject = await get_subject(subject_id)
    if not subject or not subject["visible"]:
        await show_main_menu(message, context)
        return

    files = await get_files(subject_id, kind, True)
    file_buttons = {}
    rows = []
    current = []

    for f in files:
        label = ("📘 " if kind == "book" else "📙 ") + file_button_title(f)
        file_buttons[label] = f["id"]
        current.append(label)
        if len(current) == 1:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    rows.append(["رجوع"])
    context.user_data["user_nav"] = {
        "level": "files",
        "subject_id": subject_id,
        "kind": kind,
        "file_buttons": file_buttons,
    }

    title = "📘 الكتب" if kind == "book" else "📙 الملازم"
    if not files:
        sent = await replace_user_menu(
            message, context,
            f"{title} — {subject['name']}\nلا توجد ملفات مضافة حالياً.",
            reply_keyboard([["رجوع"]]),
        )
        context.user_data["user_nav"]["menu_message_id"] = sent.message_id
        return

    sent = await replace_user_menu(
        message, context,
        f"{title} — {subject['name']}",
        reply_keyboard(rows),
    )
    context.user_data["user_nav"]["menu_message_id"] = sent.message_id


async def deliver_file_by_id(message, context, file_id: int):
    row = await get_file(file_id)
    if not row or not row["visible"]:
        await message.reply_text("الملف غير متاح حالياً.")
        return

    try:
        if row["kind"] == "note":
            await context.bot.send_document(
                chat_id=message.chat_id,
                document=row["file_id"],
                caption=note_caption(row),
            )
        else:
            await context.bot.send_document(
                chat_id=message.chat_id,
                document=row["file_id"],
                caption=row["title"],
            )
    except TelegramError as exc:
        logger.exception("Failed to deliver file %s: %s", file_id, exc)
        await message.reply_text(
            "تعذر إرسال الملف حالياً. استخدم لوحة المالك لاستبدال الملف بـ File ID جديد."
        )


async def main_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return
    if is_owner(user.id):
        return
    if not await ensure_access(update, context):
        await send_subscription_message(update, context)
        return

    text = message.text.strip()

    # Subscription verification is also handled from the ReplyKeyboard.
    if text == "تحقق من الاشتراك":
        if await is_subscribed(context.bot, user.id):
            await show_main_menu(message, context)
        else:
            await send_subscription_message(update, context)
        return

    nav = context.user_data.get("user_nav") or {}
    level = nav.get("level")

    if level == "subject":
        subject_id = nav.get("subject_id")
        if text == "📘 الكتب":
            await show_file_reply_menu(message, context, subject_id, "book")
            return
        if text == "📙 الملازم":
            await show_file_reply_menu(message, context, subject_id, "note")
            return
        if text == "رجوع":
            await show_main_menu(message, context)
            return

    if level == "files":
        subject_id = nav.get("subject_id")
        kind = nav.get("kind")
        if text == "رجوع":
            await show_subject_reply_menu(message, context, subject_id)
            return
        file_buttons = nav.get("file_buttons", {})
        file_id = file_buttons.get(text)
        if file_id:
            await deliver_file_by_id(message, context, int(file_id))
            return

    # Main subject menu.
    subjects = await get_visible_subjects()
    seen = set()
    for subject in subjects:
        name = subject["name"].strip()
        normalized = " ".join(name.split()).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        labels = {name, f"{subject['emoji']} {name}".strip()}
        if text in labels:
            await show_subject_reply_menu(message, context, subject["id"])
            return


# ============================================================
# ADMIN UI
# ============================================================


def admin_menu_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("إضافة ملف", callback_data="adm:add")],
        [InlineKeyboardButton("إضافة ملف بواسطة ID", callback_data="adm:addbyid")],
        [InlineKeyboardButton("حذف ملف", callback_data="adm:delete"),
         InlineKeyboardButton("تعديل ملف", callback_data="adm:edit")],
        [InlineKeyboardButton("استبدال ملف", callback_data="adm:replace"),
         InlineKeyboardButton("إخفاء/إظهار ملف", callback_data="adm:hide")],
        [InlineKeyboardButton("إضافة مادة", callback_data="adm:addsubject")],
        [InlineKeyboardButton("تعديل مادة", callback_data="adm:editsubject"),
         InlineKeyboardButton("حذف مادة", callback_data="adm:deletesubject")],
        [InlineKeyboardButton("🔎 التعرف على ID", callback_data="adm:fileid")],
        [InlineKeyboardButton("الإحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton("إرسال إعلان", callback_data="adm:broadcast")],
        [InlineKeyboardButton("إعدادات البوت", callback_data="adm:settings")],
        [InlineKeyboardButton("إغلاق", callback_data="adm:close")],
    ]
    return InlineKeyboardMarkup(keyboard)


def admin_state_reset(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin", None)


def set_admin_state(context, **data):
    state = context.user_data.setdefault("admin", {})
    state.update(data)


def get_admin_state(context) -> dict:
    return context.user_data.setdefault("admin", {})


async def show_admin_menu(query=None, message=None):
    text = "لوحة المالك"
    markup = admin_menu_markup()
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    elif message:
        await message.reply_text(text, reply_markup=markup)


async def admin_subject_picker(callback_prefix: str, include_hidden: bool = False) -> InlineKeyboardMarkup:
    where = "" if include_hidden else "WHERE visible=1"
    subjects = await db_execute(
        f"SELECT * FROM subjects {where} ORDER BY sort_order, id",
        fetch="all",
    )
    keyboard = [
        [InlineKeyboardButton(f"{s['emoji']} {s['name']}", callback_data=f"{callback_prefix}:{s['id']}")]
        for s in subjects
    ]
    keyboard.append([InlineKeyboardButton("رجوع", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)


async def admin_file_picker(kind: Optional[str] = None, action: str = "pickfile") -> InlineKeyboardMarkup:
    params = ()
    where = "WHERE 1=1"
    if kind:
        where += " AND f.kind=?"
        params = (kind,)
    rows = await db_execute(
        f"""
        SELECT f.id, f.title, f.teacher, f.kind, s.name AS subject_name
        FROM files f JOIN subjects s ON s.id=f.subject_id
        {where}
        ORDER BY s.sort_order, s.id, f.kind, f.id
        """,
        params,
        fetch="all",
    )
    keyboard = []
    for r in rows:
        state = "✓" if (await get_file(r["id"]))["visible"] else "×"
        label = f"{state} {r['subject_name']} | {kind_label(r['kind'])} | {r['title']}"
        keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"{action}:{r['id']}")])
    if not rows:
        keyboard.append([InlineKeyboardButton("لا توجد ملفات", callback_data="noop")])
    keyboard.append([InlineKeyboardButton("رجوع", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)

# ============================================================
# USER HANDLERS
# ============================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_state_reset(context)
    await upsert_user(update.effective_user)
    if not await is_subscribed(context.bot, update.effective_user.id):
        await send_subscription_message(update, context)
        return
    await show_main_menu(update.effective_message, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    admin_state_reset(context)
    await update.effective_message.reply_text("تم إلغاء العملية.", reply_markup=admin_menu_markup())


async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update.effective_user)
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("هذا القسم للمالك فقط.")
        return
    admin_state_reset(context)
    await show_admin_menu(message=update.effective_message)

# ============================================================
# CALLBACK HANDLER
# ============================================================


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "noop":
        return

    # Subscription check is always required for non-owner users.
    if not is_owner(user_id) and not await is_subscribed(context.bot, user_id):
        await send_subscription_message(update, context)
        return

    data = query.data

    if data == "check_sub":
        if await is_subscribed(context.bot, user_id):
            await show_main_menu(query.message, context, edit=True)
        else:
            await send_subscription_message(update, context)
        return

    if data == "home":
        await show_main_menu(query.message, context, edit=True)
        return

    if data.startswith("sub:"):
        await show_subject_menu(query, int(data.split(":")[1]))
        return

    if data.startswith("books:"):
        await show_books(query, int(data.split(":")[1]))
        return

    if data.startswith("notes:"):
        await show_notes(query, int(data.split(":")[1]))
        return

    if data.startswith("file:"):
        await deliver_file(update, context, int(data.split(":")[1]))
        return

    # -------------------- ADMIN --------------------
    if not is_owner(user_id):
        return

    if data == "admin_menu":
        admin_state_reset(context)
        await show_admin_menu(query=query)
        return

    if data == "adm:close":
        admin_state_reset(context)
        await query.edit_message_text("تم إغلاق لوحة المالك.")
        return

    # File ID lookup: enter a state that accepts the next uploaded file.
    if data == "adm:fileid":
        set_admin_state(context, action="fileid_wait_file")
        await query.edit_message_text(
            "أرسل ملف PDF الآن كـ Document حتى أستخرج الـ File ID.\n\n"
            "للإلغاء: /cancel"
        )
        return

    if data == "adm:add":
        set_admin_state(context, action="add_file_choose_kind")
        keyboard = [
            [InlineKeyboardButton("📘 كتاب", callback_data="addkind:book")],
            [InlineKeyboardButton("📙 ملزمة", callback_data="addkind:note")],
            [InlineKeyboardButton("رجوع", callback_data="admin_menu")],
        ]
        await query.edit_message_text("اختَر نوع الملف:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "adm:addbyid":
        set_admin_state(context, action="add_file_id_choose_kind")
        keyboard = [
            [InlineKeyboardButton("📘 كتاب", callback_data="addidkind:book")],
            [InlineKeyboardButton("📙 ملزمة", callback_data="addidkind:note")],
            [InlineKeyboardButton("رجوع", callback_data="admin_menu")],
        ]
        await query.edit_message_text("اختَر نوع الملف الذي تريد إضافته باستخدام File ID:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("addidkind:"):
        kind = data.split(":", 1)[1]
        set_admin_state(context, action="add_file_id_choose_subject", kind=kind)
        markup = await admin_subject_picker("addidsub")
        await query.edit_message_text("اختَر المادة:", reply_markup=markup)
        return

    if data.startswith("addidsub:"):
        subject_id = int(data.split(":", 1)[1])
        set_admin_state(context, action="add_file_wait_file_id", subject_id=subject_id)
        await query.edit_message_text("أرسل الـ File ID كنص، ثم سأطلب اسم الملف.\n\nللإلغاء: /cancel")
        return

    if data.startswith("addkind:"):
        kind = data.split(":", 1)[1]
        set_admin_state(context, action="add_file_choose_subject", kind=kind)
        markup = await admin_subject_picker("addsub")
        await query.edit_message_text("اختَر المادة:", reply_markup=markup)
        return

    if data.startswith("addsub:"):
        subject_id = int(data.split(":")[1])
        set_admin_state(context, action="add_file_wait_document", subject_id=subject_id)
        await query.edit_message_text("أرسل الملف الآن كـ Document.")
        return

    if data == "adm:delete":
        set_admin_state(context, action="delete_file_choose")
        await query.edit_message_text(
            "اختَر الملف المراد حذفه:",
            reply_markup=await admin_file_picker(action="delfile"),
        )
        return

    if data.startswith("delfile:"):
        fid = int(data.split(":")[1])
        row = await get_file(fid)
        if not row:
            await query.answer("الملف غير موجود.", show_alert=True)
            return
        await db_execute("DELETE FROM files WHERE id=?", (fid,))
        await query.edit_message_text("تم حذف الملف.", reply_markup=admin_menu_markup())
        admin_state_reset(context)
        return

    if data == "adm:hide":
        await query.edit_message_text(
            "اختَر الملف:",
            reply_markup=await admin_file_picker(action="togglefile"),
        )
        return

    if data.startswith("togglefile:"):
        fid = int(data.split(":")[1])
        row = await get_file(fid)
        if not row:
            await query.answer("الملف غير موجود.", show_alert=True)
            return
        new_value = 0 if row["visible"] else 1
        await db_execute("UPDATE files SET visible=?, updated_at=? WHERE id=?", (new_value, now_iso(), fid))
        await query.edit_message_text(
            "تم تحديث حالة الملف.",
            reply_markup=admin_menu_markup(),
        )
        return

    if data == "adm:replace":
        await query.edit_message_text(
            "اختَر الملف المراد استبداله:",
            reply_markup=await admin_file_picker(action="replacefile"),
        )
        return

    if data.startswith("replacefile:"):
        fid = int(data.split(":")[1])
        if not await get_file(fid):
            await query.answer("الملف غير موجود.", show_alert=True)
            return
        set_admin_state(context, action="replace_file_wait_document", file_id=fid)
        await query.edit_message_text("أرسل الملف الجديد الآن كـ Document.")
        return

    if data == "adm:edit":
        await query.edit_message_text(
            "اختَر الملف المراد تعديله:",
            reply_markup=await admin_file_picker(action="editfile"),
        )
        return

    if data.startswith("editfile:"):
        fid = int(data.split(":")[1])
        row = await get_file(fid)
        if not row:
            await query.answer("الملف غير موجود.", show_alert=True)
            return
        set_admin_state(context, action="edit_file_menu", file_id=fid)
        keyboard = [
            [InlineKeyboardButton("تعديل الاسم", callback_data="editfield:title")],
            [InlineKeyboardButton("تعديل الأستاذ", callback_data="editfield:teacher")],
            [InlineKeyboardButton("نقل إلى مادة", callback_data="editfield:subject")],
            [InlineKeyboardButton("رجوع", callback_data="admin_menu")],
        ]
        await query.edit_message_text(
            f"الملف: {row['title']}\nاختَر ما تريد تعديله:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("editfield:"):
        state = get_admin_state(context)
        fid = state.get("file_id")
        if not fid:
            await query.edit_message_text("انتهت العملية.", reply_markup=admin_menu_markup())
            return
        field = data.split(":", 1)[1]
        if field == "title":
            set_admin_state(context, action="edit_file_wait_text", file_id=fid, field="title")
            await query.edit_message_text("أرسل الاسم الجديد للملف.")
        elif field == "teacher":
            set_admin_state(context, action="edit_file_wait_text", file_id=fid, field="teacher")
            await query.edit_message_text("أرسل اسم الأستاذ الجديد.")
        elif field == "subject":
            set_admin_state(context, action="edit_file_choose_subject", file_id=fid)
            await query.edit_message_text(
                "اختَر المادة الجديدة:",
                reply_markup=await admin_subject_picker("newfilesub", include_hidden=True),
            )
        return

    if data.startswith("newfilesub:"):
        state = get_admin_state(context)
        fid = state.get("file_id")
        sid = int(data.split(":")[1])
        if not fid:
            await query.edit_message_text("انتهت العملية.", reply_markup=admin_menu_markup())
            return
        await db_execute("UPDATE files SET subject_id=?, updated_at=? WHERE id=?", (sid, now_iso(), fid))
        admin_state_reset(context)
        await query.edit_message_text("تم نقل الملف إلى المادة الجديدة.", reply_markup=admin_menu_markup())
        return

    if data == "adm:addsubject":
        set_admin_state(context, action="add_subject_wait_name")
        await query.edit_message_text("أرسل اسم المادة الجديدة.")
        return

    if data == "adm:editsubject":
        await query.edit_message_text(
            "اختَر المادة:",
            reply_markup=await admin_subject_picker("edsub", include_hidden=True),
        )
        return

    if data.startswith("edsub:"):
        sid = int(data.split(":")[1])
        set_admin_state(context, action="edit_subject_menu", subject_id=sid)
        keyboard = [
            [InlineKeyboardButton("تعديل الاسم", callback_data="edsubfield:name")],
            [InlineKeyboardButton("تعديل الإيموجي", callback_data="edsubfield:emoji")],
            [InlineKeyboardButton("إظهار/إخفاء", callback_data="edsubfield:visible")],
            [InlineKeyboardButton("رجوع", callback_data="admin_menu")],
        ]
        await query.edit_message_text("اختَر التعديل:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("edsubfield:"):
        state = get_admin_state(context)
        sid = state.get("subject_id")
        if not sid:
            await query.edit_message_text("انتهت العملية.", reply_markup=admin_menu_markup())
            return
        field = data.split(":", 1)[1]
        if field == "visible":
            row = await get_subject(sid)
            new_visible = 0 if row["visible"] else 1
            await db_execute("UPDATE subjects SET visible=? WHERE id=?", (new_visible, sid))
            admin_state_reset(context)
            await query.edit_message_text("تم تحديث ظهور المادة.", reply_markup=admin_menu_markup())
        elif field == "name":
            set_admin_state(context, action="edit_subject_wait_text", subject_id=sid, field="name")
            await query.edit_message_text("أرسل الاسم الجديد للمادة.")
        elif field == "emoji":
            set_admin_state(context, action="edit_subject_wait_text", subject_id=sid, field="emoji")
            await query.edit_message_text("أرسل الإيموجي الجديد.")
        return

    if data == "adm:deletesubject":
        await query.edit_message_text(
            "اختَر المادة المراد حذفها. حذف المادة يحذف ملفاتها أيضاً:",
            reply_markup=await admin_subject_picker("delsub", include_hidden=True),
        )
        return

    if data.startswith("delsub:"):
        sid = int(data.split(":")[1])
        row = await get_subject(sid)
        if not row:
            await query.answer("المادة غير موجودة.", show_alert=True)
            return
        keyboard = [
            [InlineKeyboardButton("تأكيد الحذف", callback_data=f"confirmsubdel:{sid}")],
            [InlineKeyboardButton("إلغاء", callback_data="admin_menu")],
        ]
        await query.edit_message_text(
            f"هل تؤكد حذف مادة {row['name']} وكل ملفاتها؟",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("confirmsubdel:"):
        sid = int(data.split(":")[1])
        await db_execute("DELETE FROM subjects WHERE id=?", (sid,))
        admin_state_reset(context)
        await query.edit_message_text("تم حذف المادة وملفاتها.", reply_markup=admin_menu_markup())
        return

    if data == "adm:stats":
        users = await db_execute("SELECT COUNT(*) FROM users", fetch="value")
        active = await db_execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now', '-30 days')",
            fetch="value",
        )
        subjects = await db_execute("SELECT COUNT(*) FROM subjects", fetch="value")
        visible_subjects = await db_execute("SELECT COUNT(*) FROM subjects WHERE visible=1", fetch="value")
        books = await db_execute("SELECT COUNT(*) FROM files WHERE kind='book'", fetch="value")
        notes = await db_execute("SELECT COUNT(*) FROM files WHERE kind='note'", fetch="value")
        visible_files = await db_execute("SELECT COUNT(*) FROM files WHERE visible=1", fetch="value")
        text = (
            "الإحصائيات\n\n"
            f"المستخدمون: {users}\n"
            f"المستخدمون النشطون خلال 30 يوم: {active}\n"
            f"المواد: {subjects}\n"
            f"المواد الظاهرة: {visible_subjects}\n"
            f"الكتب: {books}\n"
            f"الملازم: {notes}\n"
            f"الملفات الظاهرة: {visible_files}"
        )
        await query.edit_message_text(text, reply_markup=admin_menu_markup())
        return

    if data == "adm:broadcast":
        set_admin_state(context, action="broadcast_wait_text")
        await query.edit_message_text("أرسل نص الإعلان الآن.\n\nللإلغاء: /cancel")
        return

    if data == "adm:settings":
        enabled = await subscription_enabled()
        channel = await get_setting("mandatory_channel", MANDATORY_CHANNEL)
        keyboard = [
            [InlineKeyboardButton(
                f"الاشتراك الإجباري: {'مفعّل' if enabled else 'معطّل'}",
                callback_data="set:subscription",
            )],
            [InlineKeyboardButton("تغيير قناة الاشتراك", callback_data="set:channel")],
            [InlineKeyboardButton("تعديل رسالة الترحيب", callback_data="set:welcome")],
            [InlineKeyboardButton("رجوع", callback_data="admin_menu")],
        ]
        await query.edit_message_text(
            f"إعدادات البوت\n\nقناة الاشتراك الحالية: {channel}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "set:subscription":
        enabled = await subscription_enabled()
        await set_setting("subscription_enabled", "0" if enabled else "1")
        await query.edit_message_text(
            f"الاشتراك الإجباري الآن: {'مفعّل' if not enabled else 'معطّل'}",
            reply_markup=admin_menu_markup(),
        )
        return

    if data == "set:channel":
        set_admin_state(context, action="settings_wait_channel")
        await query.edit_message_text("أرسل معرف القناة مثل: @Kii_8i\n\nللإلغاء: /cancel")
        return

    if data == "set:welcome":
        set_admin_state(context, action="settings_wait_welcome")
        current = await get_setting("welcome", DEFAULT_WELCOME)
        await query.edit_message_text(
            f"الرسالة الحالية:\n\n{current}\n\nأرسل الرسالة الجديدة الآن."
        )
        return

# ============================================================
# ADMIN MESSAGE STATE HANDLER
# ============================================================


async def admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    state = get_admin_state(context)
    action = state.get("action")
    if not action:
        return

    message = update.effective_message

    # FILE ID LOOKUP
    if action == "fileid_wait_file":
        file_id = None
        file_type = None

        if message.document:
            file_id = message.document.file_id
            file_type = "Document"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "Photo"
        elif message.video:
            file_id = message.video.file_id
            file_type = "Video"
        elif message.audio:
            file_id = message.audio.file_id
            file_type = "Audio"
        elif message.voice:
            file_id = message.voice.file_id
            file_type = "Voice"
        elif message.animation:
            file_id = message.animation.file_id
            file_type = "Animation"
        elif message.sticker:
            file_id = message.sticker.file_id
            file_type = "Sticker"

        if not file_id:
            await message.reply_text(
                "أرسل ملفاً أو صورة أو فيديو أو صوتاً حتى أستخرج الـ File ID.\n\nللإلغاء: /cancel"
            )
            return

        await message.reply_text(
            f"تم التعرف على الملف.\n\n"
            f"النوع: {file_type}\n"
            f"🆔 File ID:\n{file_id}\n\n"
            "هذا الـ ID صادر من البوت الحالي ويمكن استخدامه مع هذا البوت."
        )
        admin_state_reset(context)
        await message.reply_text("تم إنهاء عملية التعرف على ID.", reply_markup=admin_menu_markup())
        return

    # ADD FILE BY EXISTING TELEGRAM FILE ID
    if action == "add_file_wait_file_id":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل الـ File ID كنص فقط.\n\nللإلغاء: /cancel")
            return
        file_id = message.text.strip()
        if any(ch.isspace() for ch in file_id):
            await message.reply_text("الـ File ID غير صحيح لأنه يحتوي على مسافات. أرسله كما ظهر لك تماماً.")
            return
        existing = await db_execute("SELECT id FROM files WHERE file_id=?", (file_id,), fetch="one")
        if existing:
            await message.reply_text("هذا الـ File ID موجود مسبقاً في قاعدة البيانات. أرسل ID مختلفاً أو ألغِ العملية بـ /cancel.")
            return
        state["file_id_new"] = file_id
        state["file_type"] = "document"
        state["action"] = "add_file_wait_title"
        await message.reply_text("تم استلام الـ ID. هسه أرسل اسم الملزمة أو الكتاب.")
        return

    # ADD FILE: wait for document
    if action == "add_file_wait_document":
        if not message.document:
            await message.reply_text("أرسل الملف كـ Document وليس كنص.\n\nللإلغاء: /cancel")
            return
        file_id = message.document.file_id
        existing = await db_execute("SELECT id FROM files WHERE file_id=?", (file_id,), fetch="one")
        if existing:
            await message.reply_text("هذا الملف موجود مسبقاً في قاعدة البيانات.\n\nللإلغاء: /cancel")
            return
        state["file_id_new"] = file_id
        state["original_filename"] = message.document.file_name or ""
        state["file_type"] = "document"
        if state.get("kind") == "note":
            state["action"] = "add_file_wait_title"
            await message.reply_text("أرسل اسم الملزمة.")
        else:
            state["action"] = "add_file_wait_title"
            await message.reply_text("أرسل اسم الملف.")
        return

    if action == "add_file_wait_title":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل اسم الملف كنص.")
            return
        state["title"] = message.text.strip()
        if state.get("kind") == "note":
            state["action"] = "add_file_wait_teacher"
            await message.reply_text("أرسل اسم الأستاذ.")
        else:
            await finish_add_file(update, context)
        return

    if action == "add_file_wait_teacher":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل اسم الأستاذ كنص.")
            return
        state["teacher"] = message.text.strip()
        await finish_add_file(update, context)
        return

    # REPLACE FILE
    if action == "replace_file_wait_document":
        if not message.document:
            await message.reply_text("أرسل الملف الجديد كـ Document.")
            return
        fid = state.get("file_id")
        new_file_id = message.document.file_id
        duplicate = await db_execute(
            "SELECT id FROM files WHERE file_id=? AND id<>?",
            (new_file_id, fid),
            fetch="one",
        )
        if duplicate:
            await message.reply_text("هذا الملف مستخدم بالفعل في ملف آخر.")
            return
        await db_execute(
            "UPDATE files SET file_id=?, file_type='document', updated_at=? WHERE id=?",
            (new_file_id, now_iso(), fid),
        )
        admin_state_reset(context)
        await message.reply_text("تم استبدال الملف بنجاح.", reply_markup=admin_menu_markup())
        return

    # EDIT TEXT FIELD
    if action == "edit_file_wait_text":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل النص المطلوب.")
            return
        fid = state.get("file_id")
        field = state.get("field")
        if field == "title":
            await db_execute("UPDATE files SET title=?, updated_at=? WHERE id=?", (message.text.strip(), now_iso(), fid))
        elif field == "teacher":
            await db_execute("UPDATE files SET teacher=?, updated_at=? WHERE id=?", (message.text.strip(), now_iso(), fid))
        admin_state_reset(context)
        await message.reply_text("تم تعديل الملف.", reply_markup=admin_menu_markup())
        return

    # ADD SUBJECT
    if action == "add_subject_wait_name":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل اسم المادة.")
            return
        state["subject_name"] = message.text.strip()
        state["action"] = "add_subject_wait_emoji"
        await message.reply_text("أرسل الإيموجي الخاص بالمادة.")
        return

    if action == "add_subject_wait_emoji":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل إيموجي واحد للمادة.")
            return
        name = state.get("subject_name")
        emoji = message.text.strip()
        exists = await db_execute("SELECT id FROM subjects WHERE name=?", (name,), fetch="one")
        if exists:
            await message.reply_text("هذه المادة موجودة مسبقاً.", reply_markup=admin_menu_markup())
            admin_state_reset(context)
            return
        max_order = await db_execute("SELECT COALESCE(MAX(sort_order), 0) FROM subjects", fetch="value")
        await db_execute(
            "INSERT INTO subjects(name, emoji, sort_order, visible) VALUES(?, ?, ?, 1)",
            (name, emoji, int(max_order) + 1),
        )
        admin_state_reset(context)
        await message.reply_text("تمت إضافة المادة.", reply_markup=admin_menu_markup())
        return

    # EDIT SUBJECT TEXT
    if action == "edit_subject_wait_text":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل النص المطلوب.")
            return
        sid = state.get("subject_id")
        field = state.get("field")
        value = message.text.strip()
        if field == "name":
            exists = await db_execute("SELECT id FROM subjects WHERE name=? AND id<>?", (value, sid), fetch="one")
            if exists:
                await message.reply_text("هذا الاسم مستخدم من مادة أخرى.")
                return
            await db_execute("UPDATE subjects SET name=? WHERE id=?", (value, sid))
        elif field == "emoji":
            await db_execute("UPDATE subjects SET emoji=? WHERE id=?", (value, sid))
        admin_state_reset(context)
        await message.reply_text("تم تعديل المادة.", reply_markup=admin_menu_markup())
        return

    # SETTINGS
    if action == "settings_wait_channel":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل معرف القناة مثل @Kii_8i")
            return
        channel = message.text.strip()
        if not channel.startswith("@"):
            await message.reply_text("أرسل معرف القناة بصيغة @ChannelUsername")
            return
        await set_setting("mandatory_channel", channel)
        admin_state_reset(context)
        await message.reply_text("تم تحديث قناة الاشتراك.", reply_markup=admin_menu_markup())
        return

    if action == "settings_wait_welcome":
        if not message.text:
            await message.reply_text("أرسل رسالة الترحيب كنص.")
            return
        await set_setting("welcome", message.text)
        admin_state_reset(context)
        await message.reply_text("تم تحديث رسالة الترحيب.", reply_markup=admin_menu_markup())
        return

    # BROADCAST
    if action == "broadcast_wait_text":
        if not message.text or not message.text.strip():
            await message.reply_text("أرسل نص الإعلان.")
            return
        await perform_broadcast(update, context, message.text.strip())
        admin_state_reset(context)
        return


async def finish_add_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_admin_state(context)
    try:
        subject_id = int(state["subject_id"])
        kind = state["kind"]
        title = state["title"]
        teacher = state.get("teacher") if kind == "note" else None
        file_id = state["file_id_new"]
        exists = await db_execute("SELECT id FROM files WHERE file_id=?", (file_id,), fetch="one")
        if exists:
            await update.effective_message.reply_text("هذا الملف موجود مسبقاً.", reply_markup=admin_menu_markup())
            admin_state_reset(context)
            return
        await db_execute(
            """
            INSERT INTO files(subject_id, kind, title, teacher, file_id, file_type, visible, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, 'document', 1, ?, ?)
            """,
            (subject_id, kind, title, teacher, file_id, now_iso(), now_iso()),
        )
        admin_state_reset(context)
        await update.effective_message.reply_text("تمت إضافة الملف بنجاح.", reply_markup=admin_menu_markup())
    except (KeyError, ValueError, sqlite3.Error) as exc:
        logger.exception("finish_add_file failed: %s", exc)
        await update.effective_message.reply_text("تعذر إضافة الملف بسبب خطأ داخلي. لم يتم تسجيل إضافة ناقصة.", reply_markup=admin_menu_markup())
        admin_state_reset(context)


async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    rows = await db_execute("SELECT user_id FROM users WHERE blocked=0", fetch="all")
    sent = 0
    failed = 0
    blocked = 0
    for row in rows:
        uid = row["user_id"]
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Forbidden:
            blocked += 1
            failed += 1
            await db_execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)

    await update.effective_message.reply_text(
        f"انتهى الإعلان.\n\nتم الإرسال: {sent}\nفشل الإرسال: {failed}\nالمستخدمون المحظورون المكتشفون: {blocked}",
        reply_markup=admin_menu_markup(),
    )

# ============================================================
# ERROR HANDLER
# ============================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=context.error)

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("صار خطأ غير متوقع. حاول مرة ثانية.")
    except Exception:
        pass

# ============================================================
# STARTUP
# ============================================================


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("ضع توكن البوت في BOT_TOKEN أو في متغير البيئة BOT_TOKEN قبل التشغيل.")

    init_db()
    logger.info("Database initialized.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", owner_panel))
    application.add_handler(CallbackQueryHandler(callbacks))
    async def non_command_router(update, context):
        user = update.effective_user
        if user and is_owner(user.id):
            await admin_messages(update, context)
            return
        if update.effective_message and update.effective_message.text:
            await main_menu_buttons(update, context)

    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, non_command_router), group=0)
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

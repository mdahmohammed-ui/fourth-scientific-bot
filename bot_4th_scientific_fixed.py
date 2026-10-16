import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedDocument,
    KeyboardButton,
    ReplyKeyboardMarkup,
    SwitchInlineQueryChosenChat,
    Update,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# Security: set BOT_TOKEN in your hosting environment; do not hard-code it.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 7683201905
DEFAULT_CHANNEL = "@Kii_8i"
DB_FILE = os.getenv("DB_FILE", "fourth_scientific_bot.db")
SUB_CACHE_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

SUBJECTS = [
    ("رياضيات", "📐", "primary", "ch"),
    ("كيمياء", "🧪", "primary", "ch"),
    ("فيزياء", "⚡", "primary", "ch"),
    ("أحياء", "🧬", "success", "ch"),
    ("الحاسوب", "💻", "success", "ch"),
    ("English", "🇬🇧", "danger", "unit"),
    ("اللغة العربية", "📚", "success", "unit"),
    ("التربية الإسلامية", "📖", "danger", "unit"),
    ("جرائم حزب البعث", "⚖️", "danger", "ch"),
]

HOME_TEXT = "هلا بيك ببوت الرابع العلمي 🎓\nاختر من الأزرار بالأسفل:"
GATE_TEXT = "حتى تستخدم البوت، اشترك بالقناة أولاً، ثم اضغط التحقق من الاشتراك."
BACK_TXT = "🔙 رجوع"
CANCEL_TXT = "❌ إلغاء"

CH_LABELS = {"ch1": "الفصل الأول", "ch2": "الفصل الثاني", "ch3": "الفصل الثالث"}
UNIT_LABELS = {"u1": "الوحدة الأولى", "u2": "الوحدة الثانية", "u3": "الوحدة الثالثة"}
SECTION_NAMES = {**CH_LABELS, **UNIT_LABELS}
Q_SECTIONS = {"ch1": "الفصل الأول", "half": "نصف السنة", "ch2": "الفصل الثاني"}
Q_MONTHS = {"m1": "الشهر الأول", "m2": "الشهر الثاني"}

ADMIN_ACTIONS = {
    "➕ إضافة ملف": "addfile",
    "🆔 إضافة ملف بـ ID": "addid",
    "➕ إضافة اسئلة": "addquestion",
    "➕ إضافة ملخصات": "addsummary",
    "✏️ تعديل ملف": "editfile",
    "🔁 استبدال ملف": "replacefile",
    "🗑 حذف ملف": "deletefile",
    "👁 إخفاء/إظهار ملف": "togglefile",
    "➕ إضافة مادة": "addsubject",
    "✏️ تعديل مادة": "editsubject",
    "🗑 حذف مادة": "deletesubject",
    "🔎 التعرف على ID": "identify",
    "🔗 إنشاء رابط": "genlink",
    "📢 إعلان": "broadcast",
    "📣 تغيير القناة": "channel",
}

FIRST_STEP = {
    "addfile": "subject", "addid": "subject", "addsummary": "subject",
    "addquestion": "qsection", "addsubject": "sname", "editsubject": "subject",
    "deletesubject": "subject", "editfile": "file", "replacefile": "file",
    "deletefile": "file", "togglefile": "file", "identify": "wait",
    "genlink": "fid", "broadcast": "text", "channel": "text",
}

FIELD_COL = {
    "editsubject": {"✏️ تعديل الاسم": "name", "😀 تعديل الإيموجي": "emoji"},
    "editfile": {"✏️ تعديل الاسم": "title", "👨‍🏫 تعديل الأستاذ": "teacher"},
}

# One SQLite connection is retained for compatibility with the existing DB.
DB = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.execute("PRAGMA busy_timeout=30000")
DB_LOCK = asyncio.Lock()


def q(sql, p=()):
    return DB.execute(sql, p).fetchall()


def q1(sql, p=()):
    return DB.execute(sql, p).fetchone()


def x(sql, p=()):
    cur = DB.execute(sql, p)
    DB.commit()
    return cur


def init_db():
    DB.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, joined TEXT, blocked INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, emoji TEXT NOT NULL,
        color TEXT NOT NULL DEFAULT 'success', stype TEXT NOT NULL DEFAULT 'ch',
        pos INTEGER NOT NULL DEFAULT 0, visible INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER REFERENCES subjects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, section TEXT, month TEXT,
        title TEXT NOT NULL, teacher TEXT,
        file_id TEXT UNIQUE NOT NULL, visible INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    if not q1("SELECT 1 FROM settings WHERE key='seeded'"):
        for i, (name, emoji, color, stype) in enumerate(SUBJECTS, 1):
            x("INSERT OR IGNORE INTO subjects(name, emoji, color, stype, pos) VALUES(?,?,?,?,?)",
              (name, emoji, color, stype, i))
        x("INSERT INTO settings(key, value) VALUES('seeded', '1')")
    x("INSERT OR IGNORE INTO settings(key, value) VALUES('channel', ?)", (DEFAULT_CHANNEL,))
    x("INSERT OR IGNORE INTO settings(key, value) VALUES('sub_on', '1')")


def setting(key):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else ""


def upsert_user(uid):
    x("INSERT OR IGNORE INTO users(user_id, joined) VALUES(?, ?)",
      (uid, datetime.now().isoformat(timespec="seconds")))


def label(s):
    return f"{s['emoji']} {s['name']}"


def subject_by_label(text, visible_only=False):
    sql = "SELECT * FROM subjects" + (" WHERE visible=1" if visible_only else "") + " ORDER BY pos"
    for s in q(sql):
        if label(s) == text:
            return s
    return None


def insert_file(sid, kind, section, month, title, teacher, file_id):
    try:
        x("INSERT INTO files(subject_id, kind, section, month, title, teacher, file_id) VALUES(?,?,?,?,?,?,?)",
          (sid, kind, section, month, title, teacher, file_id))
        return True, None
    except sqlite3.IntegrityError:
        return False, "هذا الـ File ID موجود مسبقاً."


def kbtn(text, style=None):
    # Telegram clients may ignore button styles; unsupported styles must not break bot setup.
    try:
        return KeyboardButton(text, style=style) if style else KeyboardButton(text)
    except (TypeError, ValueError):
        return KeyboardButton(text)


def rkb(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


BACK = kbtn(BACK_TXT, "danger")
CANCEL = kbtn(CANCEL_TXT, "danger")


def home_markup(owner):
    rows, row = [], []
    for s in q("SELECT * FROM subjects WHERE visible=1 ORDER BY pos"):
        row.append(kbtn(label(s), s["color"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([kbtn("❓ الاسئلة", "primary")])
    if owner:
        rows.append([kbtn("🛠 لوحة المالك", "primary")])
    return rkb(rows)


def subject_markup():
    return rkb([
        [kbtn("📘 الكتب", "success"), kbtn("📙 الملازم", "success")],
        [kbtn("🧾 الملخصات", "success")],
        [BACK],
    ])


def owner_markup():
    return rkb([
        [kbtn("➕ إضافة ملف", "success"), kbtn("🆔 إضافة ملف بـ ID", "success")],
        [kbtn("➕ إضافة اسئلة", "success"), kbtn("➕ إضافة ملخصات", "success")],
        [kbtn("✏️ تعديل ملف", "success"), kbtn("🔁 استبدال ملف", "success")],
        [kbtn("🗑 حذف ملف", "danger"), kbtn("👁 إخفاء/إظهار ملف", "primary")],
        [kbtn("➕ إضافة مادة", "success"), kbtn("✏️ تعديل مادة", "success")],
        [kbtn("🗑 حذف مادة", "danger")],
        [kbtn("🆔 جميع الـ IDs", "primary"), kbtn("🔎 التعرف على ID", "primary")],
        [kbtn("🔗 إنشاء رابط", "success")],
        [kbtn("📊 الإحصائيات", "primary"), kbtn("📢 إعلان", "primary")],
        [kbtn("⚙️ تشغيل/إيقاف الاشتراك", "primary"), kbtn("📣 تغيير القناة", "primary")],
        [kbtn("🏠 القائمة الرئيسية", "danger")],
    ])


def channel_username():
    raw = setting("channel").strip()
    if raw.startswith("@"):
        return raw[1:]
    match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", raw)
    return match.group(1) if match else raw.lstrip("@")


def gate_markup(pending=None):
    ch = channel_username()
    rows = []
    if ch:
        rows.append([InlineKeyboardButton("📢 اشترك بالقناة", url=f"https://t.me/{ch}", style="primary")])
    rows.append([InlineKeyboardButton("✅ التحقق من الاشتراك", callback_data=f"chk:{pending or 0}", style="primary")])
    return InlineKeyboardMarkup(rows)


def share_markup(row_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📤 مشاركة الملف",
        switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
            query=str(row_id), allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True),
        style="success",
    )]])


def file_rows(ctx, rows, icon):
    choices, out = {}, []
    for r in rows:
        base = f"{icon} {r['title']}"[:60]
        text, n = base, 2
        while text in choices:
            text = f"{base} ({n})"
            n += 1
        choices[text] = r["id"]
        out.append([kbtn(text, "success")])
    ctx.user_data["choices"] = choices
    return out


def subj_rows():
    btns = [kbtn(label(s)) for s in q("SELECT * FROM subjects ORDER BY pos")]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def owner_file_rows():
    rows = q("SELECT * FROM files ORDER BY id")
    return [[kbtn(f"#{r['id']} {r['title']}"[:60])] for r in rows]


def build_screen(ctx, screen, owner):
    kind = screen[0]
    if kind == "home":
        return HOME_TEXT, home_markup(owner)
    if kind == "subject":
        s = q1("SELECT * FROM subjects WHERE id=?", (screen[1],))
        if not s:
            return HOME_TEXT, home_markup(owner)
        return f"{label(s)}\nاختر:", subject_markup()
    if kind == "list":
        sid, ftype = screen[1], screen[2]
        icon = "📘" if ftype == "book" else "📙"
        title = "📘 الكتب" if ftype == "book" else "📙 الملازم"
        rows = q("SELECT * FROM files WHERE subject_id=? AND kind=? AND visible=1 ORDER BY id", (sid, ftype))
        if not rows:
            ctx.user_data["choices"] = {}
            return f"{title}\nلا توجد ملفات حالياً.", rkb([[BACK]])
        return title, rkb(file_rows(ctx, rows, icon) + [[BACK]])
    if kind == "summaries":
        s = q1("SELECT * FROM subjects WHERE id=?", (screen[1],))
        if not s:
            return HOME_TEXT, home_markup(owner)
        labels = CH_LABELS if s["stype"] == "ch" else UNIT_LABELS
        ctx.user_data["choices"] = {}
        return f"🧾 الملخصات — {s['name']}\nاختر:", rkb([[kbtn(v, "success") for v in labels.values()], [BACK]])
    if kind == "questions":
        ctx.user_data["choices"] = {}
        return "❓ الاسئلة\nاختر:", rkb([
            [kbtn("الفصل الأول", "success"), kbtn("نصف السنة", "success"), kbtn("الفصل الثاني", "success")],
            [BACK],
        ])
    if kind == "qchapter":
        ctx.user_data["choices"] = {}
        return f"❓ {Q_SECTIONS.get(screen[1], 'الأسئلة')}\nاختر الشهر:", rkb([
            [kbtn(Q_MONTHS["m1"], "success"), kbtn(Q_MONTHS["m2"], "success")],
            [BACK],
        ])
    return HOME_TEXT, home_markup(owner)


def parent(screen):
    k = screen[0]
    if k in ("list", "summaries"):
        return ("subject", screen[1])
    if k == "subject":
        return ("home",)
    if k == "qchapter":
        return ("questions",)
    return ("home",)


async def show(msg, ctx, screen, owner):
    text, markup = build_screen(ctx, screen, owner)
    ctx.user_data["screen"] = screen
    await msg.reply_text(text, reply_markup=markup)


async def show_admin(msg, ctx):
    ctx.user_data.pop("flow", None)
    ctx.user_data["screen"] = ("admin",)
    await msg.reply_text("🛠 لوحة المالك", reply_markup=owner_markup())


async def finish(msg, ctx, text):
    ctx.user_data.pop("flow", None)
    await msg.reply_text(text, reply_markup=owner_markup())


SUB_CACHE = {}


async def is_sub(bot, uid):
    if uid == OWNER_ID or setting("sub_on") != "1":
        return True
    now = time.time()
    if SUB_CACHE.get(uid, 0) > now:
        return True
    try:
        member = await bot.get_chat_member(setting("channel"), uid)
    except TelegramError as e:
        log.warning("subscription check failed for channel %s: %s", setting("channel"), e)
        return False
    ok = member.status in ("member", "administrator", "creator", "restricted") and getattr(member, "is_member", True)
    if ok:
        SUB_CACHE[uid] = now + SUB_CACHE_SECONDS
    else:
        SUB_CACHE.pop(uid, None)
    return ok


async def send_item(ctx, chat_id, row_id, owner):
    row = q1("SELECT * FROM files WHERE id=?", (row_id,))
    if not row or (not row["visible"] and not owner):
        await ctx.bot.send_message(chat_id, "الملف غير متاح حالياً.")
        return
    if row["kind"] == "note":
        caption = f"📙 {row['title']}\n👨‍🏫 {row['teacher'] or ''}\n🏷 للصف الرابع الاعدادي"
    else:
        caption = row["title"]
    try:
        # File IDs are stored as supplied. send_document works for document IDs.
        await ctx.bot.send_document(chat_id, row["file_id"], caption=caption, reply_markup=share_markup(row["id"]))
    except TelegramError as e:
        log.error("send failed for file %s: %s", row["id"], e)
        await ctx.bot.send_message(chat_id, "تعذر إرسال الملف. أبلغ المالك.")


async def send_question(msg, ctx, sec, month, owner):
    r = q1("SELECT * FROM files WHERE kind='question' AND section=? AND month IS ? AND visible=1", (sec, month))
    if r:
        await send_item(ctx, msg.chat_id, r["id"], owner)
    else:
        await msg.reply_text("سيتم إضافة الأسئلة لاحقاً.")


async def student_text(msg, ctx, text, owner):
    screen = ctx.user_data.get("screen", ("home",))
    chat_id = msg.chat_id

    if text == BACK_TXT:
        await show(msg, ctx, parent(screen), owner)
        return

    choices = ctx.user_data.get("choices", {})
    if text in choices:
        await send_item(ctx, chat_id, choices[text], owner)
        return

    k = screen[0]
    if k == "home":
        if text == "❓ الاسئلة":
            await show(msg, ctx, ("questions",), owner)
            return
        if owner and text == "🛠 لوحة المالك":
            await show_admin(msg, ctx)
            return
        s = subject_by_label(text, visible_only=True)
        if s:
            await show(msg, ctx, ("subject", s["id"]), owner)
        return

    if k == "subject":
        sid = screen[1]
        if text == "📘 الكتب":
            await show(msg, ctx, ("list", sid, "book"), owner)
        elif text == "📙 الملازم":
            await show(msg, ctx, ("list", sid, "note"), owner)
        elif text == "🧾 الملخصات":
            await show(msg, ctx, ("summaries", sid), owner)
        return

    if k == "summaries":
        sid = screen[1]
        s = q1("SELECT * FROM subjects WHERE id=?", (sid,))
        if not s:
            await show(msg, ctx, ("home",), owner)
            return
        labels = CH_LABELS if s["stype"] == "ch" else UNIT_LABELS
        key = next((kk for kk, v in labels.items() if v == text), None)
        if key:
            r = q1("SELECT * FROM files WHERE subject_id=? AND kind='summary' AND section=? AND visible=1", (sid, key))
            if r:
                await send_item(ctx, chat_id, r["id"], owner)
            else:
                await msg.reply_text("سيتم إضافة الملخصات لاحقاً.")
        return

    if k == "questions":
        key = next((kk for kk, v in Q_SECTIONS.items() if v == text), None)
        if key == "half":
            await send_question(msg, ctx, "half", None, owner)
        elif key:
            await show(msg, ctx, ("qchapter", key), owner)
        return

    if k == "qchapter":
        mkey = next((kk for kk, v in Q_MONTHS.items() if v == text), None)
        if mkey:
            await send_question(msg, ctx, screen[1], mkey, owner)
        return


async def start_flow(msg, ctx, typ):
    ctx.user_data["flow"] = {"type": typ, "step": FIRST_STEP[typ]}
    await prompt(msg, ctx)


async def prompt(msg, ctx):
    f = ctx.user_data["flow"]
    step, typ = f["step"], f["type"]
    rows, text = [], ""
    if step == "subject":
        text, rows = "اختر المادة:", subj_rows()
    elif step == "kind":
        text, rows = "اختر النوع:", [[kbtn("📘 كتاب"), kbtn("📙 ملزمة")]]
    elif step == "doc":
        text = "أرسل الملف الآن كـ Document."
    elif step == "fid":
        text = "أرسل الـ File ID كنص."
    elif step == "title":
        text = "أرسل اسم الملف."
    elif step == "teacher":
        text = "أرسل اسم الأستاذ."
    elif step == "qsection":
        text, rows = "اختر القسم:", [[kbtn("الفصل الأول"), kbtn("نصف السنة"), kbtn("الفصل الثاني")]]
    elif step == "qmonth":
        text, rows = "اختر الشهر:", [[kbtn("الشهر الأول"), kbtn("الشهر الثاني")]]
    elif step == "ssection":
        s = q1("SELECT * FROM subjects WHERE id=?", (f["sid"],))
        labels = CH_LABELS if s and s["stype"] == "ch" else UNIT_LABELS
        text, rows = "اختر الفصل أو الوحدة:", [[kbtn(v) for v in labels.values()]]
    elif step == "sname":
        text = "أرسل اسم المادة الجديدة."
    elif step == "semoji":
        text = "أرسل إيموجي المادة."
    elif step == "field":
        text = "اختر ما تريد تعديله:"
        rows = [[kbtn(t) for t in FIELD_COL[typ]]]
    elif step == "value":
        text = "أرسل القيمة الجديدة."
    elif step == "confirm":
        text, rows = "هل تؤكد حذف المادة وكل ملفاتها؟", [[kbtn("✅ تأكيد الحذف", "danger")]]
    elif step == "file":
        text, rows = "اختر الملف، أو أرسل رقمه مثل #12:", owner_file_rows()
    elif step == "wait":
        text = "أرسل أي ملف الآن لأستخرج الـ ID. (للخروج اضغط إلغاء)"
    elif step == "text":
        text = "أرسل النص الآن."
    await msg.reply_text(text, reply_markup=rkb(rows + [[CANCEL]]))


def media_file_id(msg):
    if msg.document:
        return msg.document.file_id
    if msg.photo:
        return msg.photo[-1].file_id
    if msg.video:
        return msg.video.file_id
    if msg.audio:
        return msg.audio.file_id
    if msg.voice:
        return msg.voice.file_id
    if msg.animation:
        return msg.animation.file_id
    if msg.sticker:
        return msg.sticker.file_id
    return None


async def retry(msg, ctx, hint):
    await msg.reply_text(hint)
    await prompt(msg, ctx)


async def do_broadcast(ctx, text):
    ok = fail = 0
    for u in q("SELECT user_id FROM users WHERE blocked=0"):
        try:
            await ctx.bot.send_message(u["user_id"], text)
            ok += 1
        except Forbidden:
            fail += 1
            x("UPDATE users SET blocked=1 WHERE user_id=?", (u["user_id"],))
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def handle_flow(msg, ctx):
    f = ctx.user_data.get("flow")
    if not f:
        return False
    text = (msg.text or "").strip()
    typ, step = f["type"], f["step"]
    mid = media_file_id(msg)

    if step == "subject":
        s = subject_by_label(text)
        if not s:
            await retry(msg, ctx, "اختر المادة من الأزرار.")
            return True
        f["sid"] = s["id"]
        f["step"] = {"addfile": "kind", "addid": "kind", "addsummary": "ssection",
                     "editsubject": "field", "deletesubject": "confirm"}[typ]
        await prompt(msg, ctx)
        return True

    if step == "kind":
        k = {"📘 كتاب": "book", "📙 ملزمة": "note"}.get(text)
        if not k:
            await retry(msg, ctx, "اختر النوع من الأزرار.")
            return True
        f["kind"] = k
        f["step"] = "doc" if typ == "addfile" else "fid"
        await prompt(msg, ctx)
        return True

    if step == "doc":
        if not msg.document:
            await retry(msg, ctx, "أرسل الملف كـ Document.")
            return True
        if typ == "replacefile":
            try:
                x("UPDATE files SET file_id=? WHERE id=?", (msg.document.file_id, f["file"]))
            except sqlite3.IntegrityError:
                await finish(msg, ctx, "هذا الملف مستخدم في ملف آخر.")
                return True
            await finish(msg, ctx, "✅ تم استبدال الملف.")
            return True
        f["fid"] = msg.document.file_id
        f["step"] = "title"
        await prompt(msg, ctx)
        return True

    if step == "fid":
        if not text or any(ch.isspace() for ch in text):
            await retry(msg, ctx, "أرسل File ID صحيحاً كنص بدون مسافات.")
            return True
        if typ == "genlink":
            row = q1("SELECT id FROM files WHERE file_id=?", (text,))
            if not row:
                await finish(msg, ctx, "لم أجد ملفاً بهذا الـ ID في البوت.")
                return True
            link = f"https://t.me/{ctx.bot.username}?start=f{row['id']}"
            await finish(msg, ctx, f"🔗 رابط الملف:\n{link}")
            return True
        if typ == "addid":
            f["fid"] = text
            f["step"] = "title"
            await prompt(msg, ctx)
            return True
        if typ == "addsummary":
            s = q1("SELECT name FROM subjects WHERE id=?", (f["sid"],))
            title = f"ملخص {s['name']} - {SECTION_NAMES[f['section']]}"
            ok, err = insert_file(f["sid"], "summary", f["section"], None, title, None, text)
            await finish(msg, ctx, "✅ تمت إضافة الملخص." if ok else err)
            return True
        if typ == "addquestion":
            title = f"أسئلة {Q_SECTIONS[f['section']]}" + (f" - {Q_MONTHS[f['month']]}" if f.get("month") else "")
            ok, err = insert_file(None, "question", f["section"], f.get("month"), title, None, text)
            await finish(msg, ctx, "✅ تمت إضافة الأسئلة." if ok else err)
            return True

    if step == "title":
        if not text:
            await retry(msg, ctx, "أرسل الاسم كنص.")
            return True
        f["title"] = text
        if f.get("kind") == "note":
            f["step"] = "teacher"
            await prompt(msg, ctx)
            return True
        ok, err = insert_file(f["sid"], f["kind"], None, None, f["title"], None, f["fid"])
        await finish(msg, ctx, "✅ تمت إضافة الملف." if ok else err)
        return True

    if step == "teacher":
        if not text:
            await retry(msg, ctx, "أرسل اسم الأستاذ كنص.")
            return True
        ok, err = insert_file(f["sid"], f["kind"], None, None, f["title"], text, f["fid"])
        await finish(msg, ctx, "✅ تمت إضافة الملزمة." if ok else err)
        return True

    if step == "qsection":
        key = next((kk for kk, v in Q_SECTIONS.items() if v == text), None)
        if not key:
            await retry(msg, ctx, "اختر القسم من الأزرار.")
            return True
        f["section"] = key
        if key == "half":
            f["month"] = None
            f["step"] = "fid"
        else:
            f["step"] = "qmonth"
        await prompt(msg, ctx)
        return True

    if step == "qmonth":
        key = next((kk for kk, v in Q_MONTHS.items() if v == text), None)
        if not key:
            await retry(msg, ctx, "اختر الشهر من الأزرار.")
            return True
        f["month"] = key
        f["step"] = "fid"
        await prompt(msg, ctx)
        return True

    if step == "ssection":
        s = q1("SELECT * FROM subjects WHERE id=?", (f["sid"],))
        labels = CH_LABELS if s and s["stype"] == "ch" else UNIT_LABELS
        key = next((kk for kk, v in labels.items() if v == text), None)
        if not key:
            await retry(msg, ctx, "اختر الفصل أو الوحدة من الأزرار.")
            return True
        f["section"] = key
        f["step"] = "fid"
        await prompt(msg, ctx)
        return True

    if step == "sname":
        if not text:
            await retry(msg, ctx, "أرسل اسم المادة كنص.")
            return True
        f["sname"] = text
        f["step"] = "semoji"
        await prompt(msg, ctx)
        return True

    if step == "semoji":
        if not text:
            await retry(msg, ctx, "أرسل إيموجي المادة.")
            return True
        pos = q1("SELECT COALESCE(MAX(pos), 0) m FROM subjects")["m"] + 1
        try:
            x("INSERT INTO subjects(name, emoji, color, stype, pos) VALUES(?,?,?,?,?)",
              (f["sname"], text, "success", "ch", pos))
            await finish(msg, ctx, "✅ تمت إضافة المادة.")
        except sqlite3.IntegrityError:
            await finish(msg, ctx, "هذه المادة موجودة مسبقاً.")
        return True

    if step == "field":
        col = FIELD_COL[typ].get(text)
        if not col:
            await retry(msg, ctx, "اختر من الأزرار.")
            return True
        f["col"] = col
        f["step"] = "value"
        await prompt(msg, ctx)
        return True

    if step == "value":
        if not text:
            await retry(msg, ctx, "أرسل القيمة كنص.")
            return True
        if typ == "editsubject":
            try:
                x(f"UPDATE subjects SET {f['col']}=? WHERE id=?", (text, f["sid"]))
            except sqlite3.IntegrityError:
                await finish(msg, ctx, "هذا الاسم مستخدم من مادة أخرى.")
                return True
        else:
            x(f"UPDATE files SET {f['col']}=? WHERE id=?", (text, f["file"]))
        await finish(msg, ctx, "✅ تم التعديل.")
        return True

    if step == "confirm":
        if text != "✅ تأكيد الحذف":
            await retry(msg, ctx, "اضغط ✅ تأكيد الحذف أو إلغاء.")
            return True
        x("DELETE FROM subjects WHERE id=?", (f["sid"],))
        await finish(msg, ctx, "تم حذف المادة وملفاتها.")
        return True

    if step == "file":
        m = re.fullmatch(r"#?(\d+)", text)
        row = q1("SELECT * FROM files WHERE id=?", (int(m.group(1)),)) if m else None
        if not row:
            await retry(msg, ctx, "اختر الملف من الأزرار أو أرسل رقمه مثل #12.")
            return True
        if typ == "deletefile":
            x("DELETE FROM files WHERE id=?", (row["id"],))
            await finish(msg, ctx, "تم حذف الملف.")
            return True
        if typ == "togglefile":
            x("UPDATE files SET visible = 1 - visible WHERE id=?", (row["id"],))
            state = "مخفي" if row["visible"] else "ظاهر"
            await finish(msg, ctx, f"تم. الحالة الآن: {state}")
            return True
        f["file"] = row["id"]
        f["step"] = "doc" if typ == "replacefile" else "field"
        await prompt(msg, ctx)
        return True

    if step == "wait":
        if not mid:
            await retry(msg, ctx, "أرسل ملفاً أو صورة أو فيديو.")
            return True
        await msg.reply_text(f"🆔 الـ ID:\n{mid}\n\nأرسل ملفاً آخر، أو اضغط إلغاء.")
        return True

    if step == "text":
        if not text:
            await retry(msg, ctx, "أرسل النص كنص.")
            return True
        if typ == "channel":
            channel = text.strip()
            if not (channel.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,}", channel)):
                await retry(msg, ctx, "الصيغة الصحيحة: @Channel")
                return True
            x("UPDATE settings SET value=? WHERE key='channel'", (channel,))
            SUB_CACHE.clear()
            await finish(msg, ctx, "✅ تم تغيير القناة.")
            return True
        ok, fail = await do_broadcast(ctx, text)
        await finish(msg, ctx, f"تم الإرسال.\nنجح: {ok}\nفشل: {fail}")
        return True

    return False


async def send_all_ids(msg):
    rows = q("SELECT f.*, s.name AS sn FROM files f LEFT JOIN subjects s ON s.id=f.subject_id ORDER BY f.id")
    if not rows:
        await msg.reply_text("لا توجد ملفات.")
        return
    lines = []
    for r in rows:
        where = r["sn"] or "الأسئلة"
        if r["section"]:
            where += f" | {SECTION_NAMES.get(r['section'], Q_SECTIONS.get(r['section'], r['section']))}"
        if r["month"]:
            where += f" | {Q_MONTHS.get(r['month'], r['month'])}"
        lines.append(f"#{r['id']} | {r['kind']} | {where} | {r['title']}\n{r['file_id']}")
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 2 > 3500:
            if cur.strip():
                chunks.append(cur.strip())
            cur = ""
        cur += line + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    for c in chunks:
        await msg.reply_text(c)


async def send_stats(msg):
    users = q1("SELECT COUNT(*) c FROM users")["c"]
    blocked = q1("SELECT COUNT(*) c FROM users WHERE blocked=1")["c"]
    subs = q1("SELECT COUNT(*) c FROM subjects WHERE visible=1")["c"]
    counts = {k: q1("SELECT COUNT(*) c FROM files WHERE kind=?", (k,))["c"]
              for k in ("book", "note", "summary", "question")}
    hidden = q1("SELECT COUNT(*) c FROM files WHERE visible=0")["c"]
    await msg.reply_text(
        "📊 الإحصائيات\n\n"
        f"المستخدمون: {users}\n"
        f"المحظورون: {blocked}\n"
        f"المواد الظاهرة: {subs}\n"
        f"الكتب: {counts['book']}\n"
        f"الملازم: {counts['note']}\n"
        f"الملخصات: {counts['summary']}\n"
        f"الأسئلة: {counts['question']}\n"
        f"الملفات المخفية: {hidden}",
        reply_markup=owner_markup(),
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    uid = user.id
    upsert_user(uid)
    owner = uid == OWNER_ID
    ctx.user_data.pop("flow", None)

    pending = None
    if ctx.args and re.fullmatch(r"f\d+", ctx.args[0]):
        pending = int(ctx.args[0][1:])

    if not owner and not await is_sub(ctx.bot, uid):
        await msg.reply_text(GATE_TEXT, reply_markup=gate_markup(pending))
        return

    if pending:
        await send_item(ctx, msg.chat_id, pending, owner)
    await show(msg, ctx, ("home",), owner)


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await show_admin(update.effective_message, ctx)


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await finish(update.effective_message, ctx, "تم الإلغاء.")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    if not cb:
        return
    if not cb.data or not cb.data.startswith("chk:"):
        await cb.answer()
        return
    uid = cb.from_user.id
    try:
        pending = int(cb.data[4:]) or None
    except ValueError:
        pending = None
    SUB_CACHE.pop(uid, None)
    if not await is_sub(ctx.bot, uid):
        await cb.answer("لم تشترك بعد، أو تعذّر التحقق من الاشتراك.", show_alert=True)
        return
    await cb.answer("تم التحقق ✅")
    owner = uid == OWNER_ID
    if pending:
        await send_item(ctx, cb.message.chat_id, pending, owner)
    ctx.user_data["screen"] = ("home",)
    await ctx.bot.send_message(cb.message.chat_id, HOME_TEXT, reply_markup=home_markup(owner))


async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    results = []
    if query.isdigit():
        row = q1("SELECT * FROM files WHERE id=? AND visible=1", (int(query),))
        if row:
            results.append(InlineQueryResultCachedDocument(
                id=str(row["id"]), title=row["title"], document_file_id=row["file_id"]))
    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    uid = user.id
    upsert_user(uid)
    owner = uid == OWNER_ID
    text = (msg.text or "").strip()

    if not owner and not await is_sub(ctx.bot, uid):
        await msg.reply_text(GATE_TEXT, reply_markup=gate_markup())
        return

    if owner:
        if text == CANCEL_TXT:
            await finish(msg, ctx, "تم الإلغاء.")
            return
        if text in ADMIN_ACTIONS:
            await start_flow(msg, ctx, ADMIN_ACTIONS[text])
            return
        if text == "🛠 لوحة المالك":
            await show_admin(msg, ctx)
            return
        if text == "🏠 القائمة الرئيسية":
            ctx.user_data.pop("flow", None)
            await show(msg, ctx, ("home",), True)
            return
        if text == "🆔 جميع الـ IDs":
            await send_all_ids(msg)
            return
        if text == "📊 الإحصائيات":
            await send_stats(msg)
            return
        if text == "⚙️ تشغيل/إيقاف الاشتراك":
            x("UPDATE settings SET value = CASE WHEN value='1' THEN '0' ELSE '1' END WHERE key='sub_on'")
            SUB_CACHE.clear()
            status = "مفعّل" if setting("sub_on") == "1" else "معطّل"
            await msg.reply_text(f"الاشتراك الإجباري الآن: {status}", reply_markup=owner_markup())
            return
        if await handle_flow(msg, ctx):
            return

    await student_text(msg, ctx, text, owner)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id == OWNER_ID and ctx.user_data.get("flow"):
        await handle_flow(update.effective_message, ctx)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    log.exception("Unhandled bot error", exc_info=err)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("ضع توكن جديداً في متغير البيئة BOT_TOKEN قبل التشغيل.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO
         | filters.VOICE | filters.ANIMATION | filters.Sticker.ALL) & ~filters.COMMAND,
        on_media,
    ))
    app.add_error_handler(on_error)
    log.info("Bot is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

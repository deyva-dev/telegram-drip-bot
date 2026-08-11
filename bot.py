"""
Telegram Drip Content Bot
--------------------------
Delivers your academy content to each subscriber on a fixed schedule
(Day 0, Day 3, Day 7, ...) counted from THEIR OWN join date — not the
calendar date. So it doesn't matter when someone joins, they always
start from lesson 1 and progress at the same pace as everyone else.

Content is managed entirely through bot commands (/addcontent,
/listcontent, /deletecontent) and stored in the database — so adding
next week's lesson never requires a redeploy or touching code.

Each piece of content can also have a DELAY (in minutes) after its
day begins — so a single day's lesson can unfold as several messages
(text, then an image, then more text...) spaced out naturally instead
of all landing at once.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in your bot token
    python bot.py
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@")  # e.g. "gloria_studyquay" (no @)
DB_PATH = os.getenv("DB_PATH", "drip_bot.db")
SEED_CONTENT_PATH = os.getenv("SEED_CONTENT_PATH", "content.json")
# Checked frequently (default: every 5 min) so that within-day delays
# (e.g. "10 minutes after Day 7 starts") fire close to on time. If you
# only ever use whole-day scheduling with no delays, this can safely be
# a larger number (e.g. 3600 for hourly) to reduce resource usage.
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for /addcontent
ASK_DAY, ASK_DELAY, ASK_TYPE, ASK_CONTENT, ASK_CAPTION, CONFIRM = range(6)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT NOT NULL,
            paused INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_log (
            chat_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, content_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day INTEGER NOT NULL,
            delay_minutes INTEGER NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            caption TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def migrate_db():
    """Adds delay_minutes to older databases created before this feature existed."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(content)")
    columns = [row[1] for row in cur.fetchall()]
    if "delay_minutes" not in columns:
        cur.execute("ALTER TABLE content ADD COLUMN delay_minutes INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migrated content table: added delay_minutes column")
    conn.close()


def seed_content_from_json():
    """One-time convenience: if the content table is empty and a
    content.json file exists (e.g. from an earlier setup), import it
    so you're not starting from zero."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM content")
    count = cur.fetchone()[0]
    conn.close()
    if count > 0 or not os.path.exists(SEED_CONTENT_PATH):
        return
    with open(SEED_CONTENT_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)
    for item in items:
        add_content_item(
            item.get("day", 0),
            item.get("type", "text"),
            item.get("content", ""),
            item.get("caption"),
            item.get("delay_minutes", 0),
        )
    logger.info("Seeded %d content items from %s", len(items), SEED_CONTENT_PATH)


def add_subscriber(chat_id, username, first_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM subscribers WHERE chat_id = ?", (chat_id,))
    existing = cur.fetchone()
    if not existing:
        cur.execute(
            "INSERT INTO subscribers (chat_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
            (chat_id, username, first_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    conn.close()
    return existing is None  # True if this person is newly enrolled


def get_active_subscribers():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id, joined_at FROM subscribers WHERE paused = 0")
    rows = cur.fetchall()
    conn.close()
    return rows


def already_sent(chat_id, content_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_log WHERE chat_id = ? AND content_id = ?", (chat_id, content_id))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(chat_id, content_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_log (chat_id, content_id, sent_at) VALUES (?, ?, ?)",
        (chat_id, content_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def add_content_item(day, ctype, content, caption, delay_minutes=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO content (day, delay_minutes, type, content, caption, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (day, delay_minutes, ctype, content, caption, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def load_content():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, day, delay_minutes, type, content, caption FROM content ORDER BY day, delay_minutes, id")
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "day": r[1], "delay_minutes": r[2], "type": r[3], "content": r[4], "caption": r[5]}
        for r in rows
    ]


def delete_content_item(content_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM content WHERE id = ?", (content_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


# ---------------------------------------------------------------------------
# Sending content
# ---------------------------------------------------------------------------

async def send_content_item(bot, chat_id, item):
    content_type = item.get("type", "text")
    caption = item.get("caption")
    body = item.get("content", "")

    try:
        if content_type == "text":
            await bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        elif content_type == "photo":
            await bot.send_photo(chat_id=chat_id, photo=body, caption=caption)
        elif content_type == "document":
            await bot.send_document(chat_id=chat_id, document=body, caption=caption)
        elif content_type == "link":
            text = f"{caption}\n{body}" if caption else body
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=False)
        else:
            logger.warning("Unknown content type: %s", content_type)
            return False
        return True
    except Exception as e:
        logger.error("Failed to send content id=%s to chat_id=%s: %s", item.get("id"), chat_id, e)
        return False


async def check_and_send(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    content_items = load_content()
    subscribers = get_active_subscribers()
    now = datetime.now(timezone.utc)

    for chat_id, joined_at in subscribers:
        joined_dt = datetime.fromisoformat(joined_at)

        for item in content_items:
            if already_sent(chat_id, item["id"]):
                continue
            due_at = joined_dt + timedelta(days=item["day"], minutes=item.get("delay_minutes", 0) or 0)
            if now < due_at:
                continue
            success = await send_content_item(bot, chat_id, item)
            if success:
                mark_sent(chat_id, item["id"])
            await asyncio.sleep(0.5)  # gentle pacing to avoid rate limits


# ---------------------------------------------------------------------------
# Subscriber-facing commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    is_new = add_subscriber(chat_id, user.username, user.first_name)

    if is_new:
        await update.message.reply_text(
            "Welcome! You're enrolled. Your content drip starts today, on the same "
            "schedule as everyone else — counting from now, not from any fixed calendar date."
        )
        await check_and_send(context)  # deliver anything due for Day 0 with 0 delay right away
    else:
        await update.message.reply_text("You're already enrolled — your next lesson is on its way.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE subscribers SET paused = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("Paused. Send /resume anytime to continue where you left off.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE subscribers SET paused = 0 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("Resumed! You'll keep getting content from where you left off.")
    await check_and_send(context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "<b>Commands</b>",
        "/start — enroll (or check in)",
        "/pause — pause your drip",
        "/resume — resume your drip",
    ]
    if is_admin(update.effective_user.id):
        lines += [
            "",
            "<b>Admin</b>",
            "/addcontent — add a new piece of content (guided, step by step)",
            "/listcontent — see everything scheduled",
            "/deletecontent &lt;id&gt; — remove a scheduled item",
            "/stats — subscriber counts",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def redirect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches anything a subscriber sends that isn't a recognized command
    (this bot doesn't hold conversations) and points them to a real DM
    instead of leaving them on read."""
    user = update.effective_user
    if not update.message or (user and is_admin(user.id)):
        return  # never intercepts the admin — needed so /addcontent's own
                 # text/photo/document replies aren't swallowed by this

    if ADMIN_USERNAME:
        link = f"https://t.me/{ADMIN_USERNAME}"
        text = (
            "Thanks for your message! This chat is just for delivering your lessons on schedule, "
            f"so I can't reply here. For questions or anything else, message me directly: {link}"
        )
    else:
        text = "Thanks for your message! This chat is just for delivering your lessons on schedule."

    await update.message.reply_text(text, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Admin: stats
# ---------------------------------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM subscribers")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM subscribers WHERE paused = 1")
    paused = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Subscribers: {total}\nPaused: {paused}\nActive: {total - paused}")


# ---------------------------------------------------------------------------
# Admin: /addcontent conversation
# ---------------------------------------------------------------------------

async def addcontent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "Let's add new content.\n\n"
        "What day should this go out on? (0 = the day someone joins, "
        "7 = one week in, 14 = two weeks in, etc.)\n\n"
        "Send /cancel anytime to stop."
    )
    return ASK_DAY


async def ask_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        day = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("That doesn't look like a number. How many days after joining should this go out?")
        return ASK_DAY
    if day < 0:
        await update.message.reply_text("Day should be 0 or more. Try again.")
        return ASK_DAY

    context.user_data["day"] = day
    keyboard = [
        [InlineKeyboardButton("Right away", callback_data="0")],
        [InlineKeyboardButton("+15 min", callback_data="15"), InlineKeyboardButton("+30 min", callback_data="30")],
        [InlineKeyboardButton("+1 hour", callback_data="60"), InlineKeyboardButton("+2 hours", callback_data="120")],
        [InlineKeyboardButton("Custom (type a number)", callback_data="custom")],
    ]
    await update.message.reply_text(
        f"Got it — Day {day}.\n\n"
        "Should this go out right when Day " + str(day) + " starts, or a bit later "
        "(useful for spacing out multiple messages within the same day's lesson)?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_DELAY


async def ask_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "custom":
        await query.edit_message_text("How many minutes after Day " + str(context.user_data["day"]) + " starts? (type a number, e.g. 45)")
        return ASK_DELAY  # stays in this state, but now expects a text reply — see ask_delay_custom below

    context.user_data["delay_minutes"] = int(query.data)
    return await proceed_to_type_selection(update, context)


async def ask_delay_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the typed-number reply after 'Custom' was tapped."""
    try:
        minutes = int(update.message.text.strip())
        if minutes < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid number of minutes. Try again, e.g. 45")
        return ASK_DELAY
    context.user_data["delay_minutes"] = minutes
    return await proceed_to_type_selection(update, context)


async def proceed_to_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 Text", callback_data="text"), InlineKeyboardButton("🖼 Photo", callback_data="photo")],
        [InlineKeyboardButton("📄 Document", callback_data="document"), InlineKeyboardButton("🔗 Link", callback_data="link")],
    ]
    target = update.effective_message
    await target.reply_text("What type of content is this?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_TYPE


async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    content_type = query.data
    context.user_data["type"] = content_type

    prompts = {
        "text": "Send me the text now. Basic HTML is supported: <b>bold</b>, <i>italic</i>, <a href=\"URL\">link text</a>.",
        "photo": "Send me the photo now — just upload it directly. You can add a caption on the photo itself, or I'll ask separately after.",
        "document": "Send me the file now — just upload it directly (PDF, worksheet, etc). You can add a caption directly, or I'll ask separately after.",
        "link": "Send me the URL now.",
    }
    await query.edit_message_text(prompts[content_type])
    return ASK_CONTENT


async def ask_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content_type = context.user_data["type"]
    msg = update.message

    if content_type == "text":
        context.user_data["content"] = msg.text
        return await ask_caption_prompt(update, context)

    if content_type == "link":
        context.user_data["content"] = msg.text.strip()
        return await ask_caption_prompt(update, context)

    if content_type == "photo":
        if not msg.photo:
            await msg.reply_text("That's not a photo — please upload an image, or /cancel to stop.")
            return ASK_CONTENT
        context.user_data["content"] = msg.photo[-1].file_id
        if msg.caption:
            context.user_data["caption"] = msg.caption
            return await show_confirmation(update, context)
        return await ask_caption_prompt(update, context)

    if content_type == "document":
        if not msg.document:
            await msg.reply_text("That's not a file — please upload a document, or /cancel to stop.")
            return ASK_CONTENT
        context.user_data["content"] = msg.document.file_id
        if msg.caption:
            context.user_data["caption"] = msg.caption
            return await show_confirmation(update, context)
        return await ask_caption_prompt(update, context)

    return ConversationHandler.END


async def ask_caption_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Want a caption or extra text with this? Send it now, or /skip.")
    return ASK_CAPTION


async def ask_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["caption"] = update.message.text
    return await show_confirmation(update, context)


async def skip_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["caption"] = None
    return await show_confirmation(update, context)


async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    preview = str(d.get("content", ""))
    if len(preview) > 80:
        preview = preview[:80] + "..."
    delay = d.get("delay_minutes", 0)
    timing = "immediately" if delay == 0 else f"{delay} minutes after Day {d['day']} starts"
    summary = (
        "Here's what I've got:\n\n"
        f"Day: {d['day']}\n"
        f"Timing: {timing}\n"
        f"Type: {d['type']}\n"
        f"Content: {preview}\n"
        f"Caption: {d.get('caption') or '(none)'}\n\n"
        "Save this?"
    )
    keyboard = [[InlineKeyboardButton("✅ Save", callback_data="save"), InlineKeyboardButton("❌ Discard", callback_data="discard")]]
    await update.effective_message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "save":
        d = context.user_data
        content_id = add_content_item(d["day"], d["type"], d["content"], d.get("caption"), d.get("delay_minutes", 0))
        delay = d.get("delay_minutes", 0)
        timing = "right when it starts" if delay == 0 else f"{delay} minutes after it starts"
        await query.edit_message_text(
            f"Saved as content #{content_id}, scheduled for Day {d['day']} ({timing}). "
            "It'll go out automatically — no redeploy needed."
        )
    else:
        await query.edit_message_text("Discarded — nothing was saved.")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled — nothing was saved.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Admin: list / delete content
# ---------------------------------------------------------------------------

async def listcontent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    items = load_content()
    if not items:
        await update.message.reply_text("No content yet. Use /addcontent to add your first piece.")
        return

    lines = []
    for it in items:
        preview = str(it["content"])
        if len(preview) > 40:
            preview = preview[:40] + "..."
        delay = it.get("delay_minutes", 0)
        timing = "Day {}".format(it["day"]) if not delay else f"Day {it['day']} +{delay}m"
        lines.append(f"#{it['id']} | {timing} | {it['type']} | {preview}")
    text = "\n".join(lines)

    for start_idx in range(0, len(text), 3500):
        await update.message.reply_text(text[start_idx:start_idx + 3500])


async def deletecontent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /deletecontent <id>  (use /listcontent to see ids)")
        return
    try:
        content_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a valid id.")
        return

    if delete_content_item(content_id):
        await update.message.reply_text(
            f"Deleted content #{content_id}. Anyone who already received it keeps their copy — "
            "this only stops it from being sent going forward."
        )
    else:
        await update.message.reply_text(f"No content found with id {content_id}.")


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Add it to your .env file (see .env.example).")

    init_db()
    migrate_db()
    seed_content_from_json()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("listcontent", listcontent))
    app.add_handler(CommandHandler("deletecontent", deletecontent))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addcontent", addcontent_start)],
        states={
            ASK_DAY: [CommandHandler("cancel", cancel), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_day)],
            ASK_DELAY: [
                CommandHandler("cancel", cancel),
                CallbackQueryHandler(ask_delay),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_delay_custom),
            ],
            ASK_TYPE: [CommandHandler("cancel", cancel), CallbackQueryHandler(ask_type)],
            ASK_CONTENT: [CommandHandler("cancel", cancel), MessageHandler(filters.ALL & ~filters.COMMAND, ask_content)],
            ASK_CAPTION: [
                CommandHandler("cancel", cancel),
                CommandHandler("skip", skip_caption),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_caption),
            ],
            CONFIRM: [CommandHandler("cancel", cancel), CallbackQueryHandler(confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)

    # Catches any other message from subscribers (this bot doesn't hold
    # conversations) and redirects them to a real DM. Registered in group=1
    # so it runs after commands/the /addcontent conversation; is_admin()
    # inside the handler also excludes the admin explicitly either way.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, redirect_message), group=1)

    # Check for due content on a timer (default every 5 min) — catches
    # anyone whose next piece became due since they last interacted with
    # the bot, including within-day delayed items.
    app.job_queue.run_repeating(check_and_send, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()

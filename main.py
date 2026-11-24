import asyncio
import base64
import functools
import html
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)


def _env(name, default=""):
    return os.environ.get(name, default).strip()


BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_ID = _env("ADMIN_ID", "7574854549")
ADMIN_ID_2 = _env("ADMIN_ID_2", "8274424703")
SUPPORT_ID = _env("SUPPORT_ID", "@Shafajs")
SUPPORT_CHANNEL = _env("SUPPORT_CHANNEL", "https://t.me/smgmailsell")
FORCE_CHANNEL = _env("FORCE_CHANNEL", "@smgmailsell")
FORCE_CHANNEL_LINK = _env("FORCE_CHANNEL_LINK", "https://t.me/smgmailsell")
WITHDRAW_CHANNEL = _env("WITHDRAW_CHANNEL", "@wdgmailsell")

GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_REPO = _env("GITHUB_REPO")
GITHUB_BRANCH = _env("GITHUB_BRANCH", "main")
BACKUP_PATH = _env("BACKUP_PATH", "backup.json")
BACKUP_INTERVAL = int(_env("BACKUP_INTERVAL", "60") or 60)

PORT = int(_env("PORT", "10000") or 10000)
SELF_URL = _env("RENDER_EXTERNAL_URL")


def _is_placeholder(value):
    return "এখানে" in str(value) or not str(value).strip()


def _make_link(value):
    value = str(value).strip()
    if _is_placeholder(value):
        return ""
    if value.startswith("@"):
        return "https://t.me/" + value[1:]
    if value.startswith("t.me/"):
        return "https://" + value
    return value


ADMIN = int(ADMIN_ID) if ADMIN_ID.isdigit() else 0
ADMIN_2 = int(ADMIN_ID_2) if ADMIN_ID_2.isdigit() else 0
ADMINS = [a for a in dict.fromkeys((ADMIN, ADMIN_2)) if a]
SUPPORT_USER = (
    ""
    if _is_placeholder(SUPPORT_ID)
    else str(SUPPORT_ID).strip().split("t.me/")[-1].lstrip("@").rstrip("/")
)
GROUP_LINK = _make_link(SUPPORT_CHANNEL)


CONFIG_FILE = "config.json"
USERS_FILE = "users.json"
GMAIL_FILE = "gmail_tasks.json"
FACEBOOK_FILE = "facebook_tasks.json"
INSTAGRAM_FILE = "instagram_tasks.json"
USER_STATS_FILE = "user_stats.json"
FB_ACCOUNT_FILE = "fb_active_account.json"

DATA_FILES = [
    CONFIG_FILE,
    USERS_FILE,
    GMAIL_FILE,
    FACEBOOK_FILE,
    INSTAGRAM_FILE,
    USER_STATS_FILE,
    FB_ACCOUNT_FILE,
]

(
    PROOF_SUBMIT,
    WITHDRAW_NUM,
    WITHDRAW_AMT,
    FB_NUMBER,
    ADMIN_FB_FIRST,
    ADMIN_FB_LAST,
    ADMIN_FB_PASS,
    USER_FB_UID,
    USER_FB_COOKIES,
) = range(9)

_file_lock = threading.RLock()
_dirty = threading.Event()
_last_backup_ok = {"time": 0.0, "error": ""}


def _gh_request(method, path, payload=None, raw=False):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    if method == "GET":
        url += f"?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "telegram-bot-backup",
        "Accept": "application/vnd.github.raw+json"
        if raw
        else "application/vnd.github+json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return body if raw else json.loads(body.decode())


def github_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def build_backup():
    files = {}
    with _file_lock:
        for name in DATA_FILES:
            if os.path.exists(name):
                try:
                    with open(name, "r", encoding="utf-8") as f:
                        files[name] = json.load(f)
                except Exception:
                    pass
    return {"saved_at": int(time.time()), "files": files}


def github_backup():
    if not github_enabled():
        return False
    try:
        content = json.dumps(build_backup(), ensure_ascii=False, indent=1).encode(
            "utf-8"
        )
        sha = None
        try:
            meta = _gh_request("GET", BACKUP_PATH)
            sha = meta.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        payload = {
            "message": "auto backup " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "content": base64.b64encode(content).decode(),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        _gh_request("PUT", BACKUP_PATH, payload)
        _last_backup_ok.update(time=time.time(), error="")
        return True
    except Exception as e:
        _last_backup_ok["error"] = str(e)
        logging.error("GitHub backup failed: %s", e)
        return False


def github_fetch_backup():
    if not github_enabled():
        return None
    try:
        raw = _gh_request("GET", BACKUP_PATH, raw=True)
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logging.info("GitHub এ এখনো কোনো ব্যাকআপ নেই।")
        else:
            logging.error("GitHub restore failed: %s", e)
    except Exception as e:
        logging.error("GitHub restore failed: %s", e)
    return None


def write_backup_to_disk(backup):
    files = (backup or {}).get("files", {})
    count = 0
    with _file_lock:
        for name in DATA_FILES:
            if name in files:
                tmp = name + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(files[name], f, ensure_ascii=False, indent=2)
                os.replace(tmp, name)
                count += 1
    return count


def startup_restore():
    have_local = os.path.exists(USER_STATS_FILE) or os.path.exists(USERS_FILE)
    if have_local:
        return
    backup = github_fetch_backup()
    if backup:
        n = write_backup_to_disk(backup)
        logging.info("GitHub থেকে %s টি ফাইল রিস্টোর হয়েছে।", n)


def backup_worker():
    while True:
        time.sleep(BACKUP_INTERVAL)
        if _dirty.is_set():
            _dirty.clear()
            if not github_backup():
                _dirty.set()


class HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(b"Bot is running")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, *args):
        pass


def run_web_server():
    try:
        HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()
    except Exception as e:
        logging.error("Web server error: %s", e)


def keep_alive_worker():
    if not SELF_URL:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(SELF_URL, timeout=20).read()
        except Exception as e:
            logging.warning("Keep-alive ping failed: %s", e)


def start_background_threads():
    for target in (run_web_server, backup_worker, keep_alive_worker):
        threading.Thread(target=target, daemon=True).start()


startup_restore()


def load_json(filename, default):
    with _file_lock:
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default
    return default


def save_json(filename, data):
    try:
        with _file_lock:
            tmp = filename + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, filename)
        _dirty.set()
    except Exception as e:
        logging.error("Save failed for %s: %s", filename, e)


DEFAULT_CONFIG = {
    "gmail_price": 14,
    "fb_price": 3,
    "ig_price": 2,
    "min_withdraw": 25,
    "min_binance": 30,
    "ref_bonus": 2,
    "ref_mode": "manual",
    "emoji_ids": {},
}
cfg = load_json(CONFIG_FILE, {})
for _k, _v in DEFAULT_CONFIG.items():
    cfg.setdefault(_k, _v)


def save_config():
    save_json(CONFIG_FILE, cfg)


gmail_tasks = load_json(GMAIL_FILE, [])
facebook_tasks = load_json(FACEBOOK_FILE, [])
instagram_tasks = load_json(INSTAGRAM_FILE, [])
user_data_db = load_json(USER_STATS_FILE, {})
fb_account_db = load_json(FB_ACCOUNT_FILE, {})


def reload_memory_from_disk():
    new_cfg = load_json(CONFIG_FILE, {})
    for _k, _v in DEFAULT_CONFIG.items():
        new_cfg.setdefault(_k, _v)
    cfg.clear()
    cfg.update(new_cfg)
    gmail_tasks[:] = load_json(GMAIL_FILE, [])
    facebook_tasks[:] = load_json(FACEBOOK_FILE, [])
    instagram_tasks[:] = load_json(INSTAGRAM_FILE, [])
    user_data_db.clear()
    user_data_db.update(load_json(USER_STATS_FILE, {}))
    fb_account_db.clear()
    fb_account_db.update(load_json(FB_ACCOUNT_FILE, {}))


def load_registered_users():
    return set(int(u) for u in load_json(USERS_FILE, []))


def save_user_id(user_id):
    users = load_registered_users()
    if int(user_id) not in users:
        users.add(int(user_id))
        save_json(USERS_FILE, sorted(users))


def get_user_stats(user_id):
    key = str(user_id)
    user_data_db.setdefault(key, {})
    defaults = {
        "completed": 0,
        "pending": 0,
        "balance": 0.0,
        "referred_by": None,
        "ref_count": 0,
        "ref_earned": 0.0,
        "ref_paid": False,
    }
    for k, v in defaults.items():
        user_data_db[key].setdefault(k, v)
    return user_data_db[key]


def save_user_stats():
    save_json(USER_STATS_FILE, user_data_db)


def load_active_fb_account():
    data = load_json(FB_ACCOUNT_FILE, {})
    if isinstance(data, dict) and data.get("first_name") and data.get("password"):
        return data
    return None


def save_active_fb_account(data):
    save_json(FB_ACCOUNT_FILE, data)
    fb_account_db.clear()
    fb_account_db.update(data)


def get_fb_price():
    return cfg.get("fb_price", 3)


_fb_user_temp = {}


BTN = {
    "tasks": ("🛠️ কাজ", "danger"),
    "balance": ("💰 আমার ব্যালেন্স", "primary"),
    "withdraw": ("💳 টাকা তুলুন", "success"),
    "support": ("📞 সাপোর্ট", "primary"),
    "howto": ("🎥 কিভাবে কাজ করবো", "primary"),
    "gmail": ("📧 জিমেইল কাজ", "danger"),
    "fb": ("📘 ফেসবুক কাজ", "primary"),
    "ig": ("📷 ইনস্টাগ্রাম কাজ", "success"),
    "cancel": ("❌ বাতিল করুন", "danger"),
    "submit": ("📥 জমা দিন", "success"),
    "cancel_task": ("❌ কাজটি বাতিল করুন", "danger"),
    "admin": ("📊 অ্যাডমিন প্যানেল", "primary"),
    "refer": ("🎁 রেফার করুন", "success"),
}
TEXT_TO_KEY = {text: key for key, (text, _) in BTN.items()}


def kbtn(key):
    text, style = BTN[key]
    extra = {"style": style}
    emoji_id = cfg["emoji_ids"].get(key)
    if emoji_id:
        extra["icon_custom_emoji_id"] = emoji_id
    return KeyboardButton(text, api_kwargs=extra)


def ibtn(text, callback_data=None, url=None, style="primary"):
    return InlineKeyboardButton(
        text, callback_data=callback_data, url=url, api_kwargs={"style": style}
    )


def main_keyboard(update=None):
    rows = [
        [kbtn("tasks"), kbtn("balance")],
        [kbtn("withdraw"), kbtn("support")],
        [kbtn("howto"), kbtn("refer")],
    ]
    if update is not None and update.effective_user and update.effective_user.id in ADMINS:
        rows.append([kbtn("admin")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def task_menu_keyboard():
    rows = [[kbtn("gmail"), kbtn("fb")], [kbtn("ig")], [kbtn("cancel")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def task_keyboard():
    return ReplyKeyboardMarkup(
        [[kbtn("submit"), kbtn("cancel_task")]], resize_keyboard=True
    )


MENU_REGEX = "^(" + "|".join(re.escape(t) for t in TEXT_TO_KEY) + ")$"
MENU_FILTER = filters.Regex(MENU_REGEX)
TEXT_INPUT = filters.TEXT & ~filters.COMMAND & ~MENU_FILTER


def esc(value):
    return html.escape(str(value))


def arg_text(update):
    parts = (update.message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            return
        return await func(update, context)

    return wrapper


def clear_task_state(context):
    for k in ("current_task_text", "last_task_msg_id", "current_task_type"):
        context.user_data.pop(k, None)


async def notify_admin(context, text, photo=None, reply_markup=None):
    for admin_id in ADMINS:
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo,
                    caption=text[:1000],
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
        except Exception as e:
            logging.error("Admin notify failed (%s): %s", admin_id, e)


async def safe_send(context, chat_id, text, **kwargs):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception:
        return False


async def post_to_channel(context, text, photo=None):
    try:
        if photo:
            await context.bot.send_photo(
                chat_id=WITHDRAW_CHANNEL,
                photo=photo,
                caption=text[:1000],
                parse_mode="HTML",
            )
        else:
            await context.bot.send_message(
                chat_id=WITHDRAW_CHANNEL, text=text, parse_mode="HTML"
            )
    except Exception as e:
        logging.error("Withdraw channel post failed: %s", e)


async def is_member(bot, user_id):
    if user_id in ADMINS:
        return True
    try:
        m = await bot.get_chat_member(FORCE_CHANNEL, user_id)
    except Exception as e:
        logging.error("Join check failed: %s", e)
        return False
    if m.status in ("creator", "administrator", "member"):
        return True
    return m.status == "restricted" and bool(getattr(m, "is_member", False))


async def ask_join(message):
    kb = InlineKeyboardMarkup(
        [
            [ibtn("📢 চ্যানেলে জয়েন করুন", url=FORCE_CHANNEL_LINK, style="primary")],
            [ibtn("✅ Done", "check_join", style="success")],
        ]
    )
    await message.reply_text(
        "⚠️ বটের মেনু ব্যবহার করতে আগে আমাদের চ্যানেলে জয়েন করুন।\n"
        "জয়েন করার পর নিচের ✅ Done বাটনে চাপ দিন।",
        reply_markup=kb,
    )


GM_DONE = set()
GM_OK_TEXT = "✅ আপনার তৈরি এই জিমেইল একাউন্টটি সফলভাবে সাপোর্ট করা হয়েছে।"
GM_NO_TEXT = "❌ আপনার জিমেইল একাউন্টটি রিজেক্ট করা হয়েছে।"def build_stats_text():
    members = load_registered_users() | set(
        int(k) for k in user_data_db if str(k).isdigit()
    )
    g, f, i = len(gmail_tasks), len(facebook_tasks), len(instagram_tasks)
    total_pending = sum(v.get("pending", 0) for v in user_data_db.values())
    total_done = sum(v.get("completed", 0) for v in user_data_db.values())
    total_balance = sum(v.get("balance", 0) for v in user_data_db.values())
    if not github_enabled():
        backup_line = "⚠️ GitHub ব্যাকআপ চালু নেই"
    elif _last_backup_ok["error"]:
        backup_line = "❌ ব্যাকআপ ব্যর্থ: " + esc(_last_backup_ok["error"][:80])
    elif _last_backup_ok["time"]:
        backup_line = "✅ শেষ ব্যাকআপ: " + time.strftime(
            "%d-%m %H:%M", time.localtime(_last_backup_ok["time"])
        )
    else:
        backup_line = "⏳ ব্যাকআপ এখনো হয়নি"
    return (
        "📊 <b>বট পরিসংখ্যান</b>\n\n"
        f"👥 মোট মেম্বার: <b>{len(members)}</b> জন\n\n"
        "🛠️ <b>বটে থাকা কাজ</b>\n"
        f"📧 জিমেইল: {g} টি\n"
        f"📘 ফেসবুক: {f} টি\n"
        f"📷 ইনস্টাগ্রাম: {i} টি\n"
        f"📦 মোট কাজ: <b>{g + f + i}</b> টি\n\n"
        f"✅ মোট সম্পন্ন: {total_done}\n"
        f"⏳ মোট পেন্ডিং: {total_pending}\n"
        f"💰 ইউজারদের মোট ব্যালেন্স: {round(total_balance, 2)} টাকা\n\n"
        f"💾 {backup_line}"
    )


def stats_markup():
    return InlineKeyboardMarkup(
        [[ibtn("🔄 রিফ্রেশ", "admin_stats", style="primary")]]
    )


@admin_only
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_stats_text(), parse_mode="HTML", reply_markup=stats_markup()
    )


@admin_only
async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    content = json.dumps(build_backup(), ensure_ascii=False, indent=1).encode("utf-8")
    note = ""
    if github_enabled():
        ok = await asyncio.to_thread(github_backup)
        note = (
            "✅ GitHub এ ব্যাকআপ হয়েছে।"
            if ok
            else "❌ GitHub ব্যাকআপ ব্যর্থ হয়েছে (লগ দেখুন)।"
        )
    else:
        note = "⚠️ GitHub ব্যাকআপ চালু নেই, শুধু ফাইল পাঠানো হলো।"
    buf = io.BytesIO(content)
    buf.name = "backup_" + time.strftime("%Y%m%d_%H%M") + ".json"
    await msg.reply_document(
        buf,
        caption=note + "\nরিস্টোর করতে এই ফাইল পাঠিয়ে ক্যাপশনে /restore লিখুন।",
    )


@admin_only
async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (update.message.caption or "").strip().lower()
    if not caption.startswith("/restore"):
        return
    try:
        tg_file = await update.message.document.get_file()
        raw = await tg_file.download_as_bytearray()
        backup = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(backup, dict) or "files" not in backup:
            raise ValueError("bad format")
    except Exception:
        await update.message.reply_text("❌ ফাইলটি সঠিক ব্যাকআপ ফাইল নয়।")
        return
    n = write_backup_to_disk(backup)
    reload_memory_from_disk()
    _dirty.set()
    await update.message.reply_text(
        f"✅ {n} টি ফাইল রিস্টোর হয়েছে।\n\n" + build_stats_text(),
        parse_mode="HTML",
    )


def norm_link(value):
    value = (value or "").strip()
    if re.match(r"^https?://\S+$", value, re.I):
        return value
    if re.match(r"^(t\.me|telegram\.me)/\S+$", value, re.I):
        return "https://" + value
    if re.match(r"^@\w{4,}$", value):
        return "https://t.me/" + value[1:]
    return None


def parse_text_link(raw):
    raw = (raw or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) >= 2:
        link = norm_link(parts[-1])
        if link:
            if len(parts) >= 3 and parts[-2]:
                return "|".join(parts[:-2]).strip(), parts[-2][:60], link
            return "|".join(parts[:-1]).strip(), "🔗 লিংক খুলুন", link
    return raw, None, None


def build_markup(label=None, link=None, reply_btn=False):
    rows = []
    if link:
        rows.append([ibtn(label or "🔗 লিংক খুলুন", url=link, style="primary")])
    if reply_btn:
        rows.append([ibtn("↩️ উত্তর দিন", "contact_admin", style="success")])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_one(context, chat_id, text, markup=None, photo=None, protect=False):
    try:
        if photo:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text[:1000],
                reply_markup=markup,
                protect_content=protect,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                protect_content=protect,
            )
        return True
    except Exception as e:
        logging.info("send failed (%s): %s", chat_id, e)
        return False


async def _broadcast_job(context, report_to, text, markup, photo):
    users = sorted(load_registered_users())
    ok = fail = 0
    for uid in users:
        if await send_one(context, uid, text, markup, photo):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await safe_send(
        context, report_to, f"📢 নোটিশ পাঠানো শেষ।\n✅ সফল: {ok}\n❌ ব্যর্থ: {fail}"
    )


async def do_notice(update, context, raw, photo=None):
    text, label, link = parse_text_link(raw)
    if not text and not photo:
        await update.message.reply_text(
            "ব্যবহার:\n/notice আপনার বার্তা\n/notice বার্তা | https://লিংক\n"
            "/notice বার্তা | বাটনের নাম | https://লিংক\n\n"
            "ছবিসহ পাঠাতে ছবির ক্যাপশনে /notice লিখুন।"
        )
        return
    body = "📢 নোটিশ\n\n" + text if text else "📢 নোটিশ"
    markup = build_markup(label, link)
    count = len(load_registered_users())
    await update.message.reply_text(f"📤 {count} জনকে পাঠানো শুরু হচ্ছে...")
    context.application.create_task(
        _broadcast_job(context, update.effective_user.id, body, markup, photo)
    )


@admin_only
async def notice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_notice(update, context, arg_text(update))


async def do_msg(update, context, raw, photo=None, oneway=True):
    cmd = "/dm" if oneway else "/msg"
    parts = (raw or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit() or (len(parts) < 2 and not photo):
        await update.message.reply_text(
            f"ব্যবহার:\n{cmd} [User_ID] বার্তা\n{cmd} [User_ID] বার্তা | https://লিংক\n"
            f"ছবিসহ পাঠাতে ছবির ক্যাপশনে {cmd} [User_ID] বার্তা লিখুন।"
        )
        return
    uid = int(parts[0])
    text, label, link = parse_text_link(parts[1] if len(parts) > 1 else "")
    body = "💬 এডমিনের মেসেজ:\n\n" + text if text else "💬 এডমিনের মেসেজ"
    ok = await send_one(
        context,
        uid,
        body,
        build_markup(label, link, reply_btn=not oneway),
        photo,
        protect=oneway,
    )
    if ok and oneway:
        try:
            context.application.user_data[uid].pop("contact_mode", None)
        except Exception:
            pass
    await update.message.reply_text(
        f"✅ {uid}-কে মেসেজ পাঠানো হয়েছে।"
        if ok
        else f"❌ {uid}-কে পাঠানো যায়নি (ইউজার বট ব্লক করেছে বা আইডি ভুল)।"
    )


@admin_only
async def dm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_msg(update, context, arg_text(update), oneway=True)


@admin_only
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    src = update.message.reply_to_message
    blob = (src.text or src.caption or "") if src else ""
    m = re.search(r"🆔 (\d+)", blob)
    if not m:
        await handle_text_messages(update, context)
        return
    uid = int(m.group(1))
    text, label, link = parse_text_link(update.message.text or "")
    ok = await send_one(
        context,
        uid,
        "💬 এডমিনের মেসেজ:\n\n" + text,
        build_markup(label, link, reply_btn=True),
    )
    await update.message.reply_text("✅ উত্তর পাঠানো হয়েছে।" if ok else "❌ পাঠানো যায়নি।")


async def forward_to_admins(update, context, text=None, photo=None):
    user = update.effective_user
    uname = f" (@{user.username})" if user.username else ""
    head = (
        "💬 <b>ইউজারের মেসেজ</b>\n"
        f"👤 {esc(user.first_name)}{esc(uname)}\n"
        f"🆔 <code>{user.id}</code>\n\n"
    )
    await notify_admin(context, head + esc(text or ""), photo=photo)
    context.user_data.pop("contact_mode", None)
    await update.message.reply_text(
        "✅ আপনার মেসেজ এডমিনের কাছে পাঠানো হয়েছে। উত্তর পেলে এখানেই জানানো হবে।",
        reply_markup=main_keyboard(update),
    )


async def user_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMINS or not context.user_data.get("contact_mode"):
        return
    await forward_to_admins(
        update, context, update.message.caption, update.message.photo[-1].file_id
    )


def referral_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def give_referral_bonus(context, referred_id, amount=None):
    st = get_user_stats(referred_id)
    referrer = st.get("referred_by")
    if not referrer or st.get("ref_paid"):
        return None
    amt = amount if amount is not None else cfg["ref_bonus"]
    rst = get_user_stats(referrer)
    rst["balance"] += amt
    rst["ref_earned"] = rst.get("ref_earned", 0.0) + amt
    st["ref_paid"] = True
    save_user_stats()
    await safe_send(
        context,
        referrer,
        f"🎁 রেফারেল বোনাস! আপনার রেফার করা একজনের কাজ অনুমোদিত হয়েছে, আপনি {amt} টাকা পেয়েছেন।",
    )
    return referrer, amt


async def referral_after_approval(context, referred_id):
    st = get_user_stats(referred_id)
    referrer = st.get("referred_by")
    if not referrer or st.get("ref_paid") or st.get("completed", 0) != 1:
        return
    bonus = cfg["ref_bonus"]
    if cfg.get("ref_mode") == "auto":
        res = await give_referral_bonus(context, referred_id)
        if res:
            await notify_admin(
                context,
                f"🎁 রেফারেল বোনাস অটো দেওয়া হয়েছে\nরেফারার: <code>{res[0]}</code>\n"
                f"রেফার্ড: <code>{referred_id}</code>\nবোনাস: {res[1]} টাকা",
            )
        return
    kb = InlineKeyboardMarkup(
        [
            [
                ibtn(
                    f"🎁 বোনাস দিন ({bonus} টাকা)",
                    f"refpay:{referred_id}",
                    style="success",
                )
            ],
            [ibtn("🚫 দেবেন না", f"refno:{referred_id}", style="danger")],
        ]
    )
    await notify_admin(
        context,
        "🎁 <b>রেফারেল বোনাস অনুমোদন</b>\n"
        f"রেফারার: <code>{referrer}</code>\n"
        f"রেফার্ড: <code>{referred_id}</code> (প্রথম কাজ অনুমোদিত)\n"
        f"বোনাস: {bonus} টাকা",
        reply_markup=kb,
    )


@admin_only
async def set_ref_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(context.args[0])
        if amt < 0:
            raise ValueError
    except Exception:
        await update.message.reply_text(
            f"ব্যবহার: /setrefbonus [টাকা]\nবর্তমান বোনাস: {cfg['ref_bonus']} টাকা"
        )
        return
    cfg["ref_bonus"] = int(amt) if amt == int(amt) else amt
    save_config()
    await update.message.reply_text(f"✅ রেফারেল বোনাস এখন {cfg['ref_bonus']} টাকা।")


@admin_only
async def set_ref_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.args[0].lower() if context.args else ""
    if mode not in ("auto", "manual"):
        await update.message.reply_text(
            "ব্যবহার: /refmode auto  অথবা  /refmode manual\n\n"
            f"বর্তমান: {cfg.get('ref_mode', 'manual')}"
        )
        return
    cfg["ref_mode"] = mode
    save_config()
    await update.message.reply_text(f"✅ রেফারেল মোড: {mode}")


@admin_only
async def ref_bonus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tid = int(context.args[0])
        amt = float(context.args[1]) if len(context.args) > 1 else float(cfg["ref_bonus"])
    except Exception:
        await update.message.reply_text("ব্যবহার: /refbonus [User_ID] [টাকা]")
        return
    st = get_user_stats(tid)
    st["balance"] += amt
    st["ref_earned"] = st.get("ref

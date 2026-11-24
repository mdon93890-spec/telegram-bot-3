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

# ╔══════════════════════════════════════════════════════════════╗
# ║  সেটিংস এখন Render এর Environment Variables থেকে আসবে।        ║
# ║  কোডে টোকেন লিখে GitHub এ আপলোড করবেন না!                     ║
# ╚══════════════════════════════════════════════════════════════╝
#
# Render → Environment এ এই ভ্যারিয়েবলগুলো দিন:
#   BOT_TOKEN       = BotFather থেকে পাওয়া টোকেন
#   ADMIN_ID        = প্রথম এডমিনের আইডি
#   ADMIN_ID_2      = দ্বিতীয় এডমিনের আইডি
#   GITHUB_TOKEN    = ডাটা ব্যাকআপের জন্য GitHub টোকেন (repo/contents write)
#   GITHUB_REPO     = আপনার প্রাইভেট ব্যাকআপ রিপো, যেমন: myname/bot-data
#   GITHUB_BRANCH   = (ঐচ্ছিক) ডিফল্ট main
# বাকিগুলো (ঐচ্ছিক, না দিলে নিচের ডিফল্ট):
#   SUPPORT_ID, SUPPORT_CHANNEL, FORCE_CHANNEL, FORCE_CHANNEL_LINK, WITHDRAW_CHANNEL


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
BACKUP_INTERVAL = int(_env("BACKUP_INTERVAL", "60") or 60)  # সেকেন্ড

PORT = int(_env("PORT", "10000") or 10000)
SELF_URL = _env("RENDER_EXTERNAL_URL")  # Render নিজে থেকেই এটা সেট করে


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
# দুইজন এডমিনই সমান ক্ষমতা পাবে (০ বা ফাঁকা আইডি বাদ)
ADMINS = [a for a in dict.fromkeys((ADMIN, ADMIN_2)) if a]
SUPPORT_USER = (
    ""
    if _is_placeholder(SUPPORT_ID)
    else str(SUPPORT_ID).strip().split("t.me/")[-1].lstrip("@").rstrip("/")
)
GROUP_LINK = _make_link(SUPPORT_CHANNEL)


# ============ ফাইল ============
CONFIG_FILE = "config.json"
USERS_FILE = "users.json"
GMAIL_FILE = "gmail_tasks.json"
FB_ACCOUNTS_FILE = "fb_accounts.json"
INSTAGRAM_FILE = "instagram_tasks.json"
USER_STATS_FILE = "user_stats.json"

# ব্যাকআপে যেসব ফাইল যাবে
DATA_FILES = [
    CONFIG_FILE,
    USERS_FILE,
    GMAIL_FILE,
    FB_ACCOUNTS_FILE,
    INSTAGRAM_FILE,
    USER_STATS_FILE,
]

PROOF_SUBMIT, WITHDRAW_NUM, WITHDRAW_AMT, FB_UID, FB_COOKIES = range(5)

_file_lock = threading.RLock()
_dirty = threading.Event()
_last_backup_ok = {"time": 0.0, "error": ""}


# ============ GitHub ব্যাকআপ (MongoDB ছাড়া, বট/টেলিগ্রাম আইডি হারালেও নিরাপদ) ============
def _gh_request(method, path, payload=None, raw=False):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    if method == "GET":
        url += f"?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "telegram-bot-backup",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return body if raw else json.loads(body.decode())


def github_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def build_backup():
    """সব ডাটা ফাইল এক জায়গায় করে ব্যাকআপ অবজেক্ট বানায়।"""
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
        content = json.dumps(build_backup(), ensure_ascii=False, indent=1).encode("utf-8")
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
    """Render এ রিডিপ্লয়ের পর লোকাল ফাইল মুছে যায় — তখন GitHub থেকে ডাটা ফেরত আনে।
    নতুন বট টোকেন বসালেও একই GITHUB_REPO দিলে সব ইউজারের ডাটা ফিরে আসবে।"""
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
                _dirty.set()  # ব্যর্থ হলে পরে আবার চেষ্টা


# ============ ওয়েব সার্ভার (Render Web Service এর জন্য) ============
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
    """নিজের URL এ নিয়মিত পিং করে, যাতে Render ঘুমিয়ে না পড়ে।"""
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


# ============ ডাটা লোড / সেভ ============
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
    """নিরাপদ সেভ: আগে tmp ফাইলে লিখে তারপর রিপ্লেস — মাঝপথে বন্ধ হলেও ফাইল নষ্ট হয় না।"""
    try:
        with _file_lock:
            tmp = filename + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, filename)
        _dirty.set()
    except Exception as e:
        logging.error("Save failed for %s: %s", filename, e)


# ============ কনফিগ (সবকিছু অ্যাডমিন কমান্ড দিয়ে বদলানো যায়) ============
DEFAULT_CONFIG = {
    "gmail_price": 14,
    "fb_price": 3,
    "ig_price": 2,
    "min_withdraw": 25,
    "min_binance": 30,
    "ref_bonus": 2,
    "ref_mode": "manual",  # manual = এডমিন বাটন চেপে দেবে, auto = নিজে থেকে যাবে
    "emoji_ids": {},
}
cfg = load_json(CONFIG_FILE, {})
for _k, _v in DEFAULT_CONFIG.items():
    cfg.setdefault(_k, _v)


def save_config():
    save_json(CONFIG_FILE, cfg)


gmail_tasks = load_json(GMAIL_FILE, [])
fb_accounts = load_json(FB_ACCOUNTS_FILE, [])
instagram_tasks = load_json(INSTAGRAM_FILE, [])
user_data_db = load_json(USER_STATS_FILE, {})


def reload_memory_from_disk():
    """/restore এর পর মেমোরির ডাটা আপডেট করে (একই অবজেক্টে)।"""
    new_cfg = load_json(CONFIG_FILE, {})
    for _k, _v in DEFAULT_CONFIG.items():
        new_cfg.setdefault(_k, _v)
    cfg.clear()
    cfg.update(new_cfg)
    gmail_tasks[:] = load_json(GMAIL_FILE, [])
    fb_accounts[:] = load_json(FB_ACCOUNTS_FILE, [])
    instagram_tasks[:] = load_json(INSTAGRAM_FILE, [])
    user_data_db.clear()
    user_data_db.update(load_json(USER_STATS_FILE, {}))


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


# ============ প্রিমিয়াম (রঙিন) বাটন ============
# style: primary = নীল, success = সবুজ, danger = লাল
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
    # শুধু এডমিনরা এই বাটন দেখবে
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


# ============ হেল্পার ============
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
    """উইথড্র চ্যানেলে পোস্ট পাঠায় (ব্যর্থ হলে শুধু লগ করে)।"""
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


# ============ চ্যানেল জয়েন বাধ্যতামূলক ============
async def is_member(bot, user_id):
    if user_id in ADMINS:
        return True
    try:
        m = await bot.get_chat_member(FORCE_CHANNEL, user_id)
    except Exception as e:
        logging.error("Join check failed (বট চ্যানেলের এডমিন কিনা দেখুন): %s", e)
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


# জিমেইল রেজাল্ট একবারের বেশি পাঠানো ঠেকাতে
GM_DONE = set()
GM_OK_TEXT = "✅ আপনার তৈরি এই জিমেইল একাউন্টটি সফলভাবে সাপোর্ট করা হয়েছে।"
GM_NO_TEXT = "❌ আপনার জিমেইল একাউন্টটি রিজেক্ট করা হয়েছে।"


# ============ অ্যাডমিন: পরিসংখ্যান ============
def build_stats_text():
    members = load_registered_users() | set(int(k) for k in user_data_db if str(k).isdigit())
    g, f, i = len(gmail_tasks), len(fb_accounts), len(instagram_tasks)
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
        f"📘 ফেসবুক (স্টকে অ্যাকাউন্ট): {f} টি\n"
        f"📷 ইনস্টাগ্রাম: {i} টি\n"
        f"📦 মোট কাজ: <b>{g + f + i}</b> টি\n\n"
        f"✅ মোট সম্পন্ন: {total_done}\n"
        f"⏳ মোট পেন্ডিং: {total_pending}\n"
        f"💰 ইউজারদের মোট ব্যালেন্স: {round(total_balance, 2)} টাকা\n\n"
        f"💾 {backup_line}"
    )


def stats_markup():
    return InlineKeyboardMarkup([[ibtn("🔄 রিফ্রেশ", "admin_stats", style="primary")]])


@admin_only
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_stats_text(), parse_mode="HTML", reply_markup=stats_markup()
    )


# ============ অ্যাডমিন: ব্যাকআপ / রিস্টোর / ব্রডকাস্ট ============
@admin_only
async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backup — GitHub এ এখনই ব্যাকআপ নেয় এবং ব্যাকআপ ফাইলটি আপনাকে পাঠায়।"""
    msg = update.message
    content = json.dumps(build_backup(), ensure_ascii=False, indent=1).encode("utf-8")
    note = ""
    if github_enabled():
        ok = await asyncio.to_thread(github_backup)
        note = "✅ GitHub এ ব্যাকআপ হয়েছে।" if ok else "❌ GitHub ব্যাকআপ ব্যর্থ হয়েছে (লগ দেখুন)।"
    else:
        note = "⚠️ GitHub ব্যাকআপ চালু নেই, শুধু ফাইল পাঠানো হলো।"
    buf = io.BytesIO(content)
    buf.name = "backup_" + time.strftime("%Y%m%d_%H%M") + ".json"
    await msg.reply_document(
        buf, caption=note + "\nরিস্টোর করতে এই ফাইল পাঠিয়ে ক্যাপশনে /restore লিখুন।"
    )


@admin_only
async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ব্যাকআপ ফাইল পাঠিয়ে ক্যাপশনে /restore লিখলে সব ডাটা ফিরে আসে।"""
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
        f"✅ {n} টি ফাইল রিস্টোর হয়েছে।\n\n" + build_stats_text(), parse_mode="HTML"
    )


# ============ লিংক / মেসেজ হেল্পার ============
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
    """'বার্তা | লিংক' বা 'বার্তা | বাটনের নাম | লিংক' → (text, label, link)"""
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
                chat_id=chat_id, photo=photo, caption=text[:1000], reply_markup=markup,
                protect_content=protect,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup, protect_content=protect
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
        await asyncio.sleep(0.05)  # টেলিগ্রামের সীমা (সেকেন্ডে ~২০ মেসেজ) মানতে
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
    # ব্যাকগ্রাউন্ডে চলবে, যাতে বট অন্য কাজ করতে পারে
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
            f"{cmd} [User_ID] বার্তা | বাটনের নাম | https://লিংক\n\n"
            f"ছবিসহ পাঠাতে ছবির ক্যাপশনে {cmd} [User_ID] বার্তা লিখুন।"
        )
        return
    uid = int(parts[0])
    text, label, link = parse_text_link(parts[1] if len(parts) > 1 else "")
    body = "💬 এডমিনের মেসেজ:\n\n" + text if text else "💬 এডমিনের মেসেজ"
    # oneway = শুধু ওই ইউজারই দেখবে, "উত্তর দিন" বাটন নেই, ফরওয়ার্ড/সেভ বন্ধ
    ok = await send_one(
        context, uid, body, build_markup(label, link, reply_btn=not oneway), photo,
        protect=oneway,
    )
    if ok and oneway:
        # আগে থেকে কনট্যাক্ট মোড চালু থাকলেও বন্ধ করে দেয়, যাতে রিপ্লাই আপনার কাছে না আসে
        try:
            context.application.user_data[uid].pop("contact_mode", None)
        except Exception:
            pass
    await update.message.reply_text(
        f"✅ {uid}-কে মেসেজ পাঠানো হয়েছে।" if ok
        else f"❌ {uid}-কে পাঠানো যায়নি (ইউজার বট ব্লক করেছে বা আইডি ভুল)।"
    )


@admin_only
async def dm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dm [User_ID] বার্তা — একতরফা মেসেজ: শুধু ওই ইউজার দেখবে, সে উত্তর দিতে পারবে না।"""
    await do_msg(update, context, arg_text(update), oneway=True)


@admin_only
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ইউজারের পাঠানো মেসেজে এডমিন Reply করলে উত্তর সরাসরি ওই ইউজারের কাছে যায়।"""
    src = update.message.reply_to_message
    blob = (src.text or src.caption or "") if src else ""
    m = re.search(r"🆔 (\d+)", blob)
    if not m:
        await handle_text_messages(update, context)  # সাধারণ মেসেজ হিসেবে চলবে
        return
    uid = int(m.group(1))
    text, label, link = parse_text_link(update.message.text or "")
    ok = await send_one(
        context, uid, "💬 এডমিনের মেসেজ:\n\n" + text, build_markup(label, link, reply_btn=True)
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


# ============ রেফারেল সিস্টেম ============
def referral_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def give_referral_bonus(context, referred_id, amount=None):
    """রেফার্ড ইউজারের রেফারারকে বোনাস দেয়। প্রতি রেফার্ডের জন্য মাত্র একবার।"""
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
        context, referrer, f"🎁 রেফারেল বোনাস! আপনার রেফার করা একজনের কাজ অনুমোদিত হয়েছে, আপনি {amt} টাকা পেয়েছেন।"
    )
    return referrer, amt


async def referral_after_approval(context, referred_id):
    """রেফার্ড ইউজারের প্রথম কাজ অনুমোদিত হলে চালু হয়।"""
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
            [ibtn(f"🎁 বোনাস দিন ({bonus} টাকা)", f"refpay:{referred_id}", style="success")],
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
    mode = (context.args[0].lower() if context.args else "")
    if mode not in ("auto", "manual"):
        await update.message.reply_text(
            "ব্যবহার: /refmode auto  অথবা  /refmode manual\n\n"
            "manual = রেফার্ডের প্রথম কাজ অনুমোদনের পর এডমিন বাটনে চেপে বোনাস দেবেন\n"
            "auto = অনুমোদনের সাথে সাথে বোনাস নিজে থেকে চলে যাবে\n\n"
            f"বর্তমান: {cfg.get('ref_mode', 'manual')}"
        )
        return
    cfg["ref_mode"] = mode
    save_config()
    await update.message.reply_text(f"✅ রেফারেল মোড: {mode}")


@admin_only
async def ref_bonus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/refbonus [User_ID] [টাকা] — যেকোনো ইউজারকে সরাসরি রেফারেল বোনাস দেয়।"""
    try:
        tid = int(context.args[0])
        amt = float(context.args[1]) if len(context.args) > 1 else float(cfg["ref_bonus"])
    except Exception:
        await update.message.reply_text("ব্যবহার: /refbonus [User_ID] [টাকা]")
        return
    st = get_user_stats(tid)
    st["balance"] += amt
    st["ref_earned"] = st.get("ref_earned", 0.0) + amt
    save_user_stats()
    await update.message.reply_text(f"✅ {tid}-কে {amt} টাকা বোনাস দেওয়া হয়েছে। ব্যালেন্স: {st['balance']}")
    await safe_send(context, tid, f"🎁 আপনি রেফারেল বোনাস হিসেবে {amt} টাকা পেয়েছেন!")



# ============ /start ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = user.id not in load_registered_users()
    # আগের ডাটা (ব্যালেন্স ইত্যাদি) থাকলে সেটাই ফিরে আসে, না থাকলে নতুন তৈরি হয়
    st = get_user_stats(user.id)
    # রেফারেল লিংক: /start ref_12345 (শুধু একদম নতুন ইউজারের জন্য, নিজেকে রেফার করা যায় না)
    arg = context.args[0] if context.args else ""
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        rid = int(arg[4:])
        if rid != user.id and rid in load_registered_users() and not st.get("referred_by"):
            st["referred_by"] = rid
            get_user_stats(rid)["ref_count"] += 1
            await safe_send(
                context, rid, f"👥 আপনার রেফারেল লিংকে {user.first_name} যুক্ত হয়েছেন! "
                "উনার প্রথম কাজ অনুমোদিত হলে আপনি বোনাস পাবেন।"
            )
    save_user_stats()
    save_user_id(user.id)
    if user.id in ADMINS:
        await apply_menu_commands(context.bot)
    if not await is_member(context.bot, user.id):
        await ask_join(update.message)
        return
    await update.message.reply_text(
        f"স্বাগতম, {user.first_name}! 👋\nনিচের মেনু থেকে অপশন বেছে নিন।",
        reply_markup=main_keyboard(update),
    )


# ============ অ্যাডমিন: কাজ যোগ / মুছে ফেলা ============
def make_add_task(cmd, label, store, filename):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        txt = arg_text(update)
        if not txt:
            await update.message.reply_text(f"ব্যবহার: /{cmd} [কাজের বিবরণ]")
            return
        store.append(txt)
        save_json(filename, store)
        await update.message.reply_text(
            f"✅ {label} কাজ যোগ করা হয়েছে! মোট: {len(store)} টি।"
        )

    return handler


def make_clear_tasks(label, store, filename):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        store.clear()
        save_json(filename, store)
        await update.message.reply_text(f"🗑️ সমস্ত {label} কাজ মুছে ফেলা হয়েছে!")

    return handler


add_gmail = make_add_task("addtask", "জিমেইল", gmail_tasks, GMAIL_FILE)
clear_gmail = make_clear_tasks("জিমেইল", gmail_tasks, GMAIL_FILE)
add_ig = make_add_task("addigtask", "ইনস্টাগ্রাম", instagram_tasks, INSTAGRAM_FILE)
clear_ig = make_clear_tasks("ইনস্টাগ্রাম", instagram_tasks, INSTAGRAM_FILE)


# ============ অ্যাডমিন: ফেসবুক অ্যাকাউন্ট স্টক ============
@admin_only
async def add_fb_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addfb নাম | পদবি | পাসওয়ার্ড  (একসাথে অনেক লাইনে অনেকগুলো দেওয়া যায়, আলাদা করতে | বা । দুটোই চলবে)"""
    body = arg_text(update)
    added, bad = 0, []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in re.split(r"[|।｜]", line)]
        if len(parts) != 3 or not all(parts):
            bad.append(line)
            continue
        fb_accounts.append(
            {"first_name": parts[0], "last_name": parts[1], "password": parts[2]}
        )
        added += 1
    if added:
        save_json(FB_ACCOUNTS_FILE, fb_accounts)
    if not added and not bad:
        await update.message.reply_text(
            "ব্যবহার: /addfb প্রথম নাম | শেষ নাম | পাসওয়ার্ড\n"
            "(আলাদা করতে | অথবা ।  দুটোই চলবে)\n\n"
            "একসাথে অনেকগুলো দিতে প্রতি লাইনে একটা করে লিখুন:\n"
            "/addfb রাকিব | হাসান | Pass@123\n"
            "তানভীর | আহমেদ | Fb@98765"
        )
        return
    msg = f"✅ {added} টি ফেসবুক অ্যাকাউন্ট যোগ হয়েছে! স্টকে মোট: {len(fb_accounts)} টি।"
    if bad:
        msg += "\n\n⚠️ ভুল ফরম্যাটের লাইন (যোগ হয়নি):\n" + "\n".join(bad[:10])
    await update.message.reply_text(msg)


@admin_only
async def list_fb_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not fb_accounts:
        await update.message.reply_text("📭 স্টকে কোনো ফেসবুক অ্যাকাউন্ট নেই। /addfb দিয়ে যোগ করুন।")
        return
    lines = [
        f"{idx}. {a['first_name']} {a['last_name']} | {a['password']}"
        for idx, a in enumerate(fb_accounts[:30], 1)
    ]
    more = f"\n... আরও {len(fb_accounts) - 30} টি" if len(fb_accounts) > 30 else ""
    await update.message.reply_text(
        f"📘 স্টকে ফেসবুক অ্যাকাউন্ট: {len(fb_accounts)} টি\n\n" + "\n".join(lines) + more
    )


@admin_only
async def clear_fb_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb_accounts.clear()
    save_json(FB_ACCOUNTS_FILE, fb_accounts)
    await update.message.reply_text("🗑️ স্টকের সব ফেসবুক অ্যাকাউন্ট মুছে ফেলা হয়েছে!")


def release_fb_account(context):
    """ইউজার কাজ বাতিল করলে বা মাঝপথে চলে গেলে অ্যাকাউন্ট আবার স্টকে ফেরত যায়।"""
    acc = context.user_data.pop("fb_account", None)
    context.user_data.pop("fb_uid", None)
    if acc:
        fb_accounts.insert(0, acc)
        save_json(FB_ACCOUNTS_FILE, fb_accounts)


def fb_account_text(acc):
    return (
        "📘 ফেসবুক অ্যাকাউন্টের তথ্য:\n\n"
        f"👤 First Name: <code>{esc(acc['first_name'])}</code>\n"
        f"👤 Last Name: <code>{esc(acc['last_name'])}</code>\n"
        f"🔑 Password: <code>{esc(acc['password'])}</code>\n\n"
        "কাজটি সম্পন্ন করতে নিচে <b>UID দিন</b> বাটনে ক্লিক করুন "
        "অথবা ফিরে যেতে <b>❌ বাতিল করুন</b> চাপুন।"
    )


# ফেসবুক কাজের রেজাল্ট একবারের বেশি পাঠানো ঠেকাতে (gmail এর GM_DONE এর মতো)
FB_DONE = set()
FB_OK_TEXT = "✅ আপনার ফেসবুক কাজটি সফলভাবে সাপোর্ট করা হয়েছে।"
FB_NO_TEXT = "❌ আপনার ফেসবুক কাজটি রিজেক্ট করা হয়েছে।"


# ============ অ্যাডমিন: দাম ও ইমোজি ============
PRICE_KEYS = {
    "gmail": ("gmail_price", "জিমেইল"),
    "fb": ("fb_price", "ফেসবুক"),
    "facebook": ("fb_price", "ফেসবুক"),
    "ig": ("ig_price", "ইনস্টাগ্রাম"),
    "instagram": ("ig_price", "ইনস্টাগ্রাম"),
}


@admin_only
async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ব্যবহার: /setprice gmail 16  (gmail / fb / ig)"""
    usage = (
        "ব্যবহার: /setprice [gmail/fb/ig] [টাকা]\n"
        "উদাহরণ: /setprice gmail 16\n\n"
        f"বর্তমান দাম:\n📧 জিমেইল: {cfg['gmail_price']} টাকা\n"
        f"📘 ফেসবুক: {cfg['fb_price']} টাকা\n📷 ইনস্টাগ্রাম: {cfg['ig_price']} টাকা"
    )
    try:
        service = context.args[0].lower()
        amount = float(context.args[1])
        if amount <= 0 or service not in PRICE_KEYS:
            raise ValueError
    except Exception:
        await update.message.reply_text(usage)
        return
    key, label = PRICE_KEYS[service]
    cfg[key] = int(amount) if amount == int(amount) else amount
    save_config()
    await update.message.reply_text(
        f"✅ এখন থেকে একটি {label} একাউন্টের দাম {cfg[key]} টাকা দেখাবে।"
    )


@admin_only
async def set_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = ", ".join(BTN.keys())
    if len(context.args) != 2 or context.args[0] not in BTN:
        await update.message.reply_text(
            f"ব্যবহার: /setemoji [বাটন] [প্রিমিয়াম ইমোজি আইডি]\nবাটন: {keys}"
        )
        return
    cfg["emoji_ids"][context.args[0]] = context.args[1]
    save_config()
    await update.message.reply_text("✅ ইমোজি সেট হয়েছে। ইউজার /start দিলে নতুন মেনু দেখবে।")


# ============ অ্যাডমিন: ব্যালেন্স / রিজেক্ট / ইউজার ============
@admin_only
async def add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tid = int(context.args[0])
        amt = float(context.args[1])
    except Exception:
        await update.message.reply_text("ব্যবহার: /add [User_ID] [টাকা]")
        return
    st = get_user_stats(tid)
    st["balance"] += amt
    if st["pending"] > 0:
        st["pending"] -= 1
    st["completed"] += 1
    save_user_stats()
    await update.message.reply_text(
        f"✅ ব্যালেন্স যোগ সফল। বর্তমান ব্যালেন্স: {st['balance']} টাকা।"
    )
    await safe_send(
        context, tid, f"🎉 আপনার কাজটি অনুমোদিত হয়েছে, অ্যাকাউন্টে {amt} টাকা যোগ হয়েছে!"
    )
    await referral_after_approval(context, tid)


@admin_only
async def cut_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tid = int(context.args[0])
        amt = float(context.args[1])
    except Exception:
        await update.message.reply_text("ব্যবহার: /cut [User_ID] [টাকা]")
        return
    st = get_user_stats(tid)
    st["balance"] = max(0.0, st["balance"] - amt)
    save_user_stats()
    await update.message.reply_text(f"✅ কাটা হয়েছে। ব্যালেন্স: {st['balance']} টাকা।")
    await safe_send(context, tid, f"⚠️ আপনার অ্যাকাউন্ট থেকে {amt} টাকা কাটা হয়েছে।")


@admin_only
async def handle_admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ছবির ক্যাপশনে /paid [User_ID] লিখলে ছবিটি পেমেন্ট প্রুফ হিসেবে ইউজারের কাছে যায়।"""
    caption = (update.message.caption or "").strip()
    low = caption.lower()
    photo_id = update.message.photo[-1].file_id
    if low.startswith("/notice"):
        await do_notice(update, context, caption[len("/notice"):].strip(), photo=photo_id)
        return
    if low.startswith("/dm"):
        await do_msg(update, context, caption[len("/dm"):].strip(), photo=photo_id, oneway=True)
        return
    if not low.startswith("/paid"):
        return
    parts = caption.split(maxsplit=2)
    try:
        tid = int(parts[1])
    except Exception:
        await update.message.reply_text("ক্যাপশনে লিখুন: /paid [User_ID] [বার্তা]")
        return
    note = parts[2].strip() if len(parts) > 2 else ""
    msg = "✅ আপনার উইথড্র সফলভাবে সম্পন্ন হয়েছে! পেমেন্টের প্রমাণ:"
    if note:
        msg += f"\n\n{note}"
    try:
        await context.bot.send_photo(
            chat_id=tid, photo=update.message.photo[-1].file_id, caption=msg
        )
        await update.message.reply_text(f"✅ ইউজার {tid}-কে প্রুফ পাঠানো হয়েছে।")
    except Exception:
        await update.message.reply_text("❌ পাঠানো যায়নি। User_ID দেখুন।")
        return
    # একই প্রুফ উইথড্র চ্যানেলেও যাবে
    channel_caption = f"✅ <b>পেমেন্ট সম্পন্ন</b>\n\n👤 ইউজার আইডি: <code>{tid}</code>"
    if note:
        channel_caption += f"\n📝 {esc(note)}"
    await post_to_channel(
        context, channel_caption, photo=update.message.photo[-1].file_id
    )


# ============ মেনু টেক্সট হ্যান্ডলার ============
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    user = update.effective_user
    save_user_id(user.id)
    key = TEXT_TO_KEY.get(text)
    if not key:
        if context.user_data.get("contact_mode") and user.id not in ADMINS:
            await forward_to_admins(update, context, text=text)
        return
    context.user_data.pop("contact_mode", None)

    if not await is_member(context.bot, user.id):
        await ask_join(update.message)
        return

    if key == "admin":
        if user.id in ADMINS:
            await update.message.reply_text(
                build_stats_text(), parse_mode="HTML", reply_markup=stats_markup()
            )
        return

    if key == "tasks":
        await update.message.reply_text(
            "🛠️ কাজের ক্যাটাগরি:", reply_markup=task_menu_keyboard()
        )
    elif key == "cancel":
        await update.message.reply_text("প্রধান মেনু:", reply_markup=main_keyboard(update))
    elif key == "gmail":
        kb = [[ibtn(f"একটি জিমেইল একাউন্টের দাম {cfg['gmail_price']} টাকা 💸", "gmail_details", style="success")]]
        await update.message.reply_text("📧 জিমেইল কাজ তথ্য:", reply_markup=InlineKeyboardMarkup(kb))
    elif key == "fb":
        kb = [[ibtn(f"একটি ফেসবুক একাউন্টের দাম {cfg['fb_price']} টাকা 💸", "fb_details", style="success")]]
        await update.message.reply_text("📘 ফেসবুক কাজ তথ্য:", reply_markup=InlineKeyboardMarkup(kb))
    elif key == "ig":
        kb = [[ibtn(f"একটি ইনস্টাগ্রাম একাউন্টের দাম {cfg['ig_price']} টাকা 💸", "insta_details", style="success")]]
        await update.message.reply_text("📷 ইনস্টাগ্রাম কাজ তথ্য:", reply_markup=InlineKeyboardMarkup(kb))
    elif key == "balance":
        st = get_user_stats(user.id)
        await update.message.reply_text(
            "👤 আপনার তথ্য:\n\n"
            f"✅ কাজ করেছেন: {st['completed']}\n⏳ পেন্ডিং: {st['pending']}\n"
            f"💰 ব্যালেন্স: {st['balance']} টাকা",
            reply_markup=main_keyboard(update),
        )
    elif key == "withdraw":
        kb = [
            [ibtn("বিকাশ", "withdraw_bkash", style="danger")],
            [ibtn("নগদ", "withdraw_nagad", style="danger")],
            [ibtn("Binance", "withdraw_binance", style="success")],
        ]
        await update.message.reply_text("💳 টাকা তুলুন:", reply_markup=InlineKeyboardMarkup(kb))
    elif key == "support":
        if SUPPORT_USER:
            msg = f"📞 সাপোর্ট: @{SUPPORT_USER}"
        else:
            msg = "📞 সাপোর্ট এখনো সেট করা হয়নি।"
        kb = InlineKeyboardMarkup(
            [[ibtn("✉️ এডমিনকে মেসেজ লিখুন", "contact_admin", style="primary")]]
        )
        await update.message.reply_text(msg, reply_markup=kb)
    elif key == "refer":
        st = get_user_stats(user.id)
        link = referral_link(context.bot.username, user.id)
        share = "https://t.me/share/url?url=" + urllib.parse.quote(link) + "&text=" + urllib.parse.quote(
            "এই বটে কাজ করে টাকা ইনকাম করুন!"
        )
        kb = InlineKeyboardMarkup([[ibtn("📤 শেয়ার করুন", url=share, style="success")]])
        await update.message.reply_text(
            "🎁 <b>রেফার করুন, বোনাস জিতুন</b>\n\n"
            f"আপনার রেফারেল লিংক:\n<code>{link}</code>\n\n"
            f"👥 আপনার রেফার: {st.get('ref_count', 0)} জন\n"
            f"💰 রেফারেল থেকে আয়: {st.get('ref_earned', 0)} টাকা\n\n"
            f"আপনার রেফার করা কেউ প্রথম কাজ সফলভাবে শেষ করলে আপনি পাবেন {cfg['ref_bonus']} টাকা বোনাস।",
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif key == "howto":
        if GROUP_LINK:
            kb = [[ibtn("গ্রুপ 🔗", url=GROUP_LINK)]]
            await update.message.reply_text(
                "কিভাবে কাজ করবেন তা জানতে আমাদের গ্রুপে যোগ দিন:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        else:
            await update.message.reply_text("গ্রুপ এখনো সেট করা হয়নি।")
    elif key == "cancel_task":
        for mid in (update.message.message_id, context.user_data.get("last_task_msg_id")):
            if mid:
                try:
                    await context.bot.delete_message(chat_id=user.id, message_id=mid)
                except Exception:
                    pass
        clear_task_state(context)
        await update.message.reply_text("প্রধান মেনু:", reply_markup=main_keyboard(update))


# ============ ইনলাইন বাটন (কনভারসেশনের বাইরে) ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = update.effective_user

    if data == "check_join":
        if await is_member(context.bot, user.id):
            await query.answer()
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.reply_text(
                f"✅ ধন্যবাদ, {user.first_name}! এখন নিচের মেনু ব্যবহার করুন।",
                reply_markup=main_keyboard(update),
            )
        else:
            await query.answer(
                "❌ আপনি এখনো চ্যানেলে জয়েন করেননি! আগে জয়েন করুন।", show_alert=True
            )
        return

    await query.answer()

    if data == "admin_stats":
        if user.id not in ADMINS:
            return
        try:
            await query.edit_message_text(
                build_stats_text(), parse_mode="HTML", reply_markup=stats_markup()
            )
        except Exception:
            pass  # তথ্য একই থাকলে টেলিগ্রাম এরর দেয়, সেটা উপেক্ষা করা হলো
        return

    if data.startswith(("gm_ok:", "gm_no:")):
        if user.id not in ADMINS:
            return
        try:
            _, uid, sid = data.split(":")
            uid = int(uid)
        except Exception:
            return
        done_key = f"{uid}:{sid}"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if done_key in GM_DONE:
            await query.message.reply_text("ℹ️ এই কাজের রেজাল্ট আগেই পাঠানো হয়েছে।")
            return
        GM_DONE.add(done_key)
        ok = data.startswith("gm_ok:")
        sent = await safe_send(context, uid, GM_OK_TEXT if ok else GM_NO_TEXT)
        label = "সঠিক ✅" if ok else "রিজেক্ট ❌"
        if sent:
            await query.message.reply_text(f"{label} — ইউজারকে মেসেজ পাঠানো হয়েছে।")
        else:
            await query.message.reply_text(f"{label} — কিন্তু ইউজারকে মেসেজ পাঠানো যায়নি।")
        return

    if data.startswith(("fb_ok:", "fb_no:")):
        if user.id not in ADMINS:
            return
        try:
            _, uid, sid = data.split(":")
            uid = int(uid)
        except Exception:
            return
        done_key = f"{uid}:{sid}"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if done_key in FB_DONE:
            await query.message.reply_text("ℹ️ এই কাজের রেজাল্ট আগেই পাঠানো হয়েছে।")
            return
        FB_DONE.add(done_key)
        ok = data.startswith("fb_ok:")
        sent = await safe_send(context, uid, FB_OK_TEXT if ok else FB_NO_TEXT)
        label = "সঠিক ✅" if ok else "রিজেক্ট ❌"
        if sent:
            await query.message.reply_text(f"{label} — ইউজারকে মেসেজ পাঠানো হয়েছে।")
        else:
            await query.message.reply_text(f"{label} — কিন্তু ইউজারকে মেসেজ পাঠানো যায়নি।")
        return

    if data.startswith(("refpay:", "refno:")):
        if user.id not in ADMINS:
            return
        try:
            referred = int(data.split(":")[1])
        except Exception:
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        st = get_user_stats(referred)
        if st.get("ref_paid"):
            await query.message.reply_text("ℹ️ এই রেফারেল আগেই নিষ্পত্তি হয়েছে।")
            return
        if data.startswith("refno:"):
            st["ref_paid"] = True
            save_user_stats()
            await query.message.reply_text("🚫 এই রেফারেলে বোনাস দেওয়া হবে না।")
            return
        res = await give_referral_bonus(context, referred)
        if res:
            await query.message.reply_text(f"✅ {res[0]}-কে {res[1]} টাকা বোনাস দেওয়া হয়েছে।")
        return

    if not await is_member(context.bot, user.id):
        await ask_join(query.message)
        return

    if data == "contact_admin":
        context.user_data["contact_mode"] = True
        await query.message.reply_text(
            "✍️ এডমিনের জন্য আপনার মেসেজ লিখুন (ছবিও পাঠাতে পারবেন)।\n"
            "বাতিল করতে যেকোনো মেনু বাটন চাপুন।"
        )
        return

    if data == "gmail_details":
        if not gmail_tasks:
            await query.message.reply_text("⚠️ জিমেইল কাজ নেই!", reply_markup=task_menu_keyboard())
            return
        task_text = gmail_tasks[0]
        context.user_data["current_task_text"] = task_text
        context.user_data["current_task_type"] = "gmail"
        sent = await query.message.reply_text(
            f"📌 জিমেইল কাজ:\n\n{task_text}", reply_markup=task_keyboard()
        )
        context.user_data["last_task_msg_id"] = sent.message_id
        return

    if data == "fb_details":
        # একই ইউজার আবার চাপলে নতুন অ্যাকাউন্ট না দিয়ে আগেরটাই দেখাবে
        acc = context.user_data.get("fb_account")
        if not acc:
            if not fb_accounts:
                await query.message.reply_text("⚠️ ফেসবুক কাজ নেই!", reply_markup=task_menu_keyboard())
                return
            acc = fb_accounts.pop(0)
            save_json(FB_ACCOUNTS_FILE, fb_accounts)
            context.user_data["fb_account"] = acc
        kb = [
            [
                ibtn("UID দিন", "fb_uid", style="success"),
                ibtn("❌ বাতিল করুন", "fb_cancel", style="danger"),
            ]
        ]
        await query.message.reply_text(
            fb_account_text(acc), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "insta_details":
        if not instagram_tasks:
            await query.message.reply_text("⚠️ ইনস্টাগ্রাম কাজ নেই!", reply_markup=task_menu_keyboard())
            return
        task_text = instagram_tasks[0]
        context.user_data["current_task_text"] = task_text
        context.user_data["current_task_type"] = "instagram"
        kb = [[ibtn("✅ Done", "insta_done", style="success")]]
        await query.message.reply_text(
            f"📷 ইনস্টাগ্রাম কাজের নির্দেশিকা:\n\n{task_text}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "insta_done":
        st = get_user_stats(user.id)
        st["pending"] += 1
        save_user_stats()
        tdesc = context.user_data.get("current_task_text", "বিবরণ নেই")
        if tdesc in instagram_tasks:
            instagram_tasks.remove(tdesc)
            save_json(INSTAGRAM_FILE, instagram_tasks)
        await notify_admin(
            context,
            "📷 ইনস্টাগ্রাম কাজ জমা হয়েছে\n"
            f"ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n"
            f"কাজের বিবরণ:\n{esc(tdesc[:600])}",
        )
        clear_task_state(context)
        await query.message.reply_text(
            "✅ আপনার কাজ সম্পন্ন হয়েছে!\n\nকাজটি সঠিক হলে ব্যালেন্সে টাকা যুক্ত হবে।",
            reply_markup=main_keyboard(update),
        )
        return


# ============ কনভারসেশন এন্ট্রি ============
async def conv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    st = get_user_stats(user.id)

    if not await is_member(context.bot, user.id):
        await ask_join(query.message)
        return ConversationHandler.END

    if data == "fb_uid":
        if not context.user_data.get("fb_account"):
            await query.message.reply_text(
                "⚠️ আপনার কোনো সক্রিয় ফেসবুক কাজ নেই। আগে কাজ নিন।",
                reply_markup=main_keyboard(update),
            )
            return ConversationHandler.END
        await query.message.reply_text(
            "📝 আপনার ফেসবুক অ্যাকাউন্টের ১৪ থেকে ১৬ সংখ্যার UID-টি দিন:\n(বাতিল করতে /cancel)"
        )
        return FB_UID

    if data == "fb_cancel":
        release_fb_account(context)
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(
            "❌ কাজটি বাতিল করা হয়েছে। আপনি আগের মেনুতে ফিরে এসেছেন।",
            reply_markup=main_keyboard(update),
        )
        return ConversationHandler.END

    if data in ("withdraw_bkash", "withdraw_nagad"):
        min_amt = cfg["min_withdraw"]
        if st["balance"] < min_amt:
            await query.message.reply_text(
                f"❌ সর্বনিম্ন {min_amt}৳ ব্যালেন্স থাকতে হবে।", reply_markup=main_keyboard(update)
            )
            return ConversationHandler.END
        method = {
            "withdraw_bkash": "বিকাশ",
            "withdraw_nagad": "নগদ",
        }[data]
        context.user_data["withdraw_method"] = method
        context.user_data["withdraw_min"] = min_amt
        await query.message.reply_text(f"আপনার {method} নাম্বার দিন:")
        return WITHDRAW_NUM

    if data == "withdraw_binance":
        min_amt = cfg["min_binance"]
        if st["balance"] < min_amt:
            await query.message.reply_text(
                f"❌ সর্বনিম্ন ৳{min_amt} ব্যালেন্স থাকতে হবে।", reply_markup=main_keyboard(update)
            )
            return ConversationHandler.END
        context.user_data["withdraw_method"] = "Binance"
        context.user_data["withdraw_min"] = min_amt
        await query.message.reply_text("USDT (BEP-20) অ্যাড্রেস দিন:")
        return WITHDRAW_NUM

    return ConversationHandler.END


# ============ উইথড্র ============
async def receive_withdraw_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["withdraw_info"] = update.message.text
    await update.message.reply_text("কত টাকা উইথড্র করবেন লিখুন:")
    return WITHDRAW_AMT


async def receive_withdraw_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amt = float(update.message.text)
    except ValueError:
        await update.message.reply_text("সঠিক সংখ্যা লিখুন:")
        return WITHDRAW_AMT

    min_amt = context.user_data.get("withdraw_min", cfg["min_withdraw"])
    if amt < min_amt:
        await update.message.reply_text(f"❌ সর্বনিম্ন {min_amt}৳ উইথড্র করতে হবে। আবার লিখুন:")
        return WITHDRAW_AMT

    st = get_user_stats(user.id)
    if amt > st["balance"]:
        await update.message.reply_text("❌ পর্যাপ্ত ব্যালেন্স নেই।", reply_markup=main_keyboard(update))
        return ConversationHandler.END

    st["balance"] -= amt
    save_user_stats()
    await notify_admin(
        context,
        "💸 উইথড্র রিকোয়েস্ট\n"
        f"ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n"
        f"মাধ্যম: {esc(context.user_data.get('withdraw_method', ''))}\n"
        f"পরিমাণ: {amt}\n"
        f"তথ্য: <code>{esc(context.user_data.get('withdraw_info'))}</code>",
    )
    # চ্যানেলে যাবে — কিন্তু বিকাশ/নগদ নাম্বার বা Binance অ্যাড্রেস ছাড়া
    await post_to_channel(
        context,
        "💸 <b>নতুন উইথড্র রিকোয়েস্ট</b>\n\n"
        f"👤 ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n"
        f"💳 মাধ্যম: {esc(context.user_data.get('withdraw_method', ''))}\n"
        f"💰 পরিমাণ: {amt} টাকা\n"
        "⏳ স্ট্যাটাস: প্রসেসিং",
    )
    await update.message.reply_text(
        f"✅ {amt} টাকার উইথড্র রিকোয়েস্ট জমা হয়েছে!", reply_markup=main_keyboard(update)
    )
    return ConversationHandler.END


# ============ কাজের স্ক্রিনশট জমা (জিমেইল) ============
async def ask_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_member(context.bot, update.effective_user.id):
        await ask_join(update.message)
        return ConversationHandler.END
    if not context.user_data.get("current_task_text"):
        await update.message.reply_text(
            "⚠️ আপনার কোনো সক্রিয় কাজ নেই। আগে কাজ নিন।", reply_markup=main_keyboard(update)
        )
        return ConversationHandler.END
    await update.message.reply_text("📸 স্ক্রিনশট জমা দিন:")
    return PROOF_SUBMIT


async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.photo:
        await update.message.reply_text("দয়া করে স্ক্রিনশট (ছবি) পাঠান:")
        return PROOF_SUBMIT

    photo = update.message.photo[-1].file_id
    st = get_user_stats(user.id)
    st["pending"] += 1
    save_user_stats()

    tdesc = context.user_data.get("current_task_text", "বিবরণ নেই")
    if tdesc in gmail_tasks:
        gmail_tasks.remove(tdesc)
        save_json(GMAIL_FILE, gmail_tasks)

    sid = int(time.time())
    review_kb = InlineKeyboardMarkup(
        [
            [ibtn("✅ সঠিক (সাপোর্টেড)", f"gm_ok:{user.id}:{sid}", style="success")],
            [ibtn("❌ রিজেক্ট", f"gm_no:{user.id}:{sid}", style="danger")],
        ]
    )
    await notify_admin(
        context,
        "📩 স্ক্রিনশট জমা হয়েছে\n"
        f"ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n"
        f"কাজের বিবরণ:\n{esc(tdesc[:600])}",
        photo=photo,
        reply_markup=review_kb,
    )
    clear_task_state(context)
    await update.message.reply_text("✅ স্ক্রিনশট সাবমিট হয়েছে!", reply_markup=main_keyboard(update))
    return ConversationHandler.END


# ============ ফেসবুক কাজ: UID ও Cookies ============
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


async def receive_fb_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = (update.message.text or "").translate(BN_DIGITS).strip().replace(" ", "")
    if not re.fullmatch(r"[0-9]{14,16}", uid):
        await update.message.reply_text(
            "⚠️ UID ঠিক নেই! ১৪ থেকে ১৬ সংখ্যার UID দিন (শুধু সংখ্যা):"
        )
        return FB_UID
    context.user_data["fb_uid"] = uid
    await update.message.reply_text("🍪 এবার আপনার ফেসবুক অ্যাকাউন্টের Cookies-টি দিন:")
    return FB_COOKIES


async def receive_fb_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cookies = (update.message.text or "").strip()
    acc = context.user_data.get("fb_account")
    uid = context.user_data.get("fb_uid")
    if not acc or not uid:
        await update.message.reply_text(
            "⚠️ আপনার কোনো সক্রিয় ফেসবুক কাজ নেই। আগে কাজ নিন।",
            reply_markup=main_keyboard(update),
        )
        return ConversationHandler.END
    if len(cookies) < 20:
        await update.message.reply_text("⚠️ Cookies ঠিক নেই! পুরো Cookies কপি করে দিন:")
        return FB_COOKIES

    st = get_user_stats(user.id)
    st["pending"] += 1
    save_user_stats()

    price = cfg["fb_price"]
    sid = int(time.time())
    send_as_file = len(cookies) > 2500
    review_kb = InlineKeyboardMarkup(
        [
            [ibtn("✅ সঠিক (সাপোর্টেড)", f"fb_ok:{user.id}:{sid}", style="success")],
            [ibtn("❌ রিজেক্ট", f"fb_no:{user.id}:{sid}", style="danger")],
        ]
    )
    report = (
        "📘 নতুন ফেসবুক কাজ জমা হয়েছে\n\n"
        f"👤 ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n\n"
        "📋 দেওয়া অ্যাকাউন্ট:\n"
        f"• First Name: <code>{esc(acc['first_name'])}</code>\n"
        f"• Last Name: <code>{esc(acc['last_name'])}</code>\n"
        f"• Password: <code>{esc(acc['password'])}</code>\n\n"
        f"📝 UID: <code>{esc(uid)}</code>\n"
    )
    if send_as_file:
        report += "🍪 Cookies: (বড় হওয়ায় নিচের ফাইলে দেওয়া হয়েছে)\n"
    else:
        report += f"🍪 Cookies:\n<code>{esc(cookies)}</code>\n"
    report += f"\n✅ ব্যালেন্স যোগ করতে: <code>/add {user.id} {price}</code>"

    await notify_admin(context, report, reply_markup=review_kb)
    if send_as_file:
        for admin_id in ADMINS:
            try:
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=io.BytesIO(cookies.encode("utf-8")),
                    filename=f"cookies_{uid}.txt",
                )
            except Exception as e:
                logging.error("Cookies file send failed: %s", e)

    context.user_data.pop("fb_account", None)
    context.user_data.pop("fb_uid", None)
    await update.message.reply_text(
        "✅ আপনার UID এবং Cookies সফলভাবে জমা হয়েছে!\n\n"
        "এটি রিভিউতে আছে। সব ঠিক থাকলে পেমেন্ট আপনার ব্যালেন্সে যুক্ত হবে।",
        reply_markup=main_keyboard(update),
    )
    return ConversationHandler.END


# ============ ক্যান্সেল / রাউটিং ============
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    release_fb_account(context)
    await update.message.reply_text("বাতিল করা হয়েছে।", reply_markup=main_keyboard(update))
    return ConversationHandler.END


async def cancel_and_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """কনভারসেশনের মাঝে মেনু বাটন চাপলে সেটা চালু করে দেয়।"""
    release_fb_account(context)
    if update.message.text == BTN["submit"][0]:
        return await ask_proof(update, context)
    await handle_text_messages(update, context)
    return ConversationHandler.END


USER_COMMANDS = [BotCommand("start", "বট শুরু করুন")]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("addtask", "জিমেইল কাজ যোগ: /addtask কাজের লেখা"),
    BotCommand("cleartasks", "সব জিমেইল কাজ মুছুন"),
    BotCommand("addfb", "ফেসবুক অ্যাকাউন্ট যোগ করুন (নাম | পদবি | পাসওয়ার্ড)"),
    BotCommand("fblist", "স্টকের ফেসবুক অ্যাকাউন্ট দেখুন"),
    BotCommand("clearfb", "স্টকের সব ফেসবুক অ্যাকাউন্ট মুছুন"),
    BotCommand("addigtask", "ইনস্টাগ্রাম কাজ যোগ: /addigtask কাজের লেখা"),
    BotCommand("clearigtasks", "সব ইনস্টাগ্রাম কাজ মুছুন"),
    BotCommand("setprice", "দাম বদলান: /setprice gmail 16"),
    BotCommand("add", "ব্যালেন্স যোগ: /add আইডি টাকা"),
    BotCommand("cut", "ব্যালেন্স কাটুন: /cut আইডি টাকা"),
    BotCommand("paid", "ছবির ক্যাপশনে লিখুন: /paid আইডি"),
    BotCommand("setemoji", "বাটনে প্রিমিয়াম ইমোজি বসান"),
    BotCommand("stats", "মেম্বার ও কাজের পরিসংখ্যান দেখুন"),
    BotCommand("backup", "এখনই ডাটা ব্যাকআপ নিন"),
    BotCommand("notice", "সব ইউজারকে নোটিশ: /notice বার্তা | লিংক"),
    BotCommand("dm", "একতরফা মেসেজ (রিপ্লাই বন্ধ): /dm আইডি বার্তা"),
    BotCommand("setrefbonus", "রেফারেল বোনাস: /setrefbonus টাকা"),
    BotCommand("refmode", "রেফারেল মোড: /refmode auto|manual"),
    BotCommand("refbonus", "সরাসরি বোনাস: /refbonus আইডি টাকা"),
]


async def apply_menu_commands(bot):
    """ইউজার শুধু /start দেখবে, এডমিন সব কমান্ডের প্যানেল দেখবে।"""
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as e:
        logging.warning("ইউজার মেনু সেট করা যায়নি: %s", e)
    for admin_id in ADMINS:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning("এডমিন মেনু সেট করা যায়নি (%s) (এডমিন আইডি দিয়ে বটে /start দিন): %s", admin_id, e)


async def setup_menu_commands(application):
    await apply_menu_commands(application.bot)
    # চালু হওয়ার সাথে সাথে একটি ব্যাকআপ (রিস্টোর হওয়া ডাটাও নিরাপদ থাকে)
    _dirty.set()


# ============ সেটিংস যাচাই ============
def check_settings():
    problems = []
    if _is_placeholder(BOT_TOKEN) or ":" not in str(BOT_TOKEN):
        problems.append("❌ BOT_TOKEN দেওয়া হয়নি। Render → Environment এ BOT_TOKEN সেট করুন।")
    if not ADMIN:
        problems.append("❌ ADMIN_ID দেওয়া হয়নি। Render → Environment এ ADMIN_ID সেট করুন।")
    if problems:
        print("\n".join(problems))
        raise SystemExit(1)
    if not ADMIN_2:
        print("⚠️ দ্বিতীয় এডমিন (ADMIN_ID_2) সেট করা নেই।")
    if not github_enabled():
        print(
            "⚠️ GITHUB_TOKEN / GITHUB_REPO সেট করা নেই — Render রিস্টার্ট দিলে ডাটা মুছে যাবে!"
        )
    if not SUPPORT_USER:
        print("⚠️ সাপোর্ট আইডি দেওয়া হয়নি (SUPPORT_ID)।")
    if not GROUP_LINK:
        print("⚠️ সাপোর্ট চ্যানেল দেওয়া হয়নি (SUPPORT_CHANNEL)।")


# ============ মেইন ============
def main():
    check_settings()
    start_background_threads()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_menu_commands).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(conv_callback, pattern=r"^(withdraw_|fb_uid$|fb_cancel$)"),
            MessageHandler(filters.Regex("^" + re.escape(BTN["submit"][0]) + "$"), ask_proof),
        ],
        states={
            WITHDRAW_NUM: [MessageHandler(TEXT_INPUT, receive_withdraw_num)],
            WITHDRAW_AMT: [MessageHandler(TEXT_INPUT, receive_withdraw_amt)],
            PROOF_SUBMIT: [MessageHandler(filters.PHOTO | TEXT_INPUT, receive_proof)],
            FB_UID: [MessageHandler(TEXT_INPUT, receive_fb_uid)],
            FB_COOKIES: [MessageHandler(TEXT_INPUT, receive_fb_cookies)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(MENU_FILTER, cancel_and_route),
        ],
        allow_reentry=True,
    )

    commands = {
        "start": start,
        "stats": show_stats,
        "setprice": set_price,
        "setemoji": set_emoji,
        "addtask": add_gmail,
        "cleartasks": clear_gmail,
        "addfb": add_fb_account,
        "fblist": list_fb_accounts,
        "clearfb": clear_fb_accounts,
        "addigtask": add_ig,
        "clearigtasks": clear_ig,
        "add": add_balance,
        "cut": cut_balance,
        "backup": backup_cmd,
        "broadcast": notice_cmd,
        "notice": notice_cmd,
        "dm": dm_cmd,
        "setrefbonus": set_ref_bonus,
        "refmode": set_ref_mode,
        "refbonus": ref_bonus_cmd,
    }
    for name, fn in commands.items():
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(
        MessageHandler(filters.PHOTO & filters.User(user_id=ADMINS), handle_admin_photo)
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL & filters.User(user_id=ADMINS), handle_admin_document)
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.REPLY & filters.User(user_id=ADMINS),
            admin_reply,
        )
    )
    app.add_handler(MessageHandler(filters.PHOTO, user_photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("✅ বট চালু হয়েছে...")
    # Python 3.12+ এ ইভেন্ট লুপ নিজে তৈরি করে দিতে হয়
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()

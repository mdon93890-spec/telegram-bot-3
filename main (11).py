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
FACEBOOK_FILE = "facebook_tasks.json"
INSTAGRAM_FILE = "instagram_tasks.json"
USER_STATS_FILE = "user_stats.json"

# ব্যাকআপে যেসব ফাইল যাবে
DATA_FILES = [
    CONFIG_FILE,
    USERS_FILE,
    GMAIL_FILE,
    FACEBOOK_FILE,
    INSTAGRAM_FILE,
    USER_STATS_FILE,
]

PROOF_SUBMIT, WITHDRAW_NUM, WITHDRAW_AMT, FB_NUMBER = range(4)

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


def reload_memory_from_disk():
    """/restore এর পর মেমোরির ডাটা আপডেট করে (একই অবজেক্টে)।"""
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
        [kbtn("howto")],
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


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast [বার্তা] — সব ইউজারকে মেসেজ পাঠায় (নতুন বটে যারা /start দিয়েছে)।"""
    txt = arg_text(update)
    if not txt:
        await update.message.reply_text("ব্যবহার: /broadcast [বার্তা]")
        return
    users = load_registered_users()
    ok = fail = 0
    await update.message.reply_text(f"📤 {len(users)} জনকে পাঠানো শুরু হচ্ছে...")
    for uid in users:
        if await safe_send(context, uid, txt):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ সফল: {ok}\n❌ ব্যর্থ: {fail}")


# ============ /start ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # আগের ডাটা (ব্যালেন্স ইত্যাদি) থাকলে সেটাই ফিরে আসে, না থাকলে নতুন তৈরি হয়
    get_user_stats(user.id)
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
add_fb = make_add_task("addfbtask", "ফেসবুক", facebook_tasks, FACEBOOK_FILE)
clear_fb = make_clear_tasks("ফেসবুক", facebook_tasks, FACEBOOK_FILE)
add_ig = make_add_task("addigtask", "ইনস্টাগ্রাম", instagram_tasks, INSTAGRAM_FILE)
clear_ig = make_clear_tasks("ইনস্টাগ্রাম", instagram_tasks, INSTAGRAM_FILE)


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
    if not caption.lower().startswith("/paid"):
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
        return

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
        await update.message.reply_text(msg, reply_markup=main_keyboard(update))
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

    if not await is_member(context.bot, user.id):
        await ask_join(query.message)
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
        if not facebook_tasks:
            await query.message.reply_text("⚠️ ফেসবুক কাজ নেই!", reply_markup=task_menu_keyboard())
            return
        task_text = facebook_tasks[0]
        context.user_data["current_task_text"] = task_text
        context.user_data["current_task_type"] = "facebook"
        kb = [[ibtn("✅ Done", "fb_done", style="success")]]
        await query.message.reply_text(
            f"📘 ফেসবুক কাজের নির্দেশিকা:\n\n{task_text}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "fb_complete":
        st = get_user_stats(user.id)
        st["pending"] += 1
        save_user_stats()
        tdesc = context.user_data.get("current_task_text", "বিবরণ নেই")
        fb_num = context.user_data.get("fb_number", "দেওয়া হয়নি")
        if tdesc in facebook_tasks:
            facebook_tasks.remove(tdesc)
            save_json(FACEBOOK_FILE, facebook_tasks)
        await notify_admin(
            context,
            "📘 ফেসবুক কাজ জমা হয়েছে\n"
            f"ইউজার: {esc(user.first_name)} (<code>{user.id}</code>)\n"
            f"ফেসবুক নাম্বার: <code>{esc(fb_num)}</code>\n"
            f"কাজের বিবরণ:\n{esc(tdesc[:600])}",
        )
        clear_task_state(context)
        context.user_data.pop("fb_number", None)
        await query.message.reply_text(
            "✅ আপনার কাজ সম্পন্ন হয়েছে!\n\nএটি রিভিউতে আছে। সব ঠিক থাকলে পেমেন্ট আপনার ব্যালেন্সে যুক্ত হবে।",
            reply_markup=main_keyboard(update),
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

    if data == "fb_done":
        if not context.user_data.get("current_task_text"):
            await query.message.reply_text(
                "⚠️ আপনার কোনো সক্রিয় ফেসবুক কাজ নেই। আগে কাজ নিন।",
                reply_markup=main_keyboard(update),
            )
            return ConversationHandler.END
        await query.message.reply_text("🔥 ফেসবুক একাউন্টের নাম্বার দিন:")
        return FB_NUMBER

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


# ============ ফেসবুক নাম্বার ============
async def receive_fb_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    number = (update.message.text or "").strip()
    if not number:
        await update.message.reply_text("🔥 ফেসবুক একাউন্টের নাম্বার দিন:")
        return FB_NUMBER
    context.user_data["fb_number"] = number
    kb = [[ibtn("✅ কাজ সম্পন্ন", "fb_complete", style="success")]]
    await update.message.reply_text(
        "✅ নাম্বার গ্রহণ করা হয়েছে। কাজ সম্পন্ন করতে নিচের বাটনে চাপ দিন।",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ConversationHandler.END


# ============ ক্যান্সেল / রাউটিং ============
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("বাতিল করা হয়েছে।", reply_markup=main_keyboard(update))
    return ConversationHandler.END


async def cancel_and_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """কনভারসেশনের মাঝে মেনু বাটন চাপলে সেটা চালু করে দেয়।"""
    if update.message.text == BTN["submit"][0]:
        return await ask_proof(update, context)
    await handle_text_messages(update, context)
    return ConversationHandler.END


USER_COMMANDS = [BotCommand("start", "বট শুরু করুন")]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("addtask", "জিমেইল কাজ যোগ: /addtask কাজের লেখা"),
    BotCommand("cleartasks", "সব জিমেইল কাজ মুছুন"),
    BotCommand("addfbtask", "ফেসবুক কাজ যোগ: /addfbtask কাজের লেখা"),
    BotCommand("clearfbtasks", "সব ফেসবুক কাজ মুছুন"),
    BotCommand("addigtask", "ইনস্টাগ্রাম কাজ যোগ: /addigtask কাজের লেখা"),
    BotCommand("clearigtasks", "সব ইনস্টাগ্রাম কাজ মুছুন"),
    BotCommand("setprice", "দাম বদলান: /setprice gmail 16"),
    BotCommand("add", "ব্যালেন্স যোগ: /add আইডি টাকা"),
    BotCommand("cut", "ব্যালেন্স কাটুন: /cut আইডি টাকা"),
    BotCommand("paid", "ছবির ক্যাপশনে লিখুন: /paid আইডি"),
    BotCommand("setemoji", "বাটনে প্রিমিয়াম ইমোজি বসান"),
    BotCommand("stats", "মেম্বার ও কাজের পরিসংখ্যান দেখুন"),
    BotCommand("backup", "এখনই ডাটা ব্যাকআপ নিন"),
    BotCommand("broadcast", "সবাইকে মেসেজ: /broadcast বার্তা"),
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
            CallbackQueryHandler(conv_callback, pattern=r"^(withdraw_|fb_done$)"),
            MessageHandler(filters.Regex("^" + re.escape(BTN["submit"][0]) + "$"), ask_proof),
        ],
        states={
            WITHDRAW_NUM: [MessageHandler(TEXT_INPUT, receive_withdraw_num)],
            WITHDRAW_AMT: [MessageHandler(TEXT_INPUT, receive_withdraw_amt)],
            PROOF_SUBMIT: [MessageHandler(filters.PHOTO | TEXT_INPUT, receive_proof)],
            FB_NUMBER: [MessageHandler(TEXT_INPUT, receive_fb_number)],
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
        "addfbtask": add_fb,
        "clearfbtasks": clear_fb,
        "addigtask": add_ig,
        "clearigtasks": clear_ig,
        "add": add_balance,
        "cut": cut_balance,
        "backup": backup_cmd,
        "broadcast": broadcast,
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("✅ বট চালু হয়েছে...")
    app.run_polling()


if __name__ == "__main__":
    main()

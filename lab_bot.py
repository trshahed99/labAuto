"""
Lab Auto Manager
----------------
Telegram বট দিয়ে নিয়ন্ত্রিত Playwright অটোমেশন।
- Playwright ON/OFF সুইচ (ডিফল্ট: OFF)
- সব সেটিং টেলিগ্রাম মেনু থেকে বদলানো যায়
- শুধু OWNER_ID-র মালিক বট চালাতে পারবেন
- গুরুত্বপূর্ণ ঘটনায় টেলিগ্রামে নোটিফিকেশন আসে

চালানোর আগে (টার্মিনালে):
    pip install playwright python-telegram-bot
    playwright install --with-deps chromium
    export TELEGRAM_TOKEN="BotFather-এর টোকেন"
    export OWNER_ID="তোমার টেলিগ্রাম ইউজার আইডি"
    python3 lab_bot.py
"""

import asyncio
import html
import io
import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==========================================
# ⚙️ বেসিক কনফিগ (টোকেন কোডে লিখো না, environment-এ রাখো)
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "9097454535:AAFmnesxWwWoKsp5CXvoByIVMCnzamOlunE")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_NAME = os.getenv("DB_NAME", "lab_config.db")
DASH_URL = "https://labs.2z2.top/dashboard"

DEFAULTS = {
    "LAB_NAME": "rana",
    "START_WITH_STOP": "True",
    "RESTART_WAIT_MIN": "1",
    "EXTRA_TIME_MIN": "9",
    "MAIN_TIME_MIN": "50",
    "COOKIES_JSON": "[]",
    "AUTO_ENABLED": "False",  # ডিফল্ট OFF
}

INT_KEYS = {"RESTART_WAIT_MIN", "EXTRA_TIME_MIN", "MAIN_TIME_MIN"}
LABELS = {
    "LAB_NAME": "🏷 ল্যাবের নাম",
    "COOKIES_JSON": "🍪 কুকি",
    "START_WITH_STOP": "🛑 শুরুতেই Stop",
    "RESTART_WAIT_MIN": "⏱ Restart-এর আগে বিরতি (মিনিট)",
    "EXTRA_TIME_MIN": "➕ অতিরিক্ত টাইম (মিনিট)",
    "MAIN_TIME_MIN": "🎯 কাঙ্ক্ষিত টাইম (মিনিট)",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ==========================================
# 🗄️ ডাটাবেস
# ==========================================
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS configs (key TEXT PRIMARY KEY, value TEXT)")
        for k, v in DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO configs (key, value) VALUES (?, ?)", (k, v))


def get_config(key):
    with sqlite3.connect(DB_NAME) as conn:
        row = conn.execute("SELECT value FROM configs WHERE key = ?", (key,)).fetchone()
    return row[0] if row else DEFAULTS.get(key, "")


def set_config(key, value):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)", (key, str(value)))


def parse_value(key, text):
    """ইউজারের পাঠানো মান যাচাই করে। ভুল হলে ValueError দেয়।"""
    t = text.strip()
    if key in INT_KEYS:
        if not t.isdigit() or not (1 <= int(t) <= 1440):
            raise ValueError("১ থেকে ১৪৪০-এর মধ্যে একটি সংখ্যা দিন।")
        return str(int(t))
    if key == "LAB_NAME":
        if not t:
            raise ValueError("নাম খালি হতে পারে না।")
        return t
    if key == "COOKIES_JSON":
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            raise ValueError("এটা সঠিক JSON নয়। Cookie-Editor থেকে Export করে পুরোটা পাঠান।")
        if not isinstance(data, list) or not data:
            raise ValueError("কুকির তালিকা ([ ... ]) দরকার।")
        for c in data:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                raise ValueError("প্রতিটি কুকিতে name আর value থাকতে হবে।")
        return json.dumps(data, ensure_ascii=False)
    raise ValueError("অজানা সেটিং।")


# ==========================================
# 🤖 Playwright অটোমেশন ফাংশন
# ==========================================
class CookieExpired(Exception):
    pass


def load_cookies():
    raw = json.loads(get_config("COOKIES_JSON") or "[]")
    same = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}
    out = []
    for c in raw:
        item = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", "labs.2z2.top"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        if c.get("sameSite") in same:
            item["sameSite"] = same[c["sameSite"]]
        if c.get("expirationDate") and not c.get("session"):
            item["expires"] = c["expirationDate"]
        out.append(item)
    return out


async def ensure_dashboard(page):
    await page.goto(DASH_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    if "/login" in page.url:
        raise CookieExpired("কুকির মেয়াদ শেষ")
    await page.wait_for_selector("button", timeout=30000)


def card(page, lab_name):
    return (
        page.locator("div")
        .filter(has_text=lab_name)
        .filter(has=page.locator("button"))
        .last
    )


async def click_btn(page, lab_name, names, timeout=15000):
    for scope in (card(page, lab_name), page):
        for name in names:
            btn = scope.get_by_role("button", name=name, exact=False)
            try:
                await btn.first.wait_for(state="visible", timeout=timeout)
                await btn.first.click()
                return True
            except PWTimeout:
                continue
    return False


async def stop_lab(page, lab_name):
    """Stop করে। সফল হলে True।"""
    await ensure_dashboard(page)
    if not await click_btn(page, lab_name, ["Stop"]):
        return False
    await page.wait_for_timeout(1500)
    for label in ["Confirm", "Yes", "OK", "Stop"]:
        popup = page.locator('[role="dialog"], .modal, [class*="modal"]').get_by_role(
            "button", name=label
        )
        try:
            await popup.first.click(timeout=3000)
            break
        except PWTimeout:
            continue
    await page.wait_for_timeout(3000)
    await page.reload(wait_until="domcontentloaded")
    return await page.get_by_text("Started", exact=True).count() == 0


async def start_lab(page, lab_name):
    await ensure_dashboard(page)
    return await click_btn(page, lab_name, ["Start", "Restart", "Run"])


# ==========================================
# 🎛 Runner: Playwright ON/OFF নিয়ন্ত্রণ
# ==========================================
class Runner:
    def __init__(self, app):
        self.app = app
        self.task = None
        self.phase = "বন্ধ"
        self.next_at = None
        self.cycles = 0
        self.last_event = "-"

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def notify(self, text):
        self.last_event = f"{datetime.now():%H:%M} {text}"
        log(text)
        if OWNER_ID:
            try:
                await self.app.bot.send_message(OWNER_ID, text, parse_mode=ParseMode.HTML)
            except Exception as e:
                log(f"নোটিফিকেশন পাঠানো যায়নি: {e}")

    async def start(self):
        if self.running:
            return
        set_config("AUTO_ENABLED", "True")
        self.cycles = 0
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        set_config("AUTO_ENABLED", "False")
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.phase = "বন্ধ"
        self.next_at = None

    async def _wait(self, minutes, phase):
        self.phase = phase
        self.next_at = datetime.now() + timedelta(minutes=minutes)
        log(f"⏳ {phase}: {minutes} মিনিট (শেষ {self.next_at:%H:%M:%S})")
        await asyncio.sleep(minutes * 60)

    async def _run(self):
        try:
            cookies = load_cookies()
            if not cookies:
                await self.notify("❌ কুকি খালি। আগে কুকি সেট করুন, তারপর চালু করুন।")
                set_config("AUTO_ENABLED", "False")
                return

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = await browser.new_context()
                await context.add_cookies(cookies)
                page = await context.new_page()
                page.on("dialog", lambda d: asyncio.create_task(d.accept()))

                self.phase = "Dashboard-এ ঢুকছি"
                await ensure_dashboard(page)
                await self.notify("🟢 <b>অটোমেশন চালু হয়েছে</b>, Dashboard-এ ঢুকেছি ✅")

                if get_config("START_WITH_STOP") != "True":
                    await self._wait(int(get_config("MAIN_TIME_MIN")), "কাঙ্ক্ষিত টাইম")

                while True:
                    lab = get_config("LAB_NAME")
                    restart_wait = int(get_config("RESTART_WAIT_MIN"))
                    extra = int(get_config("EXTRA_TIME_MIN"))
                    main = int(get_config("MAIN_TIME_MIN"))

                    try:
                        self.phase = "Stop করছি"
                        ok = await stop_lab(page, lab)
                        await self.notify("🛑 Stop সফল ✅" if ok else "⚠️ Stop নিশ্চিত হয়নি")

                        await self._wait(restart_wait, "Restart-এর আগে বিরতি")

                        self.phase = "Restart করছি"
                        ok = await start_lab(page, lab)
                        self.cycles += 1
                        await self.notify(
                            f"▶️ Restart ক্লিক হয়েছে (চক্র #{self.cycles})"
                            if ok
                            else "⚠️ Start বাটন পাইনি"
                        )
                    except CookieExpired:
                        raise
                    except Exception as e:
                        await self.notify(f"❌ ত্রুটি: {html.escape(str(e))[:200]} (চক্র চলছে)")

                    await self._wait(extra, "অতিরিক্ত টাইম")
                    await self._wait(main, "কাঙ্ক্ষিত টাইম")

        except CookieExpired:
            set_config("AUTO_ENABLED", "False")
            await self.notify("🔑 <b>কুকির মেয়াদ শেষ!</b> নতুন কুকি দিয়ে আবার চালু করুন। অটোমেশন বন্ধ হয়েছে।")
        except asyncio.CancelledError:
            log("🔴 অটোমেশন বন্ধ করা হয়েছে")
            raise
        except Exception as e:
            set_config("AUTO_ENABLED", "False")
            await self.notify(f"❌ <b>অটোমেশন থেমে গেছে:</b> {html.escape(str(e))[:300]}")
        finally:
            self.phase = "বন্ধ"
            self.next_at = None


# ==========================================
# 📱 টেলিগ্রাম বট
# ==========================================
def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if OWNER_ID and uid == OWNER_ID:
            return await fn(update, context)
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⛔ অনুমতি নেই।\nআপনার ID: {uid}\n(মালিক হলে এটাই OWNER_ID হিসেবে সেট করুন)"
            )

    return wrapper


def render_menu(runner: Runner):
    on = runner.running
    cookies = json.loads(get_config("COOKIES_JSON") or "[]")
    cookie_names = ", ".join(c.get("name", "?") for c in cookies) or "নেই"
    nxt = f"{runner.next_at:%H:%M:%S}" if runner.next_at else "-"

    text = (
        "⚙️ <b>Lab Auto Manager</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"অবস্থা: {'🟢 <b>ON</b>' if on else '🔴 <b>OFF</b>'}\n"
        f"ধাপ: {html.escape(runner.phase)}\n"
        f"পরবর্তী ঘটনা: {nxt}\n"
        f"সম্পন্ন চক্র: {runner.cycles}\n"
        f"শেষ ঘটনা: {html.escape(runner.last_event)}\n"
        "━━━━━━━━━━━━━━\n"
        f"🏷 ল্যাব: <code>{html.escape(get_config('LAB_NAME'))}</code>\n"
        f"🛑 শুরুতেই Stop: <b>{'হ্যাঁ' if get_config('START_WITH_STOP') == 'True' else 'না'}</b>\n"
        f"⏱ বিরতি: {get_config('RESTART_WAIT_MIN')} মি | "
        f"➕ অতিরিক্ত: {get_config('EXTRA_TIME_MIN')} মি | "
        f"🎯 কাঙ্ক্ষিত: {get_config('MAIN_TIME_MIN')} মি\n"
        f"🍪 কুকি: {len(cookies)}টি ({html.escape(cookie_names)})"
    )
    toggle = (
        InlineKeyboardButton("⏹ বন্ধ করুন (OFF)", callback_data="toggle")
        if on
        else InlineKeyboardButton("▶️ চালু করুন (ON)", callback_data="toggle")
    )
    sws = "✅" if get_config("START_WITH_STOP") == "True" else "❌"
    kb = InlineKeyboardMarkup(
        [
            [toggle],
            [InlineKeyboardButton("🔄 রিফ্রেশ", callback_data="refresh")],
            [
                InlineKeyboardButton(LABELS["LAB_NAME"], callback_data="edit_LAB_NAME"),
                InlineKeyboardButton(LABELS["COOKIES_JSON"], callback_data="edit_COOKIES_JSON"),
            ],
            [InlineKeyboardButton(f"{LABELS['START_WITH_STOP']} {sws}", callback_data="toggle_sws")],
            [InlineKeyboardButton(LABELS["RESTART_WAIT_MIN"], callback_data="edit_RESTART_WAIT_MIN")],
            [InlineKeyboardButton(LABELS["EXTRA_TIME_MIN"], callback_data="edit_EXTRA_TIME_MIN")],
            [InlineKeyboardButton(LABELS["MAIN_TIME_MIN"], callback_data="edit_MAIN_TIME_MIN")],
        ]
    )
    return text, kb


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"আপনার ইউজার ID: {update.effective_user.id}")


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("editing", None)
    text, kb = render_menu(context.application.bot_data["runner"])
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    runner: Runner = context.application.bot_data["runner"]
    data = query.data

    if data == "toggle":
        if runner.running:
            await runner.stop()
        else:
            await runner.start()
            await asyncio.sleep(1)
    elif data == "toggle_sws":
        cur = get_config("START_WITH_STOP") == "True"
        set_config("START_WITH_STOP", "False" if cur else "True")
    elif data.startswith("edit_"):
        key = data[5:]
        if key not in LABELS:
            return
        context.user_data["editing"] = key
        if key == "COOKIES_JSON":
            prompt = (
                "🍪 Cookie-Editor থেকে <b>Export</b> করা JSON পুরোটা পাঠান "
                "(লেখা হিসেবে অথবা .json ফাইল হিসেবে)।\n"
                "নিরাপত্তার জন্য পাঠানো মেসেজটা আমি মুছে দেব।"
            )
        else:
            prompt = (
                f"📝 <b>{LABELS[key]}</b>\n"
                f"বর্তমান মান: <code>{html.escape(get_config(key))}</code>\n\n"
                "নতুন মান লিখে পাঠান (বাতিল করতে /start):"
            )
        await safe_edit(query, prompt)
        return

    text, kb = render_menu(runner)
    await safe_edit(query, text, kb)


async def _apply_edit(update, context, raw_text):
    key = context.user_data.get("editing")
    try:
        value = parse_value(key, raw_text)
    except ValueError as e:
        await update.effective_message.reply_text(f"⚠️ {e}\nআবার পাঠান, অথবা /start দিয়ে বাতিল করুন।")
        return
    set_config(key, value)
    context.user_data.pop("editing", None)

    # কুকির মেসেজ চ্যাটে রেখে দেওয়া ঠিক নয়
    if key == "COOKIES_JSON":
        try:
            await update.effective_message.delete()
        except Exception:
            pass

    runner: Runner = context.application.bot_data["runner"]
    note = "✅ আপডেট হয়েছে।"
    if runner.running and key in ("COOKIES_JSON", "LAB_NAME"):
        note += " (কুকি/নাম কার্যকর হবে OFF করে আবার ON করলে)"
    elif runner.running:
        note += " (পরের চক্র থেকে কার্যকর)"
    text, kb = render_menu(runner)
    await update.effective_chat.send_message(note + "\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "editing" in context.user_data:
        await _apply_edit(update, context, update.message.text)
    else:
        await update.message.reply_text("মেনু খুলতে /start দিন।")


@owner_only
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("editing") != "COOKIES_JSON":
        await update.message.reply_text("আগে মেনু থেকে 🍪 কুকি বেছে নিন।")
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 200_000:
        await update.message.reply_text("⚠️ ফাইল অনেক বড়।")
        return
    buf = io.BytesIO()
    f = await doc.get_file()
    await f.download_to_memory(buf)
    await _apply_edit(update, context, buf.getvalue().decode("utf-8", errors="ignore"))


# ==========================================
# 🚀 মেইন
# ==========================================
async def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN সেট করা নেই। export TELEGRAM_TOKEN=... দিন।")
        return
    if not OWNER_ID:
        print("⚠️ OWNER_ID সেট করা নেই। বটে /id লিখে আপনার ID জেনে export OWNER_ID=... দিন।")

    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    runner = Runner(app)
    app.bot_data["runner"] = runner

    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log("🤖 বট চালু হয়েছে (অটোমেশন ডিফল্টে OFF)")

    # সার্ভার রিস্টার্টের পর আগের ON অবস্থা ফিরিয়ে আনা
    if get_config("AUTO_ENABLED") == "True":
        await runner.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

    log("বন্ধ হচ্ছে...")
    if runner.running:
        runner.task.cancel()
        try:
            await runner.task
        except (asyncio.CancelledError, Exception):
            pass
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

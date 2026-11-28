"""
Full Telegram AI Bot
--------------------
পাঠাতে পারবেন (Receive): টেক্সট, ছবি, ফাইল (txt/py/csv/json/pdf/docx/xlsx/zip),
                          ভয়েস/অডিও/ভিডিও (ট্রান্সক্রিপশন), স্টিকার, লোকেশন, কন্টাক্ট
বট পাঠাবে (Send): টেক্সট, ফাইল (ক্যাপশন সহ), ZIP, ছবি (URL/AI), লোকেশন, কন্টাক্ট,
                   পোল, ডাইস/ইমোজি গেম, অডিও/ভয়েস (TTS), ভিডিও/অডিও/ডকুমেন্ট (URL থেকে)

ইনস্টল:
    pip install python-telegram-bot openai pypdf python-docx openpyxl gTTS

নিচের "কনফিগ" অংশে TG_TOKEN আর FM_KEY বসিয়ে চালান:
    python aibot.py
"""

import os
import io
import re
import html
import json
import base64
import asyncio
import logging
import csv
import zipfile
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

from openai import AsyncOpenAI
from pypdf import PdfReader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

# ---------------- কনফিগ ----------------
TG_TOKEN = "9097454535:AAFmnesxWwWoKsp5CXvoByIVMCnzamOlunE"

# --- চ্যাট (freemodel.in) ---
FM_KEY = "fm_ba668d410047482b3642d129293d71a39796aefaf48acecc"
BASE_URL = "https://freemodel.in/v1"
CHAT_MODELS = [
    "claude-haiku-5.5",
    "gpt-6.2-astra",
    "gpt-6-luna",
    "grok-4.6",
    "grok-4.7",
    "deepseek-v4.1-flash-1017",
    "qwen3.8-omni-flash",
    "glm-5.3-flash",
]
MODEL = CHAT_MODELS[0]          # শুরুর ডিফল্ট মডেল

# --- ছবি বানানো (ecohash.com) ---
ECO_KEY = "eco_ouRKQUqbL5rdYlC6_YvUaTUEAqT0GzSq"      # eco_... দিয়ে শুরু
ECO_BASE_URL = "https://api.ecohash.com/v1"
IMAGE_MODELS = ["z-image-turbo", "qwen-image"]   # qwen-image-এর সঠিক নাম ecohash-এর কোড স্যাম্পলে দেখে নিন
IMAGE_MODEL = IMAGE_MODELS[0]

# --- ভয়েস/অডিও থেকে লেখা বের করা (সার্ভার সাপোর্ট করলে) ---
STT_MODEL = "whisper-1"

# --- বটের পরিচয় (কেউ নাম/স্রষ্টা জিজ্ঞেস করলে এটাই বলবে) ---
BOT_NAME = "Shahed AI"
OWNER_INFO = "মোঃ শাহেদ (শাহেদ ভাই) এই বটটি বানিয়েছেন।"

# শুধু নিজে ব্যবহার করতে চাইলে এখানে আপনার Telegram user id দিন (/myid দিলে পাবেন)।
# ফাঁকা রাখলে সবাই ব্যবহার করতে পারবে। যেমন: ALLOWED = {123456789}
ALLOWED = {8853172010}

# --- টোকেন লিমিট ---
USER_LIMIT = 200_000            # প্রতি ইউজারের লিমিট (পাঠানো + পাওয়া মিলিয়ে)
LIMIT_PERIOD = "monthly"        # "daily" = প্রতিদিন রিসেট, "monthly" = প্রতি মাসে রিসেট
GLOBAL_LIMIT = 7_000_000        # সব ইউজার মিলিয়ে মোট সীমা (কখনো রিসেট হয় না); 0 = সীমা নেই
ADMINS = {8853172010}              # আপনার user id দিন: এদের কোনো লিমিট নেই। যেমন: ADMINS = {123456789}
LOG_FILE = "usage_log.csv"         # প্রতিটা রিকোয়েস্টের বিস্তারিত লগ (ইউজার + মডেল সহ)
USAGE_FILE = "usage.json"       # হিসাব এই ফাইলে সেভ হয়, রিস্টার্টে হারায় না
TZ = timezone(timedelta(hours=6))   # বাংলাদেশ সময়

TG_LIMIT = 4000
CAPTION_LIMIT = 1024
MAX_FILE_MB = 20            # বট API দিয়ে ডাউনলোডের সীমা ২০MB
MAX_FILE_CHARS = 30000

client = AsyncOpenAI(base_url=BASE_URL, api_key=FM_KEY)
eco_client = AsyncOpenAI(base_url=ECO_BASE_URL, api_key=ECO_KEY)
history = defaultdict(lambda: deque(maxlen=20))
user_model: dict[int, str] = {}      # প্রতি ইউজারের বাছা চ্যাট মডেল
user_img: dict[int, str] = {}        # প্রতি ইউজারের বাছা ছবির মডেল

# ---------------- টোকেন ব্যবহারের হিসাব ----------------
class LimitError(Exception):
    pass


def period_key() -> str:
    now = datetime.now(TZ)
    return now.strftime("%Y-%m-%d") if LIMIT_PERIOD == "daily" else now.strftime("%Y-%m")


def load_usage() -> dict:
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"period": period_key(), "users": {}, "global_total": 0}


usage = load_usage()


def save_usage():
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False)
    except Exception:
        logging.exception("usage save error")


def roll_period():
    if usage.get("period") != period_key():
        usage["period"] = period_key()
        usage["users"] = {}
        save_usage()


def used_by(uid: int) -> int:
    roll_period()
    return usage["users"].get(str(uid), 0)


def check_limit(uid: int):
    if uid in ADMINS:
        return
    if GLOBAL_LIMIT and usage.get("global_total", 0) >= GLOBAL_LIMIT:
        raise LimitError("বটের মোট টোকেন সীমা শেষ হয়ে গেছে। অ্যাডমিনের সঙ্গে যোগাযোগ করুন।")
    if used_by(uid) >= USER_LIMIT:
        when = "আগামীকাল" if LIMIT_PERIOD == "daily" else "পরের মাসের ১ তারিখে"
        raise LimitError(
            f"আপনার টোকেন লিমিট ({USER_LIMIT:,}) শেষ হয়ে গেছে। {when} আবার ব্যবহার করতে পারবেন।"
        )


def add_usage(uid: int, tokens: int):
    roll_period()
    usage["users"][str(uid)] = usage["users"].get(str(uid), 0) + tokens
    usage["global_total"] = usage.get("global_total", 0) + tokens
    save_usage()


LOG_HEADER = ["time", "user_id", "username", "name", "type", "model",
              "input_tokens", "output_tokens", "total_tokens"]


def log_row(user, kind: str, model: str, tin: int, tout: int):
    """প্রতিটা চ্যাট/ছবি রিকোয়েস্ট CSV-তে জমা রাখে।"""
    try:
        new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_HEADER)
            w.writerow([
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), user.id,
                user.username or "", user.full_name or "", kind, model,
                tin, tout, tin + tout,
            ])
    except Exception:
        logging.exception("log error")


def read_log() -> list[dict]:
    try:
        with open(LOG_FILE, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


TOOLS_PROMPT = """তুমি একটি Telegram বটের ভেতরের শক্তিশালী AI সহকারী। ব্যবহারকারী যে ভাষায় লিখবে সেই ভাষায় উত্তর দাও।
তুমি সাধারণ টেক্সটের পাশাপাশি নিচের ট্যাগ ব্যবহার করে বটকে দিয়ে জিনিস পাঠাতে পারো।
ট্যাগের বাইরের লেখা সাধারণ মেসেজ হিসেবে যাবে। ব্যবহারকারী কিছু (ফাইল/zip/ছবি/ভয়েস/লোকেশন ইত্যাদি) চাইলে অবশ্যই ট্যাগ ব্যবহার করো। কখনো বোলো না যে তুমি ফাইল পাঠাতে পারো না।

১) ফাইল (যেকোনো টেক্সট-ভিত্তিক: py, txt, html, csv, json, svg, md ইত্যাদি):
<file name="hello.py" caption="ছোট বর্ণনা">
ফাইলের পুরো কনটেন্ট
</file>

২) ZIP (একাধিক ফাইল):
<zip name="project.zip" caption="বর্ণনা">
<file name="a.py">...</file>
<file name="docs/b.txt">...</file>
</zip>

৩) ছবি URL থেকে: <photo url="https://..." caption="..."/>
৪) AI দিয়ে ছবি বানানো: <image prompt="ইংরেজিতে বিস্তারিত বর্ণনা" caption="..."/>
৫) লোকেশন: <location lat="23.81" lon="90.41"/>
৬) কন্টাক্ট: <contact phone="+8801XXXXXXXXX" name="নাম"/>
৭) পোল: <poll question="প্রশ্ন" options="অপশন১|অপশন২|অপশন৩"/>
৮) ডাইস/গেম: <dice emoji="🎲"/>  (🎲 🎯 🏀 ⚽ 🎳 🎰)
৯) কথা বলা (ভয়েস/অডিও): <voice lang="bn">যা বলতে হবে</voice>
১০) URL থেকে ভিডিও/অডিও/ডকুমেন্ট: <video url="..." caption="..."/>, <audio url="..." caption="..."/>, <document url="..." caption="..."/>

নিয়ম: ফাইলের কনটেন্ট ``` দিয়ে মুড়ে দিও না। ফাইলে আসল কাজের কনটেন্ট দাও। আসল ফটো (jpg/png) ফাইল হিসেবে লিখে দেওয়া যায় না; ছবি লাগলে <image/> বা <photo/> ব্যবহার করো, অথবা SVG ফাইল বানাও।"""


def system_prompt(uid: int) -> dict:
    model = user_model.get(uid, MODEL)
    ident = (
        f"তোমার নাম {BOT_NAME}। {OWNER_INFO}\n"
        f"এই বট এখন \"{model}\" মডেল দিয়ে চলছে (ব্যবহারকারী /model দিয়ে মডেল বদলাতে পারে)। "
        "কেউ তোমার নাম, স্রষ্টা বা মডেল জিজ্ঞেস করলে শুধু এই তথ্য স্পষ্ট ও বন্ধুত্বপূর্ণভাবে বলো। "
        "এর বাইরে স্রষ্টার ব্যক্তিগত তথ্য (পরিবার, পেশা, ঠিকানা ইত্যাদি) তুমি জানো না, অনুমান করে বানিয়ে বলবে না।\n\n"
    )
    return {"role": "system", "content": ident + TOOLS_PROMPT}


# ---------------- টেক্সট পাঠানো ----------------
def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def md_to_html(t: str) -> str:
    parts = re.split(r"(```[\w+-]*\n?.*?```)", t, flags=re.S)
    out = []
    for p in parts:
        if p.startswith("```") and p.endswith("```") and len(p) >= 6:
            code = re.sub(r"^```[\w+-]*\n?", "", p)[:-3]
            out.append(f"<pre>{html.escape(code.strip())}</pre>")
        else:
            p = html.escape(p)
            p = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", p)
            p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
            p = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", p, flags=re.M)
            out.append(p)
    return "".join(out)


async def send_text(update: Update, text: str):
    text = text.strip()
    if not text:
        return
    for part in split_text(text):
        try:
            await update.message.reply_text(md_to_html(part), parse_mode=ParseMode.HTML)
        except BadRequest:
            await update.message.reply_text(part)


async def send_caption_rest(update: Update, caption: str) -> str:
    """ক্যাপশন ১০২৪-এর বেশি হলে বাকিটা আলাদা মেসেজে পাঠানোর জন্য ফেরত দেয়।"""
    if len(caption) > CAPTION_LIMIT:
        return caption[:CAPTION_LIMIT], caption[CAPTION_LIMIT:]
    return caption, ""


# ---------------- ট্যাগ পার্সার ও ডেলিভারি ----------------
TAG_RE = re.compile(
    r"<zip\b([^>]*)>(.*?)</zip>"
    r"|<file\b([^>]*)>(.*?)</file>"
    r"|<(photo|image|location|contact|poll|dice|audio|video|document)\b([^>]*?)/>"
    r"|<voice\b([^>]*)>(.*?)</voice>",
    re.S,
)


def attrs(s: str) -> dict:
    return {k: html.unescape(v) for k, v in re.findall(r'(\w+)="(.*?)"', s or "", re.S)}


def strip_fence(c: str) -> str:
    c = c.strip("\n")
    m = re.match(r"^```[\w+-]*\n(.*?)\n?```\s*$", c, re.S)
    return m.group(1) if m else c


def safe_name(name: str, default: str) -> str:
    name = (name or default).replace("\\", "/").strip("/")
    name = re.sub(r"\.\.+/?", "", name)
    return name or default


async def send_file_bytes(update, data: bytes, name: str, caption: str):
    cap, rest = await send_caption_rest(update, caption)
    await update.message.chat.send_action("upload_document")
    await update.message.reply_document(
        document=io.BytesIO(data), filename=os.path.basename(name), caption=cap or None
    )
    if rest:
        await send_text(update, rest)


async def run_tag(update: Update, m: re.Match):
    msg = update.message
    chat = msg.chat

    # ZIP
    if m.group(2) is not None:
        a = attrs(m.group(1))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fm in re.finditer(r"<file\b([^>]*)>(.*?)</file>", m.group(2), re.S):
                fa = attrs(fm.group(1))
                z.writestr(safe_name(fa.get("name"), "file.txt"), strip_fence(fm.group(2)))
        name = safe_name(a.get("name"), "files.zip")
        if not name.lower().endswith(".zip"):
            name += ".zip"
        await send_file_bytes(update, buf.getvalue(), name, a.get("caption", ""))
        return

    # FILE
    if m.group(4) is not None:
        a = attrs(m.group(3))
        name = safe_name(a.get("name"), "file.txt")
        await send_file_bytes(update, strip_fence(m.group(4)).encode("utf-8"), name, a.get("caption", ""))
        return

    # VOICE / TTS
    if m.group(8) is not None:
        a = attrs(m.group(7))
        text = m.group(8).strip()
        try:
            from gtts import gTTS

            def make():
                b = io.BytesIO()
                gTTS(text=text, lang=a.get("lang", "bn")).write_to_fp(b)
                return b.getvalue()

            await chat.send_action("record_voice")
            data = await asyncio.to_thread(make)
            await msg.reply_audio(audio=io.BytesIO(data), filename="voice.mp3",
                                  caption=text[:CAPTION_LIMIT])
        except Exception as e:
            await msg.reply_text(f"ভয়েস বানানো যায়নি (pip install gTTS লাগবে): {e}")
        return

    # সেল্ফ-ক্লোজিং ট্যাগ
    kind = m.group(5)
    a = attrs(m.group(6))
    cap = a.get("caption", "")[:CAPTION_LIMIT] or None

    if kind == "photo":
        await chat.send_action("upload_photo")
        await msg.reply_photo(photo=a["url"], caption=cap)
    elif kind == "video":
        await chat.send_action("upload_video")
        await msg.reply_video(video=a["url"], caption=cap)
    elif kind == "audio":
        await chat.send_action("upload_voice")
        await msg.reply_audio(audio=a["url"], caption=cap)
    elif kind == "document":
        await chat.send_action("upload_document")
        await msg.reply_document(document=a["url"], caption=cap)
    elif kind == "location":
        await msg.reply_location(latitude=float(a["lat"]), longitude=float(a["lon"]))
    elif kind == "contact":
        await msg.reply_contact(phone_number=a["phone"], first_name=a.get("name", "Contact"))
    elif kind == "poll":
        opts = [o.strip() for o in a.get("options", "").split("|") if o.strip()][:10]
        await msg.reply_poll(question=a["question"][:300], options=opts)
    elif kind == "dice":
        await msg.reply_dice(emoji=a.get("emoji", "🎲"))
    elif kind == "image":
        await chat.send_action("upload_photo")
        uid = update.effective_user.id
        img_model = user_img.get(uid, IMAGE_MODEL)
        try:
            extra = {"num_inference_steps": 8} if "turbo" in img_model else {}
            r = await eco_client.images.generate(
                model=img_model, prompt=a["prompt"], size="1024x1024", extra_body=extra
            )
            item = r.data[0]
            log_row(update.effective_user, "image", img_model, 0, 0)
            if getattr(item, "b64_json", None):
                await msg.reply_photo(photo=io.BytesIO(base64.b64decode(item.b64_json)), caption=cap)
            else:
                await msg.reply_photo(photo=item.url, caption=cap)
        except Exception as e:
            await msg.reply_text(f"ছবি বানানো যায়নি ({img_model}): {e}")


async def deliver(update: Update, answer: str):
    if not answer.strip():
        await update.message.reply_text("(খালি উত্তর এসেছে)")
        return
    pos = 0
    for m in TAG_RE.finditer(answer):
        await send_text(update, answer[pos:m.start()])
        pos = m.end()
        try:
            await run_tag(update, m)
        except Exception as e:
            logging.exception("tag error")
            await update.message.reply_text(f"পাঠানো যায়নি: {e}")
    await send_text(update, answer[pos:])


# ---------------- মডেল কল ----------------
async def ask_model(update: Update, content, hist_text: str | None = None) -> str:
    """hist_text দিলে সেটাই ইতিহাসে জমা হবে (ছবি/ফাইলের ক্ষেত্রে সংক্ষিপ্ত রূপ)।"""
    uid = update.effective_user.id
    await update.message.chat.send_action("typing")

    check_limit(uid)
    messages = [system_prompt(uid), *history[uid], {"role": "user", "content": content}]
    r = await client.chat.completions.create(model=user_model.get(uid, MODEL), messages=messages)
    answer = r.choices[0].message.content or ""
    tokens = getattr(getattr(r, "usage", None), "total_tokens", None)
    if tokens is None:   # সার্ভার usage না দিলে আন্দাজ
        tokens = (len(answer) + sum(len(str(m["content"])) for m in messages)) // 3
    add_usage(uid, int(tokens))
    u = getattr(r, "usage", None)
    tin = getattr(u, "prompt_tokens", None)
    tout = getattr(u, "completion_tokens", None)
    if tin is None or tout is None:
        tin, tout = int(tokens), 0
    log_row(update.effective_user, "chat", user_model.get(uid, MODEL), int(tin), int(tout))

    history[uid].append({"role": "user", "content": hist_text if hist_text is not None else content})
    history[uid].append({"role": "assistant", "content": answer})
    return answer


async def guarded(update: Update, coro_fn, err_prefix: str):
    if ALLOWED and update.effective_user.id not in ALLOWED:
        await update.message.reply_text("দুঃখিত, আপনার অনুমতি নেই।")
        return
    try:
        await coro_fn()
    except LimitError as e:
        await update.message.reply_text(f"⛔ {e}")
    except Exception as e:
        logging.exception(err_prefix)
        await update.message.reply_text(f"{err_prefix}: {e}")


# ---------------- ফাইল থেকে টেক্সট বের করা ----------------
def extract_text(data: bytes, name: str) -> str:
    n = name.lower()
    if n.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    if n.endswith(".docx"):
        from docx import Document
        d = Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    if n.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                lines.append("\t".join("" if c is None else str(c) for c in row))
        return "\n".join(lines)
    if n.endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(data))
        lines = ["ZIP-এর ভেতরের ফাইল:"]
        for info in z.infolist():
            lines.append(f"- {info.filename} ({info.file_size} bytes)")
        for info in z.infolist()[:15]:
            if info.is_dir() or info.file_size > 200_000:
                continue
            try:
                txt = z.read(info).decode("utf-8")
                lines.append(f"\n--- {info.filename} ---\n{txt[:4000]}")
            except Exception:
                pass
        return "\n".join(lines)
    return data.decode("utf-8", errors="ignore")


# ---------------- কমান্ড ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "স্বাগতম! আমাকে পাঠাতে পারেন:\n"
        "• টেক্সট, ছবি, ফাইল (txt/py/csv/json/pdf/docx/xlsx/zip)\n"
        "• ভয়েস, অডিও, ভিডিও, স্টিকার, লোকেশন, কন্টাক্ট\n\n"
        "আমি পাঠাতে পারি: ফাইল (ক্যাপশন সহ), zip, ছবি, ভয়েস, লোকেশন, কন্টাক্ট, পোল, গেম।\n"
        "যেমন: \"Md Shahed নামে zip দাও, ভেতরে hello.py আর notes.txt\"\n\n"
        "/model — চ্যাট মডেল বদলান\n"
        "/imgmodel — ছবির মডেল বদলান\n"
        "/usage — টোকেন ব্যবহার দেখুন\n"
        "/clear — ইতিহাস মুছুন"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history[update.effective_user.id].clear()
    await update.message.reply_text("ইতিহাস মুছে ফেলা হয়েছে।")


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = user_model.get(update.effective_user.id, MODEL)
    kb = [[InlineKeyboardButton(("✅ " if m == cur else "") + m, callback_data=f"m:{m}")]
          for m in CHAT_MODELS]
    await update.message.reply_text(f"চ্যাট মডেল বাছুন (এখন: {cur})",
                                    reply_markup=InlineKeyboardMarkup(kb))


async def imgmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = user_img.get(update.effective_user.id, IMAGE_MODEL)
    kb = [[InlineKeyboardButton(("✅ " if m == cur else "") + m, callback_data=f"i:{m}")]
          for m in IMAGE_MODELS]
    await update.message.reply_text(f"ছবির মডেল বাছুন (এখন: {cur})",
                                    reply_markup=InlineKeyboardMarkup(kb))


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if ALLOWED and uid not in ALLOWED:
        return
    kind, _, name = q.data.partition(":")
    if kind == "m" and name in CHAT_MODELS:
        user_model[uid] = name
        history[uid].clear()   # মডেল বদলালে পুরনো ইতিহাস মুছে যায়, যাতে গোলমাল না হয়
        await q.edit_message_text(f"✅ চ্যাট মডেল: {name}\n(নতুন কথোপকথন শুরু হলো)")
    elif kind == "i" and name in IMAGE_MODELS:
        user_img[uid] = name
        await q.edit_message_text(f"✅ ছবির মডেল: {name}")


async def usage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMINS:
        await update.message.reply_text(
            f"আপনি অ্যাডমিন, আপনার কোনো লিমিট নেই।\n"
            f"সব ইউজার মিলিয়ে মোট ব্যবহার: {usage.get('global_total', 0):,} / {GLOBAL_LIMIT:,}"
        )
        return
    used = used_by(uid)
    period = "আজ" if LIMIT_PERIOD == "daily" else "এই মাসে"
    await update.message.reply_text(
        f"{period} ব্যবহার: {used:,} / {USER_LIMIT:,} টোকেন\n"
        f"বাকি: {max(USER_LIMIT - used, 0):,} টোকেন"
    )


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("এই কমান্ড শুধু অ্যাডমিনের জন্য।")
        return
    if not os.path.exists(LOG_FILE):
        await update.message.reply_text("এখনো কোনো ডেটা জমা হয়নি।")
        return
    with open(LOG_FILE, "rb") as f:
        data = f.read()
    await update.message.reply_document(
        document=io.BytesIO(data), filename="usage_log.csv",
        caption="সব রিকোয়েস্টের লগ (Excel-এ খুলুন)।"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("এই কমান্ড শুধু অ্যাডমিনের জন্য।")
        return
    rows = read_log()
    if not rows:
        await update.message.reply_text("এখনো কোনো ডেটা জমা হয়নি।")
        return
    by_model, by_user = {}, {}
    for r in rows:
        t = int(r["total_tokens"] or 0)
        m = by_model.setdefault(r["model"], [0, 0])
        m[0] += 1
        m[1] += t
        key = f'{r["name"]} ({r["user_id"]})'
        u = by_user.setdefault(key, [0, 0])
        u[0] += 1
        u[1] += t
    lines = [f"মোট রিকোয়েস্ট: {len(rows)}", "", "মডেল অনুযায়ী:"]
    for k, (n, t) in sorted(by_model.items(), key=lambda x: -x[1][1]):
        lines.append(f"• {k}: {n} বার, {t:,} টোকেন")
    lines += ["", "ইউজার অনুযায়ী (শীর্ষ ১০):"]
    for k, (n, t) in sorted(by_user.items(), key=lambda x: -x[1][1])[:10]:
        lines.append(f"• {k}: {n} বার, {t:,} টোকেন")
    await update.message.reply_text("\n".join(lines))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"আপনার user id: {update.effective_user.id}")


# ---------------- হ্যান্ডলার ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def run():
        await deliver(update, await ask_model(update, update.message.text))
    await guarded(update, run, "ত্রুটি হয়েছে")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def run():
        file = await update.message.photo[-1].get_file()
        b64 = base64.b64encode(bytes(await file.download_as_bytearray())).decode()
        q = update.message.caption or "এই ছবিতে কী আছে, বিস্তারিত বর্ণনা করো।"
        content = [
            {"type": "text", "text": q},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        await deliver(update, await ask_model(update, content, hist_text=f"[ছবি পাঠানো হয়েছে] {q}"))
    await guarded(update, run, "ছবি প্রসেস করা যায়নি")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def run():
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
            await update.message.reply_text(f"ফাইল {MAX_FILE_MB}MB-এর বেশি হতে পারবে না।")
            return
        file = await doc.get_file()
        data = bytes(await file.download_as_bytearray())

        # ছবি ডকুমেন্ট হিসেবে এলে ভিশনে পাঠাই
        if (doc.mime_type or "").startswith("image/"):
            b64 = base64.b64encode(data).decode()
            q = update.message.caption or "এই ছবিতে কী আছে বর্ণনা করো।"
            content = [
                {"type": "text", "text": q},
                {"type": "image_url", "image_url": {"url": f"data:{doc.mime_type};base64,{b64}"}},
            ]
            await deliver(update, await ask_model(update, content, hist_text=f"[ছবি ফাইল] {q}"))
            return

        text = extract_text(data, doc.file_name or "")
        if not text.strip():
            await update.message.reply_text("ফাইল থেকে কোনো টেক্সট পাওয়া যায়নি।")
            return
        text = text[:MAX_FILE_CHARS]
        task = update.message.caption or "এই ফাইলের সারাংশ দাও।"
        prompt = f"{task}\n\n--- ফাইলের বিষয়বস্তু ({doc.file_name}) ---\n{text}"
        await deliver(update, await ask_model(update, prompt, hist_text=prompt[:4000]))
    await guarded(update, run, "ফাইল প্রসেস করা যায়নি")


async def handle_audio_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def run():
        m = update.message
        media = m.voice or m.audio or m.video_note or m.video
        if media.file_size and media.file_size > MAX_FILE_MB * 1024 * 1024:
            await m.reply_text(f"ফাইল {MAX_FILE_MB}MB-এর বেশি হতে পারবে না।")
            return
        file = await media.get_file()
        data = bytes(await file.download_as_bytearray())
        ext = os.path.splitext(file.file_path or "")[1] or ".ogg"
        await m.chat.send_action("typing")
        try:
            r = await client.audio.transcriptions.create(model=STT_MODEL, file=(f"media{ext}", data))
            transcript = r.text
        except Exception as e:
            await m.reply_text(f"অডিও/ভিডিও থেকে লেখা বের করা যায়নি (সার্ভার সাপোর্ট নাও করতে পারে): {e}")
            return
        task = m.caption or ""
        prompt = f"[ভয়েস/অডিও/ভিডিওর ট্রান্সক্রিপ্ট]: {transcript}\n{task}".strip()
        await deliver(update, await ask_model(update, prompt))
    await guarded(update, run, "মিডিয়া প্রসেস করা যায়নি")


async def handle_misc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def run():
        m = update.message
        if m.sticker:
            prompt = f"[স্টিকার: {m.sticker.emoji or ''}] ব্যবহারকারী এই স্টিকার পাঠিয়েছে, মজার করে উত্তর দাও।"
        elif m.location:
            prompt = f"[লোকেশন] latitude={m.location.latitude}, longitude={m.location.longitude}। এই জায়গা সম্পর্কে বলো।"
        elif m.contact:
            c = m.contact
            prompt = f"[কন্টাক্ট] নাম: {c.first_name} {c.last_name or ''}, ফোন: {c.phone_number}"
        elif m.poll:
            prompt = f"[পোল] {m.poll.question}: " + ", ".join(o.text for o in m.poll.options)
        else:
            return
        await deliver(update, await ask_model(update, prompt))
    await guarded(update, run, "ত্রুটি হয়েছে")


def main():
    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("usage", usage_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("imgmodel", imgmodel_cmd))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^[mi]:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE, handle_audio_video))
    app.add_handler(MessageHandler(
        filters.Sticker.ALL | filters.LOCATION | filters.CONTACT | filters.POLL, handle_misc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot চালু হয়েছে...")
    app.run_polling()


if __name__ == "__main__":
    main()
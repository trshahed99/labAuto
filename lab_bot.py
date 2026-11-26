"""
Lab Auto Manager (Web)
----------------------
টেলিগ্রামের বদলে ওয়েব ড্যাশবোর্ড। Cloudflare Tunnel দিয়ে নিজের ডোমেইনে খোলা যায়।

- Playwright ON/OFF সুইচ (ডিফল্ট: OFF)
- পাসওয়ার্ড দিয়ে লগইন (DASH_PASSWORD)
- সেটিং, কুকি, লাইভ স্ট্যাটাস ও লগ ওয়েবপেজ থেকেই
- সার্ভার শুধু 127.0.0.1-এ শোনে, বাইরে থেকে ঢোকার একমাত্র পথ Tunnel

চালানোর আগে:
    pip install -r requirements.txt
    playwright install --with-deps chromium
    export DASH_PASSWORD="একটা শক্ত পাসওয়ার্ড"
    python3 lab_web.py
"""

import asyncio
import collections
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# ==========================================
# ⚙️ বেসিক কনফিগ
# ==========================================
DASH_PASSWORD = os.getenv("DASH_PASSWORD", "742422")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DB_NAME = os.getenv("DB_NAME", "lab_config.db")
DASH_URL = "https://labs.2z2.top/dashboard"
SESSION_TTL = 12 * 3600

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
EDITABLE = {"LAB_NAME", "START_WITH_STOP", *INT_KEYS}


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


def parse_value(key, value):
    """মান যাচাই করে। ভুল হলে ValueError দেয়।"""
    t = str(value).strip()
    if key in INT_KEYS:
        if not t.isdigit() or not (1 <= int(t) <= 1440):
            raise ValueError("সময় ১ থেকে ১৪৪০ মিনিটের মধ্যে একটি সংখ্যা হতে হবে।")
        return str(int(t))
    if key == "LAB_NAME":
        if not t or len(t) > 100:
            raise ValueError("ল্যাবের নাম খালি বা অতি দীর্ঘ হতে পারে না।")
        return t
    if key == "START_WITH_STOP":
        return "True" if t.lower() in ("true", "1", "yes", "on") else "False"
    if key == "COOKIES_JSON":
        if len(t) > 200_000:
            raise ValueError("কুকি ডাটা অনেক বড়।")
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            raise ValueError("এটা সঠিক JSON নয়। Cookie-Editor থেকে Export করে পুরোটা দিন।")
        if not isinstance(data, list) or not data:
            raise ValueError("কুকির তালিকা ([ ... ]) দরকার।")
        for c in data:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                raise ValueError("প্রতিটি কুকিতে name আর value থাকতে হবে।")
        return json.dumps(data, ensure_ascii=False)
    raise ValueError("অজানা সেটিং।")


# ==========================================
# 🤖 Playwright অটোমেশন
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


class Runner:
    def __init__(self):
        self.task = None
        self.phase = "বন্ধ"
        self.next_at = None
        self.cycles = 0
        self.last_event = "-"
        self.logs = collections.deque(maxlen=200)

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def note(self, text):
        line = f"{datetime.now():%H:%M:%S}  {text}"
        self.logs.append(line)
        self.last_event = line
        log(text)

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
        self.note("🔴 অটোমেশন বন্ধ করা হয়েছে")

    async def _wait(self, minutes, phase):
        self.phase = phase
        self.next_at = datetime.now() + timedelta(minutes=minutes)
        self.note(f"⏳ {phase}: {minutes} মিনিট (শেষ {self.next_at:%H:%M:%S})")
        await asyncio.sleep(minutes * 60)

    async def _run(self):
        try:
            cookies = load_cookies()
            if not cookies:
                self.note("❌ কুকি খালি। আগে কুকি সেট করুন, তারপর চালু করুন।")
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
                self.note("🟢 অটোমেশন চালু হয়েছে, Dashboard-এ ঢুকেছি ✅")

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
                        self.note("🛑 Stop সফল ✅" if ok else "⚠️ Stop নিশ্চিত হয়নি")

                        await self._wait(restart_wait, "Restart-এর আগে বিরতি")

                        self.phase = "Restart করছি"
                        ok = await start_lab(page, lab)
                        self.cycles += 1
                        self.note(
                            f"▶️ Restart ক্লিক হয়েছে (চক্র #{self.cycles})"
                            if ok
                            else "⚠️ Start বাটন পাইনি"
                        )
                    except CookieExpired:
                        raise
                    except Exception as e:
                        self.note(f"❌ ত্রুটি: {str(e)[:200]} (চক্র চলছে)")

                    await self._wait(extra, "অতিরিক্ত টাইম")
                    await self._wait(main, "কাঙ্ক্ষিত টাইম")

        except CookieExpired:
            set_config("AUTO_ENABLED", "False")
            self.note("🔑 কুকির মেয়াদ শেষ! নতুন কুকি দিয়ে আবার চালু করুন। অটোমেশন বন্ধ হয়েছে।")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            set_config("AUTO_ENABLED", "False")
            self.note(f"❌ অটোমেশন থেমে গেছে: {str(e)[:300]}")
        finally:
            self.phase = "বন্ধ"
            self.next_at = None


runner = Runner()

# ==========================================
# 🔐 লগইন ও সেশন
# ==========================================
sessions = {}  # token -> expiry
fails = {}  # ip -> [count, first_time]


def client_ip(request: Request):
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def is_authed(request: Request):
    token = request.cookies.get("lab_session")
    exp = sessions.get(token)
    if exp and exp > time.time():
        return True
    sessions.pop(token, None)
    return False


def require_auth(request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="লগইন করুন")
    if request.method == "POST" and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(status_code=403, detail="অনুরোধ বাতিল")


# ==========================================
# 🌐 ওয়েব অ্যাপ
# ==========================================
@asynccontextmanager
async def lifespan(app):
    init_db()
    if get_config("AUTO_ENABLED") == "True":
        await runner.start()
    yield
    if runner.running:
        runner.task.cancel()
        try:
            await runner.task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    count, first = fails.get(ip, [0, time.time()])
    if time.time() - first > 900:
        count, first = 0, time.time()
    if count >= 5:
        raise HTTPException(status_code=429, detail="অনেকবার ভুল হয়েছে। ১৫ মিনিট পর চেষ্টা করুন।")

    body = await request.json()
    pw = str(body.get("password", ""))
    if not DASH_PASSWORD or not hmac.compare_digest(pw.encode(), DASH_PASSWORD.encode()):
        fails[ip] = [count + 1, first]
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="পাসওয়ার্ড ভুল")

    fails.pop(ip, None)
    token = secrets.token_urlsafe(32)
    sessions[token] = time.time() + SESSION_TTL
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "lab_session",
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=request.headers.get("x-forwarded-proto") == "https",
    )
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    sessions.pop(request.cookies.get("lab_session"), None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("lab_session")
    return resp


@app.get("/api/status")
async def status(request: Request):
    require_auth(request)
    cookies = json.loads(get_config("COOKIES_JSON") or "[]")
    next_in = None
    if runner.next_at:
        next_in = max(0, int((runner.next_at - datetime.now()).total_seconds()))
    return {
        "running": runner.running,
        "phase": runner.phase,
        "next_in": next_in,
        "next_at": f"{runner.next_at:%H:%M:%S}" if runner.next_at else None,
        "cycles": runner.cycles,
        "last_event": runner.last_event,
        "settings": {k: get_config(k) for k in EDITABLE},
        "cookie_names": [c.get("name", "?") for c in cookies],
        "logs": list(runner.logs)[-80:],
    }


@app.post("/api/toggle")
async def toggle(request: Request):
    require_auth(request)
    if runner.running:
        await runner.stop()
    else:
        await runner.start()
        await asyncio.sleep(0.5)
    return {"running": runner.running}


@app.post("/api/config")
async def save_config(request: Request):
    require_auth(request)
    body = await request.json()
    settings = body.get("settings", {})
    clean = {}
    try:
        for k, v in settings.items():
            if k not in EDITABLE:
                raise ValueError("অজানা সেটিং।")
            clean[k] = parse_value(k, v)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for k, v in clean.items():
        set_config(k, v)
    return {"ok": True}


@app.post("/api/cookies")
async def save_cookies(request: Request):
    require_auth(request)
    body = await request.json()
    try:
        value = parse_value("COOKIES_JSON", body.get("json", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    set_config("COOKIES_JSON", value)
    return {"ok": True, "count": len(json.loads(value))}


PAGE = r"""<!doctype html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lab Auto Manager</title>
<style>
  :root{--bg:#0b0f14;--card:#121923;--line:#1f2a38;--txt:#e6edf3;--mut:#8b9bb0;--green:#22c55e;--red:#ef4444;--blue:#3b82f6}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,"Noto Sans Bengali",sans-serif}
  .wrap{max-width:860px;margin:0 auto;padding:16px}
  header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
  h1{font-size:18px;margin:0} .pill{padding:4px 12px;border-radius:999px;font-weight:700;font-size:13px}
  .on{background:#0f2e1a;color:var(--green)} .off{background:#331416;color:var(--red)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
  .card h2{font-size:14px;color:var(--mut);margin:0 0 12px;font-weight:600;letter-spacing:.3px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .stat{background:#0e141c;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
  .stat small{color:var(--mut);display:block} .stat b{font-size:17px;word-break:break-word}
  button{font:inherit;cursor:pointer;border:0;border-radius:10px;padding:10px 16px;color:#fff;background:var(--blue);font-weight:600}
  button.big{width:100%;padding:16px;font-size:17px} button.go{background:var(--green)} button.stop{background:var(--red)}
  button.ghost{background:#1b2533;color:var(--txt)} button:disabled{opacity:.5}
  label{display:block;color:var(--mut);font-size:13px;margin:10px 0 4px}
  input[type=text],input[type=number],input[type=password],textarea{width:100%;background:#0e141c;border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:10px;font:inherit}
  textarea{min-height:110px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
  .row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .chk{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--txt)}
  .chk input{width:18px;height:18px}
  #logs{background:#070a0e;border:1px solid var(--line);border-radius:10px;padding:10px;height:220px;overflow:auto;font:12px/1.6 ui-monospace,Menlo,monospace;white-space:pre-wrap}
  #toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#1b2533;border:1px solid var(--line);padding:10px 16px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;max-width:90%}
  #toast.show{opacity:1} #login{max-width:360px;margin:12vh auto}
  .hide{display:none!important} .mut{color:var(--mut);font-size:13px}
</style>
</head>
<body>
<div class="wrap">

  <div id="login" class="card hide">
    <h1 style="margin-bottom:12px">🔐 Lab Auto Manager</h1>
    <label>পাসওয়ার্ড</label>
    <input id="pw" type="password" autocomplete="current-password">
    <button id="loginBtn" style="width:100%;margin-top:14px">লগইন</button>
  </div>

  <div id="app" class="hide">
    <header>
      <h1>⚙️ Lab Auto Manager</h1>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="pill" class="pill off">OFF</span>
        <button class="ghost" id="logoutBtn">লগআউট</button>
      </div>
    </header>

    <div class="card">
      <button id="toggle" class="big go">▶️ চালু করুন (ON)</button>
    </div>

    <div class="card">
      <h2>লাইভ অবস্থা</h2>
      <div class="grid">
        <div class="stat"><small>ধাপ</small><b id="phase">-</b></div>
        <div class="stat"><small>পরের ঘটনা</small><b id="countdown">-</b></div>
        <div class="stat"><small>সম্পন্ন চক্র</small><b id="cycles">0</b></div>
        <div class="stat"><small>কুকি</small><b id="cookieCount">0টি</b></div>
      </div>
    </div>

    <div class="card">
      <h2>সেটিংস <span class="mut">(পরের চক্র থেকে কার্যকর)</span></h2>
      <div class="row">
        <div><label>ল্যাবের নাম</label><input id="LAB_NAME" type="text"></div>
        <div><label>Restart-এর আগে বিরতি (মিনিট)</label><input id="RESTART_WAIT_MIN" type="number" min="1"></div>
        <div><label>অতিরিক্ত টাইম (মিনিট)</label><input id="EXTRA_TIME_MIN" type="number" min="1"></div>
        <div><label>কাঙ্ক্ষিত টাইম (মিনিট)</label><input id="MAIN_TIME_MIN" type="number" min="1"></div>
      </div>
      <label class="chk"><input id="START_WITH_STOP" type="checkbox"> চালু হওয়ার সাথে সাথেই Stop দিয়ে শুরু</label>
      <button id="saveSettings" style="margin-top:14px">💾 সেভ করুন</button>
    </div>

    <div class="card">
      <h2>🍪 কুকি</h2>
      <div class="mut" id="cookieNames" style="margin-bottom:8px"></div>
      <textarea id="cookieJson" placeholder="Cookie-Editor থেকে Export করা JSON এখানে পেস্ট করুন"></textarea>
      <button id="saveCookies" style="margin-top:10px">🍪 কুকি সেভ করুন</button>
      <div class="mut" style="margin-top:8px">সেভের পর কুকির মান আর দেখানো হয় না। নতুন কুকি কার্যকর করতে OFF করে আবার ON করুন।</div>
    </div>

    <div class="card">
      <h2>লগ</h2>
      <div id="logs"></div>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let formLoaded = false, nextAt = null, timer = null;

function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}

async function api(path, body){
  const opt = body === undefined ? {} :
    {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'fetch'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('auth'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || 'ত্রুটি হয়েছে');
  return d;
}

function showLogin(){ $('#app').classList.add('hide'); $('#login').classList.remove('hide'); formLoaded=false; }
function showApp(){ $('#login').classList.add('hide'); $('#app').classList.remove('hide'); }

function fmt(s){ const m=Math.floor(s/60), r=s%60; return m+' মি '+String(r).padStart(2,'0')+' সে'; }
function tick(){ $('#countdown').textContent = nextAt ? fmt(Math.max(0, Math.round((nextAt-Date.now())/1000))) : '-'; }

async function refresh(){
  let d;
  try { d = await api('/api/status'); } catch(e){ return; }
  showApp();
  const pill = $('#pill'); pill.textContent = d.running ? 'ON' : 'OFF'; pill.className = 'pill ' + (d.running ? 'on' : 'off');
  const tg = $('#toggle'); tg.textContent = d.running ? '⏹ বন্ধ করুন (OFF)' : '▶️ চালু করুন (ON)'; tg.className = 'big ' + (d.running ? 'stop' : 'go');
  $('#phase').textContent = d.phase;
  $('#cycles').textContent = d.cycles;
  $('#cookieCount').textContent = d.cookie_names.length + 'টি';
  $('#cookieNames').textContent = d.cookie_names.length ? 'বর্তমান কুকি: ' + d.cookie_names.join(', ') : 'কোনো কুকি সেট করা নেই';
  nextAt = d.next_in === null ? null : Date.now() + d.next_in * 1000; tick();
  if (!formLoaded) {
    for (const k of ['LAB_NAME','RESTART_WAIT_MIN','EXTRA_TIME_MIN','MAIN_TIME_MIN']) $('#'+k).value = d.settings[k];
    $('#START_WITH_STOP').checked = d.settings.START_WITH_STOP === 'True';
    formLoaded = true;
  }
  const lg = $('#logs'); const atBottom = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 20;
  lg.textContent = d.logs.join('\n'); if (atBottom) lg.scrollTop = lg.scrollHeight;
}

$('#loginBtn').onclick = async () => {
  try { await api('/api/login', {password: $('#pw').value}); $('#pw').value=''; await refresh(); }
  catch(e){ toast(e.message); }
};
$('#pw').addEventListener('keydown', e => { if (e.key === 'Enter') $('#loginBtn').click(); });
$('#logoutBtn').onclick = async () => { await api('/api/logout', {}); showLogin(); };

$('#toggle').onclick = async () => {
  $('#toggle').disabled = true;
  try { await api('/api/toggle', {}); await refresh(); } catch(e){ toast(e.message); }
  $('#toggle').disabled = false;
};

$('#saveSettings').onclick = async () => {
  const settings = {};
  for (const k of ['LAB_NAME','RESTART_WAIT_MIN','EXTRA_TIME_MIN','MAIN_TIME_MIN']) settings[k] = $('#'+k).value;
  settings.START_WITH_STOP = $('#START_WITH_STOP').checked ? 'True' : 'False';
  try { await api('/api/config', {settings}); toast('✅ সেটিংস সেভ হয়েছে'); } catch(e){ toast('⚠️ ' + e.message); }
};

$('#saveCookies').onclick = async () => {
  try {
    const d = await api('/api/cookies', {json: $('#cookieJson').value});
    $('#cookieJson').value = ''; toast('✅ ' + d.count + 'টি কুকি সেভ হয়েছে'); refresh();
  } catch(e){ toast('⚠️ ' + e.message); }
};

refresh().then(() => { if ($('#app').classList.contains('hide')) showLogin(); });
setInterval(refresh, 3000); setInterval(tick, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if not DASH_PASSWORD or len(DASH_PASSWORD) < 8:
        raise SystemExit("❌ DASH_PASSWORD সেট করুন (কমপক্ষে ৮ অক্ষর): export DASH_PASSWORD='...'")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

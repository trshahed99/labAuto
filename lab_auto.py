import asyncio
import json
from datetime import datetime, timedelta
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================== CONFIG ==================
LAB_NAME = "rana"          # কার্ডে যে নামটা দেখা যায়

START_WITH_STOP  = True    # True = চালু হওয়ার সাথে সাথেই Stop দিয়ে শুরু
RESTART_WAIT_MIN = 1       # Stop করার পর কত মিনিট অপেক্ষা করে Restart করবে
EXTRA_TIME_MIN   = 9       # Restart-এর পর আরেকটা টাইম (শেষ হলে কাঙ্ক্ষিত টাইম শুরু)
MAIN_TIME_MIN    = 50      # কাঙ্ক্ষিত মিনিট (এটা শেষ হলে আবার Stop)

HEADLESS = False           # True দিলে ব্রাউজার দেখা যাবে না

# ============================================================
# 👇👇👇 এখানে Cookie-Editor থেকে কপি করা JSON পেস্ট করো 👇👇👇
# (নিচের ''' আর ''' এর মাঝখানে, [ ] সহ পুরোটা বসাও)
# ============================================================
COOKIES_JSON = r'''
[
    {
        "domain": "labs.2z2.top",
        "expirationDate": 1796095377.533922,
        "hostOnly": true,
        "httpOnly": true,
        "name": "connect.sid",
        "path": "/",
        "sameSite": "lax",
        "secure": false,
        "session": false,
        "storeId": null,
        "value": "s%3AiroVb1sONMsb7tjVQ_kttNcJeaiAmcAr.mTGUat6XJFsOKCyAdCL%2FY5Cx59sbfZu84D7hiRz0c44"
    }
]
'''
# ============================================================
# 👆👆👆 কুকি পেস্ট করার জায়গা এখানে শেষ 👆👆👆
# ============================================================

LOGIN_URL = "https://labs.2z2.top/login"
DASH_URL = "https://labs.2z2.top/dashboard"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_cookies():
    """Cookie-Editor-এর JSON কে Playwright ফরম্যাটে বদলায়।"""
    raw = json.loads(COOKIES_JSON)
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
    """Dashboard-এ যায়। লগইন পেজে পাঠিয়ে দিলে বুঝতে হবে কুকির মেয়াদ শেষ।"""
    await page.goto(DASH_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    if "/login" in page.url:
        raise RuntimeError(
            "কুকির মেয়াদ শেষ! ব্রাউজারে নতুন করে লগইন করে নতুন কুকি "
            "COOKIES_JSON-এ বসাও।"
        )
    await page.wait_for_selector("button", timeout=30000)  # কার্ড লোড হওয়া পর্যন্ত


def card(page):
    # LAB_NAME লেখা আছে এমন কার্ড
    return (
        page.locator("div")
        .filter(has_text=LAB_NAME)
        .filter(has=page.locator("button"))
        .last
    )


async def click_btn(page, names, timeout=15000):
    """আগে কার্ডের ভেতরে খোঁজে, না পেলে পুরো পেজে খোঁজে।"""
    for scope in (card(page), page):
        for name in names:
            btn = scope.get_by_role("button", name=name, exact=False)
            try:
                await btn.first.wait_for(state="visible", timeout=timeout)
                await btn.first.click()
                return True
            except PWTimeout:
                continue
    return False


async def stop_lab(page):
    await ensure_dashboard(page)
    if await click_btn(page, ["Stop"]):
        log("🛑 Stop ক্লিক হয়েছে")
        await page.wait_for_timeout(1500)

        # সাইটের নিজস্ব কনফার্ম পপআপ এলে সেটা চাপবে
        for label in ["Confirm", "Yes", "OK", "Stop"]:
            popup = page.locator('[role="dialog"], .modal, [class*="modal"]').get_by_role(
                "button", name=label
            )
            try:
                await popup.first.click(timeout=3000)
                log(f"✔ কনফার্ম পপআপে '{label}' চাপা হয়েছে")
                break
            except PWTimeout:
                continue

        await page.wait_for_timeout(3000)
        await page.reload(wait_until="domcontentloaded")
        # সত্যিই বন্ধ হয়েছে কিনা দেখা
        still = await page.get_by_text("Started", exact=True).count()
        log("✅ Stop সফল" if still == 0 else "⚠️ এখনো Started দেখাচ্ছে")
    else:
        log("⚠️ Stop বাটন পাইনি (হয়তো আগে থেকেই বন্ধ)")

async def start_lab(page):
    await ensure_dashboard(page)
    if await click_btn(page, ["Start", "Restart", "Run"]):
        log("▶️ Restart/Start ক্লিক হয়েছে")
    else:
        log("⚠️ Start বাটন পাইনি")


async def wait_minutes(minutes, label):
    end = datetime.now() + timedelta(minutes=minutes)
    log(f"⏳ {label}: {minutes} মিনিট অপেক্ষা (শেষ হবে {end:%H:%M:%S})")
    await asyncio.sleep(minutes * 60)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=HEADLESS)
        context = await browser.new_context()

        cookies = load_cookies()
        await context.add_cookies(cookies)
        log(f"🍪 {len(cookies)}টি কুকি বসানো হয়েছে")

        page = await context.new_page()
        # confirm/alert dialog আসলে নিজে নিজে Accept করবে
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        await ensure_dashboard(page)
        log("Dashboard-এ ঢুকেছি ✅")

        # START_WITH_STOP=False হলে আগে কাঙ্ক্ষিত টাইম পূর্ণ করবে
        if not START_WITH_STOP:
            await wait_minutes(MAIN_TIME_MIN, "কাঙ্ক্ষিত টাইম")

        while True:
            try:
                await stop_lab(page)
                await wait_minutes(RESTART_WAIT_MIN, "Restart-এর আগে বিরতি")
                await start_lab(page)
            except Exception as e:
                log(f"❌ ত্রুটি: {e} (চক্র চলতে থাকবে)")
            await wait_minutes(EXTRA_TIME_MIN, "অতিরিক্ত টাইম")
            await wait_minutes(MAIN_TIME_MIN, "কাঙ্ক্ষিত টাইম")


if __name__ == "__main__":
    asyncio.run(main())
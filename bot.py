import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Config
# -------------------------
TOKEN = os.getenv("TG_BOT_TOKEN")  # IMPORTANT: use getenv to avoid KeyError
BASE_URL = "https://vehiclescore.co.uk/score"
VRM_RE = re.compile(r"^[A-Z0-9]{1,8}$")

PORT = int(os.getenv("PORT", "8080"))

# -------------------------
# FastAPI health server (for Fly checks)
# -------------------------
web = FastAPI()

@web.get("/health")
def health():
    return {"ok": True}

# -------------------------
# Helpers
# -------------------------
def normalize_vrm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").upper())

def build_url(vrm: str) -> str:
    return f"{BASE_URL}?{urlencode({'registration': vrm})}"

async def take_screenshot(url: str, out_path: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # 1) صبر هوشمند برای نهایی شدن Score (تا 20 ثانیه)
            # ایده: عدد بزرگ وسط گیج تغییر می‌کنه؛ ما صبر می‌کنیم 2 بار پشت‌سرهم ثابت بمونه
            await page.wait_for_timeout(1500)

            stable = 0
            last = None
            for _ in range(20):  # ~20s
                try:
                    score = await page.evaluate("""
                      () => {
                        // اولین عدد بزرگ که معمولاً score هست
                        const el = document.querySelector("h1, h2, h3, .score, [class*='score']");
                        // fallback: پیدا کردن بزرگترین متن عددی داخل صفحه
                        const texts = Array.from(document.querySelectorAll("body *"))
                          .map(e => e.textContent?.trim())
                          .filter(t => t && /^[0-9]{2,4}$/.test(t));
                        if (texts.length) return texts[0];
                        return null;
                      }
                    """)
                except Exception:
                    score = None

                if score and score == last:
                    stable += 1
                else:
                    stable = 0
                last = score

                if stable >= 2:  # دو بار پشت سر هم ثابت شد
                    break
                await page.wait_for_timeout(1000)

            # 2) اسکرول مرحله‌ای برای lazy-load (محدود برای جلوگیری از OOM)
            await page.evaluate("""
                async () => {
                  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                  let lastH = -1;
                  for (let i = 0; i < 14; i++) {   // محدود ولی کافی
                    window.scrollBy(0, Math.max(800, window.innerHeight * 0.9));
                    await sleep(700);
                    const h = document.body.scrollHeight;
                    if (h === lastH) break;
                    lastH = h;
                  }
                  await sleep(800);
                  window.scrollTo(0, 0);
                  await sleep(800);
                }
            """)

            # 3) یک صبر کوتاه بعد از برگشت به بالا
            await page.wait_for_timeout(1200)

            # 4) full_page
            await page.screenshot(path=out_path, full_page=True)

        finally:
            await context.close()
            await browser.close()

# -------------------------
# Telegram bot handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("پلاک رو بفرست (مثال: VN64NWG)")

async def handle_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vrm = normalize_vrm(update.message.text)
    if not VRM_RE.match(vrm):
        await update.message.reply_text("پلاک معتبر نیست. فقط حروف/عدد، بدون فاصله/کاراکتر اضافی.")
        return

    url = build_url(vrm)
    context.user_data["vrm"] = vrm
    context.user_data["url"] = url

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Screenshot", callback_data="shot")],
        [InlineKeyboardButton("🔗 Open link", url=url)],
    ])

    await update.message.reply_text(
        f"پلاک: {vrm}\nمی‌خوای اسکرین‌شات صفحه رو بگیرم؟",
        reply_markup=kb
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data != "shot":
        return

    vrm = context.user_data.get("vrm")
    url = context.user_data.get("url")
    if not vrm or not url:
        await q.edit_message_text("اول پلاک رو بفرست.")
        return

    await q.edit_message_text(f"در حال گرفتن اسکرین‌شات: {vrm} ...")

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = f"/tmp/{vrm}_{ts}.png"

    try:
        await take_screenshot(url, out_path)
        await q.message.reply_photo(
            photo=open(out_path, "rb"),
            caption=f"{vrm}\n{url}"
        )
    except Exception as e:
        await q.edit_message_text(f"اسکرین‌شات ناموفق شد.\n{url}\nError: {e}")

# -------------------------
# Runners
# -------------------------
async def run_bot():
    if not TOKEN:
        # واضح و تمیز تو لاگ
        raise RuntimeError("TG_BOT_TOKEN is missing. Set it in Fly secrets and restart the machine.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plate))
    app.add_handler(CallbackQueryHandler(on_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # keep alive forever
    await asyncio.Event().wait()

async def run_web():
    config = uvicorn.Config(web, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())
import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlencode

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

BASE = "https://vehiclescore.co.uk/score"
VRM_RE = re.compile(r"^[A-Z0-9]{1,8}$")  # ساده و سخت‌گیر

def normalize_vrm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").upper())

def build_url(vrm: str) -> str:
    return f"{BASE}?{urlencode({'registration': vrm})}"

async def take_screenshot(url: str, out_path: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36",
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # اگر پاپ‌آپ/کوکی داشت و پیدا شد، ببندیم (best-effort)
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('I Agree')",
                "button:has-text('OK')",
                "button:has-text('Got it')",
                "text=Accept all",
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click(timeout=2000)
                        break
                except Exception:
                    pass

            # کمی صبر برای رندر
            await page.wait_for_timeout(2000)

            await page.screenshot(path=out_path, full_page=True)
        except PWTimeoutError:
            # اگر دیر لود شد، باز هم تلاش برای اسکرین
            await page.screenshot(path=out_path, full_page=True)
        finally:
            await context.close()
            await browser.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("پلاک رو بفرست (مثال: VN64NWG).")

async def handle_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vrm = normalize_vrm(update.message.text)
    if not VRM_RE.match(vrm):
        await update.message.reply_text("پلاک معتبر نیست. فقط حروف/عدد (بدون فاصله یا کاراکتر اضافی).")
        return

    url = build_url(vrm)
    context.user_data["vrm"] = vrm
    context.user_data["url"] = url

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Screenshot", callback_data="shot")],
        [InlineKeyboardButton("🔗 Open link", url=url)],
    ])

    await update.message.reply_text(
        f"پلاک: {vrm}\nلینک آماده است. می‌خوای اسکرین‌شات بگیرم؟",
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

    await q.edit_message_text(f"دارم اسکرین‌شات می‌گیرم: {vrm} ...")

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

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plate))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
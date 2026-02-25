import os
import re
import asyncio
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from playwright.async_api import async_playwright


# ============================
# Config
# ============================
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN is not set. Use: fly secrets set TG_BOT_TOKEN='...'")


BASE_URL = "https://vehiclescore.co.uk/score?registration={reg}"

TMP_DIR = Path("/tmp")
TMP_DIR.mkdir(parents=True, exist_ok=True)

# UK-ish simple plate validator (strict enough for your use)
PLATE_RE = re.compile(r"^[A-Z0-9]{2,8}$", re.IGNORECASE)


# ============================
# FastAPI Health (for Fly)
# ============================
app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


async def run_web():
    """
    Must listen on 0.0.0.0:$PORT so Fly smoke checks pass.
    """
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# ============================
# Helpers
# ============================
def normalize_plate(text: str) -> str:
    return (text or "").strip().upper().replace(" ", "")


# ============================
# Playwright screenshot
# ============================
async def take_screenshot_full(url: str, out_path: str) -> None:
    """
    - Loads page
    - Waits for SPA to render + score to stabilize
    - Tries to detect the main scroll container (or falls back to document)
    - Scrolls down repeatedly until scrollHeight stops changing
    - Takes full_page screenshot
    """
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
            # Avoid networkidle (can hang on modern sites)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Give time to render
            await page.wait_for_timeout(2500)

            # Extra wait: score and sections often load after initial render
            await page.wait_for_timeout(8000)

            # Scroll logic: pick biggest scrollable container, else document
            await page.evaluate(
                """
                async () => {
                  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

                  const getScroller = () => {
                    const els = Array.from(document.querySelectorAll('*'));
                    const scrollables = els.filter(el => {
                      const s = getComputedStyle(el);
                      const overflowY = s.overflowY;
                      const canScroll = (overflowY === 'auto' || overflowY === 'scroll');
                      return canScroll && el.scrollHeight > el.clientHeight + 300;
                    });

                    if (scrollables.length) {
                      scrollables.sort((a,b) =>
                        (b.scrollHeight - b.clientHeight) -
                        (a.scrollHeight - a.clientHeight)
                      );
                      return scrollables[0];
                    }

                    return document.scrollingElement || document.documentElement;
                  };

                  const scroller = getScroller();

                  let lastHeight = -1;
                  let stableCount = 0;

                  // Scroll down up to N steps; stop when height stops growing
                  for (let i = 0; i < 35; i++) {
                    scroller.scrollTo(0, scroller.scrollHeight);
                    await sleep(1400);

                    const h = scroller.scrollHeight;

                    if (h === lastHeight) {
                      stableCount++;
                      if (stableCount >= 3) break;
                    } else {
                      stableCount = 0;
                    }

                    lastHeight = h;
                  }

                  // Let late content settle
                  await sleep(1500);

                  // Go top for nicer screenshot header
                  scroller.scrollTo(0, 0);
                  await sleep(900);
                }
                """
            )

            await page.screenshot(path=out_path, full_page=True)

        finally:
            await context.close()
            await browser.close()


# ============================
# Telegram handlers
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "پلاک رو بفرست (مثال: VN64NWG)\n"
        "بعد دکمه Screenshot رو بزن."
    )


async def on_plate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plate = normalize_plate(update.message.text)

    if not PLATE_RE.match(plate):
        await update.message.reply_text("فرمت پلاک درست نیست. مثال: VN64NWG")
        return

    context.user_data["plate"] = plate

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Screenshot", callback_data="shot")],
        [InlineKeyboardButton("🔗 Open link", url=BASE_URL.format(reg=plate))],
    ])

    await update.message.reply_text(
        f"پلاک: {plate}\nمی‌خوای اسکرین‌شات صفحه رو بگیرم؟",
        reply_markup=keyboard
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data != "shot":
        return

    plate = context.user_data.get("plate")
    if not plate:
        await query.edit_message_text("اول پلاک رو بفرست (مثال: VN64NWG)")
        return

    url = BASE_URL.format(reg=plate)
    out_path = str(TMP_DIR / f"{plate}.png")

    await query.edit_message_text(f"در حال گرفتن اسکرین‌شات کامل برای: {plate} ...")

    try:
        await take_screenshot_full(url, out_path)
        await query.message.reply_photo(photo=open(out_path, "rb"), caption=f"{plate}\n{url}")
    except Exception as e:
        await query.message.reply_text(f"خطا: {type(e).__name__}: {e}")
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except Exception:
            pass


# ============================
# Bot runner (PTB v20+ stable)
# ============================
async def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plate_text))
    application.add_handler(CallbackQueryHandler(on_callback))

    # ✅ PTB v20+ stable runner (no updater.idle)
    await application.run_polling(close_loop=False)


# ============================
# Main
# ============================
async def main():
    # Run both: bot + web health server
    await asyncio.gather(run_bot(), run_web())


if __name__ == "__main__":
    asyncio.run(main())
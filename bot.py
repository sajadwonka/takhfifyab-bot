"""Telegram price-hunter bot (ربات شکار تخفیف و قیمت) — MVP.

Flow
----
1. The user sends a Digikala product link (``digikala.com/product/dkp-<id>/``).
2. The bot fetches the current cheapest price and saves a watch.
3. Every hour the bot re-checks every watch and sends a Telegram alert
   when the price drops by ``ALERT_DROP_PCT``% or more, or reaches the
   user's target price (``/target``).

Commands: /start /help /add /list /remove /price /target

Environment
-----------
TELEGRAM_BOT_TOKEN  (required) — BotFather token, never in files.
AFFILIO_ID          (optional) — Digikala Affilio id, added to product links.
AFFILIO_URL_TEMPLATE (optional) — exact Affilio link template, {id} / {aff}.
CHECK_INTERVAL_MINUTES (optional, default 60) — how often prices re-check.
ALERT_DROP_PCT      (optional, default 3) — alert when the price falls this %.
MAX_WATCHES_PER_USER (optional, default 10).
DB_PATH             (optional, default pricebot.db).
"""

from __future__ import annotations

import logging
import os
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from price import (
    ApiError,
    DigikalaClient,
    ProductNotFound,
    ProductOutOfStock,
    build_product_url,
    drop_percent,
    extract_product_id,
    fetch_product_resilient,
    format_toman,
)
from store import Store

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pricebot")

MAX_WATCHES = int(os.environ.get("MAX_WATCHES_PER_USER", "10"))
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
ALERT_DROP_PCT = float(os.environ.get("ALERT_DROP_PCT", "3"))
DB_PATH = os.environ.get("DB_PATH", "pricebot.db")

store = Store(DB_PATH)

WELCOME = """👋 سلام! من ربات شکار تخفیفم.

لینک هر کالایی از دیجی‌کالا را بفرست تا قیمتش را زیر نظر بگیرم؛ وقتی ارزان شد، همین‌جا خبرت می‌کنم. 🎯

📌 دستورها:
/add <لینک> — اضافه کردن کالا
/list — کالاهای زیر نظر
/price <لینک یا شماره> — قیمت تازه یک کالا
/target <شماره> <قیمت> — هشدار وقتی به قیمت هدفت رسید
/remove <شماره> — حذف یک کالا

💡 مثال: کافی است لینک کالا را بفرستی؛ مثلاً
https://www.digikala.com/product/dkp-20109389/
"""

HELP = """📌 دستورها:

/add <لینک دیجی‌کالا> — اضافه کردن کالا به لیست پیگیری
/list — دیدن کالاهای زیر نظر و قیمتشان
/price <لینک یا شماره در لیست> — قیمت تازه
/target <شماره> <قیمت به تومان> — مثلاً: /target 1 35000000
/remove <شماره> — حذف کالا از لیست

⏰ قیمت‌ها هر ساعت بررسی می‌شوند؛ اگر قیمت ۳٪ یا بیشتر کم شود، یا به قیمت هدفت برسد، پیام می‌دهم.

💰 لینک خریدی که می‌فرستم، لینک افیلیت است؛ اگر از آن خرید کنی، درصدی از فروش به من می‌رسد و برای تو هزینه اضافه‌ای ندارد.
"""


def _watch_line(i: int, w, current: int | None = None) -> str:
    price = current if current is not None else w.last_price
    change = drop_percent(w.first_price, price)
    arrow = "📉" if change > 0 else ("📈" if change < 0 else "➖")
    target = f" | 🎯 هدف: {format_toman(w.target_price)}" if w.target_price else ""
    return (
        f"{i}. {w.title}\n"
        f"   💰 {format_toman(price)} تومان {arrow} {abs(change):.1f}٪"
        f" (اولین قیمت: {format_toman(w.first_price)}){target}\n"
        f"   🔗 {build_product_url(w.product_id)}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def _add_watch(update: Update, user_id: int, product_id: int) -> None:
    if store.get_watch(user_id, product_id):
        watch = store.get_watch(user_id, product_id)
        await update.message.reply_text(
            "این کالا از قبل در لیست توست:\n\n"
            f"📦 {watch.title}\n💰 قیمت فعلی: {format_toman(watch.last_price)} تومان"
        )
        return
    if store.count_watches(user_id) >= MAX_WATCHES:
        await update.message.reply_text(
            f"سقف نسخه آزمایشی {MAX_WATCHES} کالاست. اول یکی را با /remove حذف کن."
        )
        return
    wait = await update.message.reply_text("🔍 دارم قیمت کالا را می‌گیرم…")
    try:
        info = await fetch_product_resilient(product_id)
    except ProductNotFound as e:
        await wait.edit_text(f"❌ {e}")
        return
    except ProductOutOfStock as e:
        await wait.edit_text(f"⚠️ {e}\nمی‌توانی بعداً دوباره امتحان کنی.")
        return
    except ApiError as e:
        await wait.edit_text(f"❌ {e}")
        return
    if info.price_toman is None:
        await wait.edit_text("⚠️ قیمت این کالا فعلاً در دسترس نیست.")
        return
    watch = store.add_watch(user_id, product_id, info.title, info.price_toman)
    lowest = f"\n📉 کمترین قیمت ۳۰ روز گذشته: {format_toman(info.lowest_30d_toman)} تومان" if info.lowest_30d_toman else ""
    await wait.edit_text(
        "✅ کالا به لیست پیگیری اضافه شد:\n\n"
        f"📦 {info.title}\n"
        f"💰 قیمت فعلی: {format_toman(info.price_toman)} تومان"
        + (f" (فروشنده: {info.seller})" if info.seller else "")
        + lowest
        + f"\n\nهر ساعت چک می‌کنم؛ اگر ۳٪ یا بیشتر ارزان شد خبرت می‌کنم. 🎯\n"
        f"برای تعیین قیمت هدف: /target {len(store.list_watches(user_id))} <قیمت>\n"
        f"🔗 {build_product_url(product_id)}"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""
    product_id = extract_product_id(text)
    if product_id is None:
        await update.message.reply_text(
            "لینک کالا را هم بفرست؛ مثلاً:\n/add https://www.digikala.com/product/dkp-20109389/"
        )
        return
    await _add_watch(update, update.effective_user.id, product_id)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any message containing a Digikala product link adds a watch."""
    product_id = extract_product_id(update.message.text or "")
    if product_id is None:
        await update.message.reply_text(
            "برای پیگیری قیمت، لینک کالای دیجی‌کالا را بفرست؛ مثلاً:\n"
            "https://www.digikala.com/product/dkp-20109389/\n\n"
            "دستورها: /help"
        )
        return
    await _add_watch(update, update.effective_user.id, product_id)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    watches = store.list_watches(update.effective_user.id)
    if not watches:
        await update.message.reply_text(
            "هنوز کالایی را زیر نظر نگرفته‌ای. لینک یک کالا از دیجی‌کالا بفرست. 🔍"
        )
        return
    lines = ["📋 کالاهای زیر نظر تو:\n"]
    for i, w in enumerate(watches, 1):
        lines.append(_watch_line(i, w))
    lines.append("\nبرای حذف: /remove <شماره>")
    await update.message.reply_text("\n\n".join(lines), disable_web_page_preview=True)


async def _resolve_watch(update: Update, arg: str) -> tuple | None:
    """Resolve /price and /remove argument: a link or a list number."""
    user_id = update.effective_user.id
    product_id = extract_product_id(arg)
    if product_id is not None:
        return product_id, store.get_watch(user_id, product_id)
    if arg.isdigit():
        watches = store.list_watches(user_id)
        idx = int(arg) - 1
        if 0 <= idx < len(watches):
            w = watches[idx]
            return w.product_id, w
    return None


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = " ".join(context.args) if context.args else ""
    if not arg:
        await update.message.reply_text("لینک کالا یا شماره‌اش در لیست را بده؛ مثلاً: /price 1")
        return
    resolved = await _resolve_watch(update, arg)
    if resolved is None:
        await update.message.reply_text("این کالا در لیست تو نیست. اول لینکش را بفرست تا اضافه‌اش کنم.")
        return
    product_id, watch = resolved
    wait = await update.message.reply_text("🔍 دارم قیمت تازه را می‌گیرم…")
    try:
        info = await fetch_product_resilient(product_id)
    except (ProductNotFound, ProductOutOfStock, ApiError) as e:
        await wait.edit_text(f"❌ {e}")
        return
    if info.price_toman is None:
        await wait.edit_text("⚠️ قیمت این کالا فعلاً در دسترس نیست.")
        return
    if watch is not None:
        store.update_price(update.effective_user.id, product_id, info.price_toman)
        change = drop_percent(watch.first_price, info.price_toman)
        arrow = "📉" if change > 0 else ("📈" if change < 0 else "➖")
        change_txt = f"{arrow} {abs(change):.1f}٪ نسبت به اولین قیمت ({format_toman(watch.first_price)})"
    else:
        change_txt = "این کالا در لیست پیگیری تو نیست؛ برای اضافه کردنش لینکش را بفرست."
    lowest = f"\n📉 کمترین قیمت ۳۰ روز گذشته: {format_toman(info.lowest_30d_toman)} تومان" if info.lowest_30d_toman else ""
    await wait.edit_text(
        f"📦 {info.title}\n"
        f"💰 قیمت فعلی: {format_toman(info.price_toman)} تومان"
        + (f" (فروشنده: {info.seller})" if info.seller else "")
        + lowest
        + f"\n{change_txt}\n🔗 {build_product_url(product_id)}"
    )


async def target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "قالب درست: /target <شماره کالا> <قیمت به تومان>\nمثلاً: /target 1 35000000"
        )
        return
    price_text = re.sub(r"[^\d]", "", args[1])
    if not price_text:
        await update.message.reply_text("قیمت هدف را به تومان و با عدد بنویس؛ مثلاً: /target 1 35000000")
        return
    watches = store.list_watches(update.effective_user.id)
    idx = int(args[0]) - 1
    if not 0 <= idx < len(watches):
        await update.message.reply_text("شماره کالا درست نیست؛ با /list شماره‌ها را ببین.")
        return
    watch = watches[idx]
    target = int(price_text)
    store.set_target(update.effective_user.id, watch.product_id, target)
    await update.message.reply_text(
        f"🎯 ثبت شد! وقتی قیمت «{watch.title}» به {format_toman(target)} تومان یا کمتر برسد، خبرت می‌کنم.\n"
        f"قیمت فعلی: {format_toman(watch.last_price)} تومان"
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = " ".join(context.args) if context.args else ""
    if not arg:
        await update.message.reply_text("شماره کالا را بده؛ مثلاً: /remove 1 (شماره‌ها را با /list ببین)")
        return
    resolved = await _resolve_watch(update, arg)
    if resolved is None:
        await update.message.reply_text("این کالا در لیست تو نیست.")
        return
    product_id, watch = resolved
    if store.remove_watch(update.effective_user.id, product_id):
        await update.message.reply_text(f"🗑 «{watch.title}» از لیست پیگیری حذف شد.")
    else:
        await update.message.reply_text("حذف انجام نشد؛ دوباره تلاش کن.")


async def check_all_prices(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly job: re-check every watch, alert on drops or target hits."""
    watches = store.all_watches()
    if not watches:
        return
    log.info("check cycle: %d watches", len(watches))
    async with DigikalaClient() as client:
        for watch in watches:
            try:
                info = await client.fetch_product(watch.product_id)
            except ProductNotFound:
                log.warning("product %s gone, keeping watch", watch.product_id)
                continue
            except (ProductOutOfStock, ApiError) as e:
                log.warning("check failed for %s: %s", watch.product_id, e)
                continue
            if info.price_toman is None:
                continue
            old_price = watch.last_price
            store.update_price(watch.user_id, watch.product_id, info.price_toman)
            drop = drop_percent(old_price, info.price_toman)
            target_hit = (
                watch.target_price is not None
                and info.price_toman <= watch.target_price
                and old_price > watch.target_price
            )
            if drop >= ALERT_DROP_PCT or target_hit:
                reason = (
                    f"🎯 به قیمت هدفت ({format_toman(watch.target_price)} تومان) رسید!"
                    if target_hit
                    else f"📉 قیمت {drop:.1f}٪ کم شد!"
                )
                try:
                    await context.bot.send_message(
                        chat_id=watch.user_id,
                        text=(
                            f"🔔 هشدار کاهش قیمت!\n\n"
                            f"📦 {info.title}\n"
                            f"{reason}\n"
                            f"💰 قیمت قبل: {format_toman(old_price)} تومان\n"
                            f"💰 قیمت جدید: {format_toman(info.price_toman)} تومان\n"
                            f"📉 کمترین قیمت دیده‌شده: {format_toman(min(watch.lowest_price, info.price_toman))} تومان\n"
                            f"🔗 خرید: {build_product_url(watch.product_id)}"
                        ),
                        disable_web_page_preview=True,
                    )
                except Exception as e:  # user blocked the bot, etc.
                    log.warning("could not alert user %s: %s", watch.user_id, e)
            # Small pause so Digikala is never hammered.
            await _sleep_between_requests()
    log.info("check cycle done")


async def _sleep_between_requests() -> None:
    import asyncio

    await asyncio.sleep(0.8)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN تنظیم نشده است. (توکن را فقط از متغیر محیطی بخوان؛ هرگز در فایل ننویس.)")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("target", target_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            check_all_prices,
            interval=CHECK_INTERVAL_MINUTES * 60,
            first=60,
            name="price_check",
        )
        log.info("price checker scheduled every %d minutes", CHECK_INTERVAL_MINUTES)
    else:
        log.warning("JobQueue در دسترس نیست؛ بررسی دوره‌ای قیمت کار نمی‌کند. python-telegram-bot[job-queue] را نصب کن.")
    log.info("price-bot started; DB=%s", DB_PATH)
    app.run_polling()


if __name__ == "__main__":
    main()

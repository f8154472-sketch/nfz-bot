import os
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

load_dotenv()

TOKEN_ADDRESS = os.getenv("TOKEN_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 300))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def escape_markdown(text: str) -> str:
    special = ["_", "*", "[", "]", "(", ")", "`"]
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


async def fetch_json(session, url, params=None):
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, params=params, timeout=timeout) as resp:
            if resp.status == 429:
                logger.warning("Rate limit")
                return None
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.error(f"HTTP error: {e}")
        return None


async def fetch_dex_data(session):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    data = await fetch_json(session, url)
    if not data:
        return None
    pairs = data.get("pairs")
    if pairs and isinstance(pairs, list) and len(pairs) > 0:
        return pairs[0]
    return None


async def get_holders_count(session):
    if not BSCSCAN_API_KEY:
        return "N/A"
    url = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": 56,
        "module": "token",
        "action": "tokenholderlist",
        "contractaddress": TOKEN_ADDRESS,
        "page": 1,
        "offset": 100,
        "apikey": BSCSCAN_API_KEY,
    }
    data = await fetch_json(session, url, params)
    if data and data.get("status") == "1" and isinstance(data.get("result"), list):
        count = len(data["result"])
        return f"{count}+" if count >= 100 else str(count)
    return "N/A"


async def build_message(session, pair):
    try:
        price = float(pair.get("priceUsd", 0))
        change = pair.get("priceChange", {}).get("h24", 0) or 0
        volume = pair.get("volume", {}).get("h24", 0) or 0
        liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
        symbol = escape_markdown(pair.get("baseToken", {}).get("symbol", "TOKEN"))
        trend = "📈" if float(change) >= 0 else "📉"
        holders = await get_holders_count(session)

        return (
            f"*{symbol} Price Update*\n\n"
            f"💰 Price: *${price:.8f}*\n"
            f"{trend} 24h: *{float(change):.2f}%*\n"
            f"🔄 Volume: *${volume:,.0f}*\n"
            f"💧 Liquidity: *${liquidity:,.0f}*\n"
            f"👥 Holders: *{holders}*\n\n"
            f"🔗 [DexScreener]({pair.get('url', '')})"
        )
    except Exception as e:
        logger.error(f"Ошибка сборки: {e}")
        return None


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Price", callback_data="cmd:price"),
            InlineKeyboardButton("👥 Holders", callback_data="cmd:holders"),
        ],
        [InlineKeyboardButton("🔗 DexScreener", url=f"https://dexscreener.com/bsc/{TOKEN_ADDRESS}")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Token Tracker*\n\nReal-time price & holder updates.",
        parse_mode="Markdown", reply_markup=main_keyboard(),
    )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data["session"]
    pair = await fetch_dex_data(session)
    if pair:
        msg = await build_message(session, pair)
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            return
    await update.message.reply_text("❌ Токен не найден на DexScreener")


async def holders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data["session"]
    count = await get_holders_count(session)
    await update.message.reply_text(f"👥 *Holders:* {count}", parse_mode="Markdown")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = context.application.bot_data["session"]

    if data == "cmd:price":
        pair = await fetch_dex_data(session)
        if pair:
            msg = await build_message(session, pair)
            if msg:
                try:
                    await query.edit_message_text(msg, parse_mode="Markdown",
                        disable_web_page_preview=True, reply_markup=main_keyboard())
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
                return
        await query.edit_message_text("❌ Токен не найден", reply_markup=main_keyboard())

    elif data == "cmd:holders":
        count = await get_holders_count(session)
        try:
            await query.edit_message_text(f"👥 *Holders:* {count}",
                parse_mode="Markdown", reply_markup=main_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise


async def periodic_update(context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data["session"]
    pair = await fetch_dex_data(session)
    if not pair:
        return
    msg = await build_message(session, pair)
    if not msg:
        return
    try:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode="Markdown", disable_web_page_preview=True)
        logger.info("Обновление отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


async def post_init(app: Application):
    app.bot_data["session"] = aiohttp.ClientSession()


async def post_shutdown(app: Application):
    session = app.bot_data.get("session")
    if session:
        await session.close()


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("holders", holders_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    if app.job_queue:
        app.job_queue.run_repeating(periodic_update, interval=UPDATE_INTERVAL, first=10)

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

import os
import json
import random
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import BadRequest

load_dotenv()

TOKEN_ADDRESS = os.getenv("TOKEN_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 300))
ADMIN_ID = os.getenv("ADMIN_ID", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org/")
GAME_PRIZE_AMOUNT = os.getenv("GAME_PRIZE_AMOUNT", "5000000000")
REFERRAL_PRIZE_AMOUNT = os.getenv("REFERRAL_PRIZE_AMOUNT", "2000000000")

DATA_FILE = "data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

w3 = None
sender_account = None
AUTO_SEND_ENABLED = False

if WALLET_PRIVATE_KEY:
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))
        sender_account = w3.eth.account.from_key(WALLET_PRIVATE_KEY)
        AUTO_SEND_ENABLED = True
        logger.info("Automatic prize sending is ENABLED")
    except Exception as e:
        logger.error(f"Failed to set up web3 auto-send: {e}")
        AUTO_SEND_ENABLED = False
else:
    logger.info("WALLET_PRIVATE_KEY not set. Manual mode.")

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"referrals": {}, "game_wins": {}, "wallets": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


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
        "chainid": 56, "module": "token", "action": "tokenholderlist",
        "contractaddress": TOKEN_ADDRESS, "page": 1, "offset": 100,
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
        trend = "\U0001F4C8" if float(change) >= 0 else "\U0001F4C9"
        holders = await get_holders_count(session)
        return (
            f"*{symbol} Price Update*\n\n"
            f"Price: *${price:.8f}*\n"
            f"{trend} 24h: *{float(change):.2f}%*\n"
            f"Volume: *${volume:,.0f}*\n"
            f"Liquidity: *${liquidity:,.0f}*\n"
            f"Holders: *{holders}*\n\n"
            f"[DexScreener]({pair.get('url', '')})"
        )
    except Exception as e:
        logger.error(f"Message build error: {e}")
        return None


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Price", callback_data="cmd:price"),
            InlineKeyboardButton("Holders", callback_data="cmd:holders"),
        ],
        [
            InlineKeyboardButton("Play", callback_data="cmd:play"),
            InlineKeyboardButton("Referral", callback_data="cmd:ref"),
        ],
        [InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/bsc/{TOKEN_ADDRESS}")],
    ])


def send_token_reward(to_address: str, amount_tokens: int) -> tuple:
    if not AUTO_SEND_ENABLED:
        return False, "Automatic sending is not configured."
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(TOKEN_ADDRESS), abi=ERC20_ABI)
        decimals = contract.functions.decimals().call()
        amount_wei = int(amount_tokens * (10 ** decimals))
        nonce = w3.eth.get_transaction_count(sender_account.address)
        txn = contract.functions.transfer(
            Web3.to_checksum_address(to_address), amount_wei
        ).build_transaction({
            "chainId": 56,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
        })
        signed_txn = w3.eth.account.sign_transaction(txn, private_key=WALLET_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        return True, tx_hash.hex()
    except Exception as e:
        logger.error(f"Token send error: {e}")
        return False, str(e)async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if context.args:
        ref_id = context.args[0]
        if ref_id != user_id and user_id not in data["referrals"]:
            data["referrals"].setdefault(ref_id, {"count": 0, "invited": []})
            if user_id not in data["referrals"][ref_id]["invited"]:
                data["referrals"][ref_id]["invited"].append(user_id)
                data["referrals"][ref_id]["count"] += 1
                save_data(data)
                try:
                    await context.bot.send_message(
                        chat_id=int(ref_id),
                        text="A new user joined using your referral link! Use /wallet to set your BSC address."
                    )
                except Exception:
                    pass

    await update.message.reply_text(
        "*NFZ Token Bot*\n\nPrice, holders, game and referrals.\n"
        "Use /wallet YOUR_BSC_ADDRESS to register where prizes should be sent.",
        parse_mode="Markdown", reply_markup=main_keyboard(),
    )


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            "Usage: /wallet 0xYourBscAddress\nThis is where your prizes will be sent."
        )
        return
    address = context.args[0]
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("Invalid address format.")
        return
    data = load_data()
    data.setdefault("wallets", {})
    data["wallets"][user_id] = address
    save_data(data)
    await update.message.reply_text(f"Wallet saved: {address}")


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data["session"]
    pair = await fetch_dex_data(session)
    if pair:
        msg = await build_message(session, pair)
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            return
    await update.message.reply_text("Token not found on DexScreener")


async def holders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data["session"]
    count = await get_holders_count(session)
    await update.message.reply_text(f"Holders: *{count}*", parse_mode="Markdown")


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["game_number"] = random.randint(1, 10)
    await update.message.reply_text("I picked a number from 1 to 10! Type a number to guess.")


async def guess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "game_number" not in context.user_data:
        return
    text = update.message.text.strip()
    if not text.isdigit():
        return
    guess = int(text)
    target = context.user_data["game_number"]

    if guess == target:
        user_id = str(update.effective_user.id)
        username = update.effective_user.username or update.effective_user.first_name
        data = load_data()
        data["game_wins"].setdefault(user_id, {"wins": 0, "username": username})
        data["game_wins"][user_id]["wins"] += 1
        save_data(data)
        del context.user_data["game_number"]

        wallet = data.get("wallets", {}).get(user_id)

        if AUTO_SEND_ENABLED and wallet:
            amount = int(GAME_PRIZE_AMOUNT)
            success, result = send_token_reward(wallet, amount)
            if success:
                await update.message.reply_text(
                    f"You guessed it! Number was {target}.\n{amount:,} NFZ sent!\nTx: {result}"
                )
            else:
                await update.message.reply_text(
                    f"You guessed it! Number was {target}.\nAuto-send failed. Admin will send manually."
                )
                if ADMIN_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=int(ADMIN_ID),
                            text=f"AUTO-SEND FAILED for @{username} ({user_id}), wallet: {wallet}"
                        )
                    except Exception:
                        pass
        else:
            await update.message.reply_text(
                f"You guessed it! Number was {target}.\nPrize sent manually by admin."
                + ("" if wallet else "\nSet your wallet with /wallet 0xYourAddress")
            )
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=int(ADMIN_ID),
                        text=f"Game winner: @{username} ({user_id}), wallet: {wallet or 'NOT SET'}"
                    )
                except Exception:
                    pass
    elif guess < target:
        await update.message.reply_text("Higher!")
    else:
        await update.message.reply_text("Lower!")


async def ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_id}"
    data = load_data()
    count = data["referrals"].get(user_id, {}).get("count", 0)
    await update.message.reply_text(
        f"Your referral link:\n{link}\n\nInvited: *{count}*\nRegister wallet with /wallet 0xAddress",
        parse_mode="Markdown",
    )


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_ID:
        await update.message.reply_text("Admin only command.")
        return
    if not CHANNEL_ID:
        await update.message.reply_text("CHANNEL_ID not set.")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /post your text")
        return
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        await update.message.reply_text("Posted.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data
    session = context.application.bot_data["session"]

    if data_cb == "cmd:price":
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
        await query.edit_message_text("Token not found", reply_markup=main_keyboard())

    elif data_cb == "cmd:holders":
        count = await get_holders_count(session)
        try:
            await query.edit_message_text(f"Holders: *{count}*",
                parse_mode="Markdown", reply_markup=main_keyboard())
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    elif data_cb == "cmd:play":
        context.user_data["game_number"] = random.randint(1, 10)
        await query.message.reply_text("I picked a number from 1 to 10! Type a number.")

    elif data_cb == "cmd:ref":
        user_id = str(query.from_user.id)
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={user_id}"
        data_store = load_data()
        count = data_store["referrals"].get(user_id, {}).get("count", 0)
        await query.message.reply_text(f"Your link:\n{link}\n\nInvited: *{count}*", parse_mode="Markdown")


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
    except Exception as e:
        logger.error(f"Send error: {e}")


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
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("holders", holders_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("ref", ref_command))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guess_handler))

    if app.job_queue:
        app.job_queue.run_repeating(periodic_update, interval=UPDATE_INTERVAL, first=10)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()

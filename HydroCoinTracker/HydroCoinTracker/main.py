import http.client
import json
import time
import os
from datetime import datetime
from collections import defaultdict
import asyncio
import logging
from typing import Dict, List, Set, Optional

from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram import __version__ as TG_VER

try:
    from telegram import __version_info__
except ImportError:
    __version_info__ = (0, 0, 0, 0, 0)  # type: ignore[assignment]

if __version_info__ < (20, 0, 0, "alpha", 1):
    raise RuntimeError(
        f"This bot is not compatible with your current PTB version {TG_VER}. To upgrade use command: "
        f"`pip install python-telegram-bot --upgrade`"
    )

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
import traceback

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
TOKEN = "INSERT_TELEGRAM_BOT_TOKEN_HERE"  # Replace with your bot token
MAX_WALLETS_PER_USER = 5
SWAP_CHECK_INTERVAL = 10  # seconds

# Conversation states
ADDING_WALLET = 0
REMOVING_WALLET = 1

# User data structure - stores wallets per user
user_data = {}  # user_id -> {'wallets': [wallet1, wallet2], 'last_seen_swaps': {wallet1: [swap_ids]}}

# Set up data directory for Replit
DATA_DIR = os.path.join(os.getcwd(), "user_data")
os.makedirs(DATA_DIR, exist_ok=True)

def save_user_data():
    """Save all user data to disk"""
    data_file = os.path.join(DATA_DIR, "users.json")
    with open(data_file, "w") as f:
        json.dump(user_data, f, indent=2)

def load_user_data():
    """Load user data from disk"""
    global user_data
    try:
        data_file = os.path.join(DATA_DIR, "users.json")
        if os.path.exists(data_file):
            with open(data_file, "r") as f:
                user_data = json.load(f)
            logger.info(f"Loaded data for {len(user_data)} users")
    except Exception as e:
        logger.error(f"Error loading user data: {e}")
        user_data = {}

def fetch_transactions(address, row=50, page=0):
    """Fetch transactions from the Subscan API"""
    conn = http.client.HTTPSConnection("hydration.api.subscan.io")

    payload = json.dumps({
       "address": address,
       "row": row,
       "page": page,
       "direction": "all",
       "include_total": True,
       "success": True,
       "order": "desc"
    })

    headers = {
       'Content-Type': 'application/json'
    }

    try:
        conn.request("POST", "/api/v2/scan/transfers", payload, headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        logger.error(f"Error fetching transactions: {e}")
        return {"code": 1, "message": f"API Error: {str(e)}"}

def format_timestamp(timestamp):
    """Format unix timestamp to readable date/time"""
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    return "Unknown"

def create_swap_id(timestamp, address1, address2, token1, token2):
    """Create a unique ID for a swap transaction"""
    return f"{timestamp}_{address1}_{address2}_{token1}_{token2}"

def identify_swaps(transfers, target_address):
    """Identify swaps in transaction data"""
    # Group transactions by timestamp
    tx_by_time = defaultdict(list)
    for tx in transfers:
        tx_time = tx.get("block_timestamp")
        if tx_time:
            tx_by_time[tx_time].append(tx)

    # Identify swaps (sent and received at the same timestamp)
    swaps = []
    for timestamp, txs in tx_by_time.items():
        if len(txs) >= 2:  # Need at least 2 transactions for a swap
            sent = []
            received = []

            # Categorize transactions as sent or received
            for tx in txs:
                if tx.get("from") == target_address:
                    sent.append(tx)
                if tx.get("to") == target_address:
                    received.append(tx)

            # If we have both sent and received transactions at this timestamp, it's potentially a swap
            if sent and received:
                formatted_time = format_timestamp(timestamp)

                for s in sent:
                    for r in received:
                        # Create a unique identifier for this swap
                        sent_token = s.get("asset_symbol", "Unknown")
                        received_token = r.get("asset_symbol", "Unknown")

                        # Skip if it's the same token (transfer, not swap)
                        if sent_token == received_token:
                            continue

                        counterparty = s.get("to")

                        # Include both transactions in the swap record
                        swap = {
                            "time": formatted_time,
                            "raw_time": timestamp,
                            "sent": {
                                "token": sent_token,
                                "amount": s.get("amount", "Unknown"),
                                "to": s.get("to")
                            },
                            "received": {
                                "token": received_token,
                                "amount": r.get("amount", "Unknown"),
                                "from": r.get("from")
                            },
                            "counterparty": counterparty,
                            "swap_id": create_swap_id(timestamp, s.get("to"), r.get("from"), sent_token, received_token)
                        }
                        swaps.append(swap)

    return swaps

# Import all the command handlers and background task functions from the original file
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /start command"""
    user_id = update.effective_user.id
    init_user(user_id)

    await update.message.reply_text(
        "Welcome to the Swap Tracker Bot! 🔄\n\n"
        "I can monitor blockchain wallets and notify you about swap transactions.\n\n"
        "Commands:\n"
        "/add_wallet - Add a wallet to track (max 5)\n"
        "/remove_wallet - Remove a wallet\n"
        "/list_wallets - Show your tracked wallets\n"
        "/help - Show this help message"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /help command"""
    await update.message.reply_text(
        "Swap Tracker Bot Help 🔄\n\n"
        "I check for swap transactions in your wallets every 10 seconds.\n\n"
        "Commands:\n"
        "/add_wallet - Add a wallet to track (max 5)\n"
        "/remove_wallet - Remove a wallet\n"
        "/list_wallets - Show your tracked wallets\n"
        "/help - Show this help message"
    )

def init_user(user_id):
    """Initialize user data structure"""
    if str(user_id) not in user_data:
        user_data[str(user_id)] = {
            "wallets": [],
            "last_seen_swaps": {}
        }
        save_user_data()

def get_user_wallets(user_id):
    """Get list of wallets for a user"""
    user_id_str = str(user_id)
    if user_id_str in user_data and "wallets" in user_data[user_id_str]:
        return user_data[user_id_str]["wallets"]
    return []

def get_last_seen_swaps(user_id, wallet):
    """Get list of last seen swap IDs for a wallet"""
    user_id_str = str(user_id)
    # First validate that this wallet belongs to the user
    if wallet not in get_user_wallets(user_id):
        logger.warning(f"Attempted unauthorized access to wallet {wallet} by user {user_id}")
        return []

    if user_id_str in user_data and "last_seen_swaps" in user_data[user_id_str]:
        if wallet in user_data[user_id_str]["last_seen_swaps"]:
            data = user_data[user_id_str]["last_seen_swaps"][wallet]
            if isinstance(data, dict):
                return data.get("swap_ids", [])
            # Handle legacy data format
            return data if isinstance(data, list) else []
    return []

def update_last_seen_swaps(user_id, wallet, swap_ids):
    """Update the list of seen swap IDs for a wallet"""
    user_id_str = str(user_id)
    # Verify wallet ownership before updating
    if wallet not in get_user_wallets(user_id):
        logger.warning(f"Attempted unauthorized swap update for wallet {wallet} by user {user_id}")
        return False

    if user_id_str not in user_data:
        user_data[user_id_str] = {}
    if "last_seen_swaps" not in user_data[user_id_str]:
        user_data[user_id_str]["last_seen_swaps"] = {}
    user_data[user_id_str]["last_seen_swaps"][wallet] = swap_ids
    save_user_data()
    return True

async def add_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /add_wallet command"""
    user_id = update.effective_user.id
    wallets = get_user_wallets(user_id)

    if len(wallets) >= MAX_WALLETS_PER_USER:
        await update.message.reply_text(
            f"⚠️ You've reached the maximum limit of {MAX_WALLETS_PER_USER} wallets.\n"
            "Please remove a wallet before adding a new one using /remove_wallet."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Please send me the wallet address you want to track.\n"
    )

    return ADDING_WALLET

async def wallet_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wallet address input"""
    user_id = update.effective_user.id
    wallet = update.message.text.strip()

    # Validate wallet address (basic check - improve as needed)
    if len(wallet) < 30 or " " in wallet:
        await update.message.reply_text(
            "⚠️ This doesn't look like a valid wallet address.\n"
            "Please try again with a valid address."
        )
        return ADDING_WALLET

    success = add_wallet(user_id, wallet)

    if success:
        await update.message.reply_text(
            f"✅ Wallet added successfully!\n\n"
            f"Address: `{wallet}`\n\n",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "⚠️ This wallet is already in your tracking list or you've reached the maximum limit."
        )

    return ConversationHandler.END

def add_wallet(user_id, wallet):
    """Add a wallet to user's tracking list"""
    init_user(user_id)
    if wallet not in user_data[str(user_id)]["wallets"]:
        if len(user_data[str(user_id)]["wallets"]) < MAX_WALLETS_PER_USER:
            # Fetch current swaps to establish a baseline
            try:
                response_data = fetch_transactions(wallet)
                if response_data.get("code") == 0:
                    transfers = response_data.get("data", {}).get("transfers", [])
                    swaps = identify_swaps(transfers, wallet)
                    # Store all current swap IDs and the addition timestamp
                    user_data[str(user_id)]["last_seen_swaps"][wallet] = {
                        "swap_ids": [swap["swap_id"] for swap in swaps],
                        "added_at": int(time.time())
                    }
                else:
                    user_data[str(user_id)]["last_seen_swaps"][wallet] = {
                        "swap_ids": [],
                        "added_at": int(time.time())
                    }
            except Exception as e:
                logger.error(f"Error initializing swaps for wallet {wallet}: {e}")
                user_data[str(user_id)]["last_seen_swaps"][wallet] = {
                    "swap_ids": [],
                    "added_at": int(time.time())
                }

            # Add the wallet to the user's list
            user_data[str(user_id)]["wallets"].append(wallet)
            save_user_data()
            return True
    return False

async def remove_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /remove_wallet command"""
    user_id = update.effective_user.id
    wallets = get_user_wallets(user_id)

    if not wallets:
        await update.message.reply_text("You don't have any wallets in your tracking list.")
        return ConversationHandler.END

    keyboard = []
    for wallet in wallets:
        # Truncate wallet address for display
        short_wallet = wallet[:10] + "..." + wallet[-4:]
        keyboard.append([InlineKeyboardButton(short_wallet, callback_data=f"remove_{wallet}")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Select a wallet to remove from your tracking list:",
        reply_markup=reply_markup
    )

    return REMOVING_WALLET

async def wallet_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wallet removal selection"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    wallet = query.data.replace("remove_", "")

    success = remove_wallet(user_id, wallet)

    if success:
        await query.edit_message_text(
            f"✅ Wallet removed successfully!\n\n"
            f"Address: `{wallet}`",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            "⚠️ Error removing wallet. Please try again."
        )

    return ConversationHandler.END

def remove_wallet(user_id, wallet):
    """Remove a wallet from user's tracking list"""
    if str(user_id) in user_data and wallet in user_data[str(user_id)]["wallets"]:
        user_data[str(user_id)]["wallets"].remove(wallet)
        if wallet in user_data[str(user_id)]["last_seen_swaps"]:
            del user_data[str(user_id)]["last_seen_swaps"][wallet]
        save_user_data()
        return True
    return False

async def list_wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /list_wallets command"""
    user_id = update.effective_user.id
    wallets = get_user_wallets(user_id)

    if not wallets:
        await update.message.reply_text(
            "You don't have any wallets in your tracking list.\n"
            "Use /add_wallet to add a wallet."
        )
        return

    message = "Your tracked wallets:\n\n"
    for i, wallet in enumerate(wallets, 1):
        message += f"{i}. `{wallet}`\n"

    await update.message.reply_text(
        message,
        parse_mode='Markdown'
    )

# Background task to check for swaps
async def check_swaps_task(context: ContextTypes.DEFAULT_TYPE):
    """Background task to check for new swaps"""
    try:
        bot = context.bot

        for user_id_str, data in user_data.items():
            wallets = data.get("wallets", [])

            if not wallets:
                continue

            for wallet in wallets:
                try:
                    # Verify wallet belongs to user before processing
                    if wallet not in get_user_wallets(user_id_str):
                        logger.warning(f"Skipping unauthorized wallet {wallet} for user {user_id_str}")
                        continue

                    logger.info(f"Checking swaps for wallet {wallet} (user: {user_id_str})")
                    response_data = fetch_transactions(wallet)

                    if response_data.get("code") != 0:
                        logger.error(f"API error for wallet {wallet}: {response_data.get('message', 'Unknown error')}")
                        continue

                    transfers = response_data.get("data", {}).get("transfers", [])
                    if not transfers:
                        continue

                    # Identify swaps
                    swaps = identify_swaps(transfers, wallet)

                    # Get wallet data including addition timestamp
                    wallet_data = user_data[user_id_str]["last_seen_swaps"].get(wallet, {})
                    if isinstance(wallet_data, list):
                        # Handle legacy data format
                        last_seen_swap_ids = set(wallet_data)
                        added_at = 0  # Process all swaps for legacy data
                    else:
                        last_seen_swap_ids = set(wallet_data.get("swap_ids", []))
                        added_at = wallet_data.get("added_at", 0)

                    # Find new swaps that occurred after wallet was added
                    new_swaps = [
                        swap for swap in swaps 
                        if swap["swap_id"] not in last_seen_swap_ids 
                        and swap["raw_time"] > added_at
                    ]

                    # Update last seen swaps
                    if swaps:
                        current_ids = [swap["swap_id"] for swap in swaps]
                        if isinstance(wallet_data, dict):
                            wallet_data["swap_ids"] = current_ids
                        else:
                            # Convert to new format
                            wallet_data = {
                                "swap_ids": current_ids,
                                "added_at": added_at
                            }
                        # Only update if the wallet still belongs to the user
                        if update_last_seen_swaps(user_id_str, wallet, wallet_data):
                            logger.info(f"Updated swap data for wallet {wallet} (user: {user_id_str})")

                    # Send notifications for new swaps
                    if new_swaps:
                        for swap in new_swaps:
                            message = (
                                f"🔄 New Swap Detected!\n\n"
                                f"Wallet: `{wallet}`\n"
                                f"Time: {swap['time']}\n"
                                f"Swapped {swap['sent']['amount']} {swap['sent']['token']} for {swap['received']['amount']} {swap['received']['token']}"
                            )

                            await bot.send_message(
                                chat_id=int(user_id_str),
                                text=message,
                                parse_mode='Markdown'
                            )

                except Exception as e:
                    logger.error(f"Error checking swaps for wallet {wallet}: {str(e)}")
                    logger.error(traceback.format_exc())
    except Exception as e:
        logger.error(f"Error in check_swaps_task: {str(e)}")
        logger.error(traceback.format_exc())

def main():
    """Start the bot"""
    # Load existing user data
    load_user_data()

    # Create the Application
    application = Application.builder().token(TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list_wallets", list_wallets_command))

    # Add conversation handlers
    add_wallet_handler = ConversationHandler(
        entry_points=[CommandHandler("add_wallet", add_wallet_command)],
        states={
            ADDING_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_added)]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    application.add_handler(add_wallet_handler)

    remove_wallet_handler = ConversationHandler(
        entry_points=[CommandHandler("remove_wallet", remove_wallet_command)],
        states={
            REMOVING_WALLET: [CallbackQueryHandler(wallet_remove_callback, pattern=r"^remove_")]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    application.add_handler(remove_wallet_handler)

    # Create a separate thread for checking swaps
    import threading

    def check_swaps_loop():
        """Background thread to periodically check for new swaps"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        class DummyContext:
            def __init__(self):
                self.bot = application.bot

        context = DummyContext()

        while True:
            try:
                # Run the check_swaps_task in the loop
                loop.run_until_complete(check_swaps_task(context))
                # Wait before checking again
                time.sleep(SWAP_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in check_swaps_loop: {str(e)}")
                logger.error(traceback.format_exc())
                # Wait a bit before retrying after an error
                time.sleep(5)

    # Start the background thread
    swap_thread = threading.Thread(target=check_swaps_loop, daemon=True)
    swap_thread.start()

    logger.info("Bot started, monitoring for swaps...")

    # Start the Bot
    application.run_polling()

if __name__ == "__main__":
    main()
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================

TOKEN = 8957137062:AAE96L_pX20X5OtQiaW45a8U7kqEKo9-Xlo


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("123456", callback_data="scan_6"),
            InlineKeyboardButton("1234567", callback_data="scan_7"),
        ],
        [
            InlineKeyboardButton("8 Digits", callback_data="scan_8"),
            InlineKeyboardButton("a-z", callback_data="scan_lower"),
        ],
        [
            InlineKeyboardButton("ALL", callback_data="scan_all"),
        ],
    ]

    text = (
        "🤖 Demo Bot\n\n"
        "အောက်က option တစ်ခုရွေးပါ 👇\n\n"
        "⚠️ ဒီ bot က Demo/Mock simulation သာဖြစ်ပြီး "
        "တကယ့် account/API ကို စမ်းသပ်ခြင်းမပြုပါ။"
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# DEMO SCAN
# =========================

async def scan_demo(query, mode):
    total = 10000

    await query.edit_message_text(
        f"🔎 Demo Scan Started\n\n"
        f"Mode: {mode}\n"
        f"Checked: 0/{total}\n"
        f"Progress: 0%\n"
        f"Found: 0\n\n"
        f"⚠️ Mock simulation only."
    )

    for checked in range(1000, total + 1, 1000):
        progress = int((checked / total) * 100)

        await asyncio.sleep(0.4)

        await query.edit_message_text(
            f"🔎 Demo Scan Running...\n\n"
            f"Mode: {mode}\n"
            f"Checked: {checked:,}/{total:,}\n"
            f"Progress: {progress}%\n"
            f"Speed: Demo\n"
            f"Found: 0\n"
            f"Retry: 0\n\n"
            f"⚠️ Mock simulation only."
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Scan Again", callback_data="back")],
    ]

    await query.edit_message_text(
        f"✅ Demo Scan Finished\n\n"
        f"Mode: {mode}\n"
        f"Checked: {total:,}/{total:,}\n"
        f"Progress: 100%\n"
        f"Found: 0\n"
        f"Retry: 0\n\n"
        f"⚠️ ဒီရလဒ်က Demo/Mock result သာဖြစ်ပါတယ်။",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# BUTTON HANDLER
# =========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        keyboard = [
            [
                InlineKeyboardButton("123456", callback_data="scan_6"),
                InlineKeyboardButton("1234567", callback_data="scan_7"),
            ],
            [
                InlineKeyboardButton("8 Digits", callback_data="scan_8"),
                InlineKeyboardButton("a-z", callback_data="scan_lower"),
            ],
            [
                InlineKeyboardButton("ALL", callback_data="scan_all"),
            ],
        ]

        await query.edit_message_text(
            "🤖 Demo Bot\n\nOption တစ်ခုရွေးပါ 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    modes = {
        "scan_6": "6 Digits",
        "scan_7": "7 Digits",
        "scan_8": "8 Digits",
        "scan_lower": "Lowercase a-z",
        "scan_all": "ALL",
    }

    mode = modes.get(query.data)

    if mode:
        await scan_demo(query, mode)


# =========================
# MAIN
# =========================

def main():
    if TOKEN == 8957137062:AAE96L_pX20X5OtQiaW45a8U7kqEKo9-Xlo:
        print("ERROR: 8957137062:AAE96L_pX20X5OtQiaW45a8U7kqEKo9-Xlo")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

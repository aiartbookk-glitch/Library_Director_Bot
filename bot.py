import telebot
import os
import json
import time
import secrets

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

DATA_FILE = "data.json"

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()
    data = load_data()

    if len(args) == 1:
        bot.reply_to(message, "Send /upload to upload files.")
    else:
        media_id = args[1]

        if media_id not in data:
            bot.reply_to(message, "Link not found.")
            return

        entry = data[media_id]

        entry["views"] += 1
        save_data(data)

        for f in entry["files"]:
            bot.copy_message(
                message.chat.id,
                entry["channel_id"],
                f,
                protect_content=True
            )

@bot.message_handler(commands=['upload'])
def upload(message):
    media_id = secrets.token_urlsafe(8)
    bot.reply_to(message, "Send files now. Type DONE when finished.")
    bot.register_next_step_handler(message, handle_files, media_id, [])

def handle_files(message, media_id, files):
    if message.text == "DONE":
        data = load_data()
        data[media_id] = {
            "files": files,
            "views": 0,
            "channel_id": message.chat.id
        }
        save_data(data)

        link = f"https://t.me/{bot.get_me().username}?start={media_id}"
        bot.send_message(message.chat.id, f"Upload complete:\n{link}")
        return

    if message.content_type in ["photo", "video", "document"]:
        files.append(message.message_id)
        bot.reply_to(message, "Saved.")
    else:
        bot.reply_to(message, "Send media only.")

    bot.register_next_step_handler(message, handle_files, media_id, files)

print("Bot running...")
bot.infinity_polling()

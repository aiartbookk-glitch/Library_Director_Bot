import telebot
import json
import secrets
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument
)

TOKEN = "YOUR_TOKEN"
bot = telebot.TeleBot(TOKEN)
BOT_USERNAME = bot.get_me().username

DATA_FILE = "data.json"
FORCE_FILE = "force_channels.json"

upload_sessions = {}
force_setup_mode = set()


# ================= DATA =================

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def load_force_channels():
    try:
        with open(FORCE_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_force_channels(data):
    with open(FORCE_FILE, "w") as f:
        json.dump(data, f)


# ================= FORCE SETUP =================

@bot.message_handler(commands=['setforce'])
def enable_force_setup(message):
    force_setup_mode.add(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Forward a message from the channel you want to force."
    )


@bot.message_handler(func=lambda m: m.from_user.id in force_setup_mode and m.forward_from_chat is not None)
def save_force_channel(message):

    if message.forward_from_chat.type != "channel":
        bot.send_message(message.chat.id, "Forward from a channel only.")
        return

    channel_id = message.forward_from_chat.id
    channels = load_force_channels()

    if channel_id not in channels:
        channels.append(channel_id)
        save_force_channels(channels)

    force_setup_mode.remove(message.from_user.id)

    bot.send_message(
        message.chat.id,
        f"✅ Channel added to force list.\nID: {channel_id}"
    )


# ================= CHECK JOIN =================

def is_joined(user_id):
    channels = load_force_channels()

    for channel in channels:
        try:
            member = bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True


def join_required_markup(media_id):
    channels = load_force_channels()
    markup = InlineKeyboardMarkup()

    for channel in channels:
        chat = bot.get_chat(channel)
        invite_link = chat.invite_link

        if not invite_link:
            invite_link = bot.export_chat_invite_link(channel)

        markup.add(
            InlineKeyboardButton(
                f"📢 Join {chat.title}",
                url=invite_link
            )
        )

    markup.add(
        InlineKeyboardButton(
            "✅ I've Joined",
            callback_data=f"check_{media_id}"
        )
    )

    return markup


# ================= START =================

@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()
    data = load_data()

    if len(args) == 1:
        bot.send_message(message.chat.id, "Welcome!")
        return

    media_id = args[1]

    if media_id not in data:
        bot.send_message(message.chat.id, "Link not found.")
        return

    if not is_joined(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "🚫 You must join required channels.",
            reply_markup=join_required_markup(media_id)
        )
        return

    send_files(message.chat.id, media_id)


# ================= SEND FILES =================

def send_files(chat_id, media_id):
    data = load_data()
    entry = data[media_id]

    entry["views"] += 1
    save_data(data)

    media_list = []

    for item in entry["files"]:
        if item["type"] == "photo":
            media_list.append(InputMediaPhoto(item["file_id"]))
        elif item["type"] == "video":
            media_list.append(InputMediaVideo(item["file_id"]))
        elif item["type"] == "document":
            media_list.append(InputMediaDocument(item["file_id"]))

    if len(media_list) == 1:
        item = entry["files"][0]

        if item["type"] == "photo":
            bot.send_photo(chat_id, item["file_id"], protect_content=True)
        elif item["type"] == "video":
            bot.send_video(chat_id, item["file_id"], protect_content=True)
        elif item["type"] == "document":
            bot.send_document(chat_id, item["file_id"], protect_content=True)
    else:
        for i in range(0, len(media_list), 10):
            chunk = media_list[i:i+10]
            bot.send_media_group(chat_id, chunk, protect_content=True)


# ================= CHECK BUTTON =================

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_"))
def check_join(call):
    media_id = call.data.split("_")[1]

    if is_joined(call.from_user.id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        send_files(call.message.chat.id, media_id)
    else:
        bot.answer_callback_query(call.id, "Join all channels first.", show_alert=True)


print("Bot running...")
bot.infinity_polling()

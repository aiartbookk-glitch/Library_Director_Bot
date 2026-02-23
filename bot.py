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

TOKEN = "8287739944:AAHp-OIJEpGoIEqt6iBiL1DbKnYYE8Lq3i0"
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


# ================= FORCE MANAGEMENT =================

@bot.message_handler(commands=['setforce'])
def enable_force_setup(message):
    force_setup_mode.add(message.from_user.id)
    bot.send_message(message.chat.id, "Forward a message from the channel to add.")

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
    bot.send_message(message.chat.id, f"✅ Added force channel:\n{channel_id}")


@bot.message_handler(commands=['listforce'])
def list_force(message):
    channels = load_force_channels()

    if not channels:
        bot.send_message(message.chat.id, "No force channels set.")
        return

    text = "📢 Force Channels:\n\n"
    for ch in channels:
        try:
            chat = bot.get_chat(ch)
            text += f"{chat.title} ({ch})\n"
        except:
            text += f"Invalid Channel ({ch})\n"

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=['removeforce'])
def remove_force(message):
    args = message.text.split()

    if len(args) != 2:
        bot.send_message(message.chat.id, "Usage: /removeforce CHANNEL_ID")
        return

    channel_id = int(args[1])
    channels = load_force_channels()

    if channel_id in channels:
        channels.remove(channel_id)
        save_force_channels(channels)
        bot.send_message(message.chat.id, "✅ Xóa.")
    else:
        bot.send_message(message.chat.id, "Channel not found.")


# ================= SAFE CHECK JOIN =================

def is_joined(user_id):
    channels = load_force_channels()
    updated = []
    all_joined = True

    for ch in channels:
        try:
            member = bot.get_chat_member(ch, user_id)

            if member.status in ["left", "kicked"]:
                all_joined = False

            updated.append(ch)

        except:
            print("Removed invalid force channel:", ch)

    if len(updated) != len(channels):
        save_force_channels(updated)

    return all_joined


def join_required_markup(media_id):
    channels = load_force_channels()
    markup = InlineKeyboardMarkup()

    for ch in channels:
        try:
            chat = bot.get_chat(ch)
            invite = chat.invite_link or bot.export_chat_invite_link(ch)

            markup.add(
                InlineKeyboardButton(
                    f"📢 Join {chat.title}",
                    url=invite
                )
            )
        except:
            continue

    markup.add(
        InlineKeyboardButton(
            "✅ Kiểm tra đã tham gia đủ",
            callback_data=f"check_{media_id}"
        )
    )

    return markup


# ================= MENU =================

def main_menu():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📤 Upload File", callback_data="upload"))
    markup.add(InlineKeyboardButton("📊 My Links", callback_data="mylinks"))
    return markup


# ================= START =================

@bot.message_handler(commands=['start'])
def start(message):

    text = message.text
    media_id = None

    if text.startswith("/start "):
        media_id = text.replace("/start ", "").strip()

    data = load_data()

    if not media_id:
        bot.send_message(message.chat.id, "Welcome!", reply_markup=main_menu())
        return

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


# ================= SEND FILES (ALBUM LOGIC GIỮ NGUYÊN) =================

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


# ================= CALLBACK =================

@bot.callback_query_handler(func=lambda call: True)
def callback(call):

    if call.data.startswith("check_"):
        media_id = call.data.split("_")[1]

        if is_joined(call.from_user.id):
            bot.delete_message(call.message.chat.id, call.message.message_id)
            send_files(call.message.chat.id, media_id)
        else:
            bot.answer_callback_query(call.id, "Join all channels first.", show_alert=True)

    elif call.data == "upload":

        media_id = secrets.token_urlsafe(8)

        upload_sessions[call.from_user.id] = {
            "media_id": media_id,
            "files": []
        }

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Finish Upload", callback_data="finish"))

        bot.edit_message_text(
            "Send files now.\nPress Finish when done.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )

    elif call.data == "finish":

        user_id = call.from_user.id

        if user_id not in upload_sessions or not upload_sessions[user_id]["files"]:
            bot.answer_callback_query(call.id, "No files uploaded.")
            return

        upload_sessions[user_id]["waiting_name"] = True

        bot.edit_message_text(
            "Enter name for this link:",
            call.message.chat.id,
            call.message.message_id
        )

    elif call.data == "mylinks":

        data = load_data()
        user_id = call.from_user.id

        text = "📊 Your Links:\n\n"
        found = False

        for media_id, info in data.items():
            if info.get("owner") == user_id:
                found = True
                link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

                text += f"{info.get('name')}\n{link}\nViews: {info['views']}\nFiles: {len(info['files'])}\n\n"

        markup = InlineKeyboardMarkup()

        if found:
            markup.add(InlineKeyboardButton("🗑 Reset All", callback_data="reset_all"))
        else:
            text = "You have no links yet."

        markup.add(InlineKeyboardButton("⬅ Back", callback_data="back_menu"))

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            disable_web_page_preview=True
        )

    elif call.data == "reset_all":

        data = load_data()
        user_id = call.from_user.id

        new_data = {k: v for k, v in data.items() if v.get("owner") != user_id}
        save_data(new_data)

        bot.edit_message_text(
            "🗑 All links deleted.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu()
        )

    elif call.data == "back_menu":
        bot.edit_message_text(
            "Welcome!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu()
        )


# ================= RECEIVE NAME =================

@bot.message_handler(func=lambda m: m.from_user.id in upload_sessions and upload_sessions[m.from_user.id].get("waiting_name"))
def receive_name(message):

    user_id = message.from_user.id
    session = upload_sessions[user_id]

    link_name = message.text.strip()
    media_id = session["media_id"]

    data = load_data()

    data[media_id] = {
        "owner": user_id,
        "name": link_name,
        "files": session["files"],
        "views": 0
    }

    save_data(data)

    link = f"https://t.me/{BOT_USERNAME}?start={media_id}"

    bot.send_message(
        message.chat.id,
        f"✅ Upload Complete!\n{link}",
        disable_web_page_preview=True
    )

    del upload_sessions[user_id]


# ================= HANDLE MEDIA =================

@bot.message_handler(content_types=['photo', 'video', 'document'])
def handle_media(message):

    user_id = message.from_user.id

    if user_id not in upload_sessions:
        return

    if message.photo:
        upload_sessions[user_id]["files"].append({
            "type": "photo",
            "file_id": message.photo[-1].file_id
        })
    elif message.video:
        upload_sessions[user_id]["files"].append({
            "type": "video",
            "file_id": message.video.file_id
        })
    elif message.document:
        upload_sessions[user_id]["files"].append({
            "type": "document",
            "file_id": message.document.file_id
        })


print("Bot running...")
bot.infinity_polling()

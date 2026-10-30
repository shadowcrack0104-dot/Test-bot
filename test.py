# -*- coding: utf-8 -*-
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import logging
import sys
import threading
from flask import Flask
from dotenv import load_dotenv

# --- Load environment variables ---
load_dotenv()

TOKEN = os.getenv('BOT_TOKEN', '8620671084:AAHi_Za_rDVYdPwhzH66pT-kj8E5wXD53gI')
OWNER_ID = int(os.getenv('OWNER_ID', 7986731915))
ADMIN_ID = int(os.getenv('ADMIN_ID', 7986731915))

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Flask Keep Alive ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Alive!"
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- Directories & Database ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DB_PATH = os.path.join(BASE_DIR, 'bot_data.db')
os.makedirs(UPLOAD_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# --- Global Variables ---
bot_locked = False
admin_ids = {OWNER_ID, ADMIN_ID}
pending_files = {} # {file_id: {'user_id': id, 'file_name': name, 'path': path, 'type': type, 'msg_id': admin_msg_id}}

# --- Database Initialization ---
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_banned INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, channel_link TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS files (user_id INTEGER, file_name TEXT, file_type TEXT)''')
    
    # Insert default owner
    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
    if ADMIN_ID != OWNER_ID:
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
    conn.commit()
    conn.close()

init_db()

def is_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None or user_id == OWNER_ID

def is_banned(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
    res = c.fetchone()
    conn.close()
    return res and res[0] == 1

def add_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def get_channels():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT channel_id, channel_link FROM channels')
    res = c.fetchall()
    conn.close()
    return res

def check_subscription(user_id):
    if user_id == OWNER_ID or is_admin(user_id):
        return True, []
    
    channels = get_channels()
    if not channels:
        return True, []
    
    not_joined = []
    for ch_id, ch_link in channels:
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append((ch_id, ch_link))
        except Exception:
            not_joined.append((ch_id, ch_link))
    
    return len(not_joined) == 0, not_joined

def get_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if is_admin(user_id):
        markup.add("📤 Upload File", "📂 My Files")
        markup.add("📊 Statistics", "📢 Broadcast")
        markup.add("👥 User Management", "👑 Admin Management")
        markup.add("📢 Channels Setup", "🔒 Lock/Unlock Bot")
    else:
        markup.add("📤 Upload File", "📂 My Files")
        markup.add("⚡ Bot Status")
    return markup

# --- Start Command ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 You are banned from using this bot.")
        return
    
    add_user(user_id)
    
    is_sub, not_joined = check_subscription(user_id)
    if not is_sub:
        markup = types.InlineKeyboardMarkup()
        for ch_id, ch_link in not_joined:
            markup.add(types.InlineKeyboardButton("📢 Join Channel", url=ch_link))
        markup.add(types.InlineKeyboardButton("✅ Verify Joined", callback_data="verify_sub"))
        bot.send_message(message.chat.id, "⚠️ **You must join our channels to use this bot!**", reply_markup=markup, parse_mode='Markdown')
        return
        
    bot.send_message(message.chat.id, f"👋 Welcome {message.from_user.first_name}!\n\n🤖 I am a Shadow Hosting Bot.\n📤 Upload your Python (.py) or JS (.js) files to run.", reply_markup=get_main_menu(user_id))

@bot.callback_query_handler(func=lambda call: call.data == 'verify_sub')
def verify_sub(call):
    is_sub, not_joined = check_subscription(call.from_user.id)
    if is_sub:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "✅ Verification successful!", reply_markup=get_main_menu(call.from_user.id))
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined all channels yet!", show_alert=True)

# --- Upload File ---
@bot.message_handler(func=lambda msg: msg.text == "📤 Upload File" or msg.text == "/upload")
def prompt_upload(message):
    if bot_locked and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔒 Bot is locked by Admin.")
        return
    bot.reply_to(message, "📤 Send your `.py` or `.js` file now.\n_Security Check will be applied._", parse_mode='Markdown')

@bot.message_handler(content_types=['document'])
def handle_file(message):
    user_id = message.from_user.id
    if is_banned(user_id) or (bot_locked and not is_admin(user_id)):
        return

    doc = message.document
    file_name = doc.file_name
    ext = os.path.splitext(file_name)[1].lower()

    if ext not in ['.py', '.js']:
        bot.reply_to(message, "❌ Only .py and .js files are allowed.")
        return

    bot.reply_to(message, "⏳ Downloading and checking file...")
    
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        
        user_dir = os.path.join(UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, file_name)
        
        with open(file_path, 'wb') as f:
            f.write(downloaded)
            
        # Send to all Admins for Approval
        admin_markup = types.InlineKeyboardMarkup()
        admin_markup.add(
            types.InlineKeyboardButton("✅ Approve & Run", callback_data=f"app_{user_id}_{file_name}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user_id}_{file_name}")
        )
        
        pending_files[f"{user_id}_{file_name}"] = {
            'path': file_path, 'type': ext[1:], 'user_id': user_id, 'name': file_name
        }
        
        admin_msg = f"⚠️ **New File Uploaded**\n\n👤 User: {message.from_user.first_name} (`{user_id}`)\n📁 File: `{file_name}`\n\nApprove to run this script."
        
        for adm in admin_ids:
            try:
                bot.send_message(adm, admin_msg, reply_markup=admin_markup, parse_mode='Markdown')
            except: pass
            
        bot.reply_to(message, "✅ File uploaded successfully! Waiting for Admin approval to run.")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

# --- Admin Approve/Reject ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('app_') or call.data.startswith('rej_'))
def handle_approval(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Admin only!")
        return
        
    action, user_id, file_name = call.data.split('_', 2)
    user_id = int(user_id)
    key = f"{user_id}_{file_name}"
    
    if action == 'app':
        if key in pending_files:
            file_data = pending_files[key]
            bot.edit_message_text(f"✅ Approved `{file_name}`. Starting process...", call.message.chat.id, call.message.message_id)
            start_script(file_data['path'], file_data['type'], user_id, call.message)
            del pending_files[key]
        else:
            bot.answer_callback_query(call.id, "File not found in pending list.")
            
    elif action == 'rej':
        if key in pending_files:
            try: os.remove(pending_files[key]['path'])
            except: pass
            del pending_files[key]
        bot.edit_message_text(f"❌ Rejected `{file_name}`.", call.message.chat.id, call.message.message_id)
        try: bot.send_message(user_id, f"❌ Your file `{file_name}` was rejected by Admin.")
        except: pass

# --- Run Script ---
def start_script(file_path, file_type, user_id, msg_obj):
    try:
        if file_type == 'py':
            cmd = [sys.executable, file_path]
        elif file_type == 'js':
            cmd = ['node', file_path]
            
        log_file = open(file_path + ".log", "w")
        
        # Run in background
        subprocess.Popen(cmd, stdout=log_file, stderr=log_file, cwd=os.path.dirname(file_path))
        
        bot.send_message(msg_obj.chat.id, f"🚀 Script `{os.path.basename(file_path)}` is now running!")
        
        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM files WHERE user_id=? AND file_name=?", (user_id, os.path.basename(file_path)))
        c.execute("INSERT INTO files (user_id, file_name, file_type) VALUES (?, ?, ?)", (user_id, os.path.basename(file_path), file_type))
        conn.commit()
        conn.close()
        
    except Exception as e:
        bot.send_message(msg_obj.chat.id, f"❌ Failed to run: {e}")

# --- My Files ---
@bot.message_handler(func=lambda msg: msg.text == "📂 My Files")
def my_files(message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_name, file_type FROM files WHERE user_id=?", (user_id,))
    files = c.fetchall()
    conn.close()
    
    if not files:
        bot.reply_to(message, "📂 You have no running files.")
        return
        
    text = "📂 **Your Files:**\n\n"
    for f_name, f_type in files:
        text += f"• `{f_name}` ({f_type})\n"
    bot.reply_to(message, text, parse_mode='Markdown')

# --- Admin Features ---
@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.from_user.id))
def broadcast_prompt(message):
    msg = bot.reply_to(message, "📢 Send the message you want to broadcast to all users.")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(message):
    if not is_admin(message.from_user.id): return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    
    success, failed = 0, 0
    status_msg = bot.reply_to(message, "⏳ Broadcasting started...")
    
    for (u_id,) in users:
        try:
            bot.copy_message(u_id, message.chat.id, message.message_id)
            success += 1
        except:
            failed += 1
            
    bot.edit_message_text(f"✅ Broadcast Finished!\n\nSuccess: {success}\nFailed: {failed}", message.chat.id, status_msg.message_id)

@bot.message_handler(func=lambda msg: msg.text == "👥 User Management" and is_admin(msg.from_user.id))
def user_manage(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🚫 Ban User", callback_data="ban_u"),
        types.InlineKeyboardButton("✅ Unban User", callback_data="unban_u")
    )
    bot.send_message(message.chat.id, "👥 **User Management**\nSelect an action:", reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data in ['ban_u', 'unban_u'])
def handle_ban_unban(call):
    action = "Ban" if call.data == 'ban_u' else "Unban"
    msg = bot.send_message(call.message.chat.id, f"Send the User ID to {action}:")
    bot.register_next_step_handler(msg, process_ban_unban, action)

def process_ban_unban(message, action):
    try:
        target_id = int(message.text)
        ban_val = 1 if action == "Ban" else 0
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned=? WHERE user_id=?", (ban_val, target_id))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ User `{target_id}` has been {action}ned.")
    except:
        bot.reply_to(message, "❌ Invalid User ID.")

@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Management" and is_admin(msg.from_user.id))
def admin_manage(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only Owner can manage Admins.")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Add Admin", callback_data="add_adm"),
        types.InlineKeyboardButton("➖ Remove Admin", callback_data="rem_adm")
    )
    bot.send_message(message.chat.id, "👑 **Admin Management**", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['add_adm', 'rem_adm'])
def handle_adm_mng(call):
    action = "Add" if call.data == 'add_adm' else "Remove"
    msg = bot.send_message(call.message.chat.id, f"Send the User ID to {action} as Admin:")
    bot.register_next_step_handler(msg, process_adm_mng, action)

def process_adm_mng(message, action):
    try:
        target_id = int(message.text)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if action == "Add":
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target_id,))
            admin_ids.add(target_id)
        else:
            if target_id == OWNER_ID:
                bot.reply_to(message, "❌ Cannot remove Owner.")
                return
            c.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
            admin_ids.discard(target_id)
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ User `{target_id}` {action}ed as Admin.")
    except:
        bot.reply_to(message, "❌ Invalid User ID.")

@bot.message_handler(func=lambda msg: msg.text == "📢 Channels Setup" and is_admin(msg.from_user.id))
def channel_setup(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Add Channel", callback_data="add_ch"),
        types.InlineKeyboardButton("🗑️ Remove Channel", callback_data="rem_ch")
    )
    bot.send_message(message.chat.id, "📢 **Channel Setup**\nBot must be Admin in the channel.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'add_ch')
def add_ch_prompt(call):
    msg = bot.send_message(call.message.chat.id, "Send Channel ID and Link separated by space.\nExample: `-100123456789 https://t.me/yourchannel`")
    bot.register_next_step_handler(msg, process_add_ch)

def process_add_ch(message):
    try:
        ch_id, ch_link = message.text.split()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO channels (channel_id, channel_link) VALUES (?, ?)", (ch_id, ch_link))
        conn.commit()
        conn.close()
        bot.reply_to(message, "✅ Channel added successfully!")
    except:
        bot.reply_to(message, "❌ Invalid format. Use: `-100xxx https://t.me/link`")

@bot.callback_query_handler(func=lambda call: call.data == 'rem_ch')
def rem_ch_prompt(call):
    msg = bot.send_message(call.message.chat.id, "Send Channel ID to remove:")
    bot.register_next_step_handler(msg, process_rem_ch)

def process_rem_ch(message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channels WHERE channel_id=?", (message.text,))
    conn.commit()
    conn.close()
    bot.reply_to(message, "✅ Channel removed (if existed).")

@bot.message_handler(func=lambda msg: msg.text == "🔒 Lock/Unlock Bot" and is_admin(msg.from_user.id))
def lock_bot(message):
    global bot_locked
    bot_locked = not bot_locked
    status = "Locked 🔒" if bot_locked else "Unlocked 🔓"
    bot.reply_to(message, f"Bot is now {status}.")

@bot.message_handler(func=lambda msg: msg.text == "📊 Statistics" and is_admin(msg.from_user.id))
def stats(message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM files")
    files_count = c.fetchone()[0]
    conn.close()
    
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    
    text = f"📊 **Bot Statistics**\n\n👥 Total Users: {users_count}\n📁 Running Files: {files_count}\n💻 CPU: {cpu}%\n🧠 RAM: {ram}%\n🔒 Bot Status: {'Locked' if bot_locked else 'Unlocked'}"
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text == "⚡ Bot Status")
def bot_status(message):
    bot.reply_to(message, "✅ Bot is online and running smoothly!")

# --- Main Execution ---
if __name__ == '__main__':
    keep_alive()
    logger.info("Bot Started...")
    bot.infinity_polling()
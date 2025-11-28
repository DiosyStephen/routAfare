#!/usr/bin/env python3
"""
routAfare_bot_v3.py
UI Overhaul: Role-based Menus, Icon Interfaces, and Service Management
"""

import os
import json
import re
import sys
import time
from datetime import datetime, timedelta

# Third-party libraries
try:
    from dotenv import load_dotenv
    from flask import Flask, request
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
    import pandas as pd
    
    # Vertex AI / Google Cloud Imports (kept from V2)
    try:
        from google.cloud import aiplatform
        from google.oauth2 import service_account
    except ImportError:
        aiplatform = None
except ImportError as e:
    print(f"CRITICAL: Missing required package. Error: {e}")
    sys.exit(1)

load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBHOOK_URL_BASE = os.getenv('WEBHOOK_URL_BASE')
WEBHOOK_URL_PATH = os.getenv('WEBHOOK_URL_PATH', '/')
SERVER_PORT = int(os.getenv('PORT', 8080))

# Files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE = os.path.join(BASE_DIR, 'sessions.json')
SERVICES_FILE = os.path.join(BASE_DIR, 'services.json') # New file for provider data
CSV_FILE_NAME = os.getenv('CSV_FILE_NAME', 'final routa dataset for bus routes.csv')
CSV_FILE_PATH = os.path.join(BASE_DIR, CSV_FILE_NAME)

PROVIDER_PASSWORD = "admin" # Simple password for providers

# --- Data Persistence Helpers ---
def safe_write_json(file_path, data):
    try:
        with open(file_path, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {file_path}: {e}")

def safe_read_json(file_path, fallback):
    if not os.path.exists(file_path):
        return fallback
    try:
        with open(file_path, 'r', encoding='utf8') as f:
            return json.loads(f.read().strip() or "{}")
    except Exception:
        return fallback

sessions = safe_read_json(SESSIONS_FILE, {})
# Load dynamic services (added by providers)
services_db = safe_read_json(SERVICES_FILE, {"services": []})

def save_sessions(): safe_write_json(SESSIONS_FILE, sessions)
def save_services(): safe_write_json(SERVICES_FILE, services_db)

def clear_session(chat_id):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

# --- Data Loading (CSV + JSON) ---
# We now merge CSV data with Provider Added data
def get_all_routes():
    # 1. CSV Routes
    csv_routes = set()
    if os.path.exists(CSV_FILE_PATH):
        try:
            df = pd.read_csv(CSV_FILE_PATH)
            if 'bus_route' in df.columns:
                csv_routes = set(df['bus_route'].dropna().unique())
        except Exception: pass
    
    # 2. JSON Routes (Provider added)
    json_routes = {s['route'] for s in services_db['services']}
    
    return sorted(list(csv_routes.union(json_routes)))

# --- Bot Initialization ---
bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# --- KEYBOARDS & UI HELPERS ---

def main_menu_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("👤 Passenger", callback_data="role_passenger"),
        InlineKeyboardButton("🚌 Bus Provider", callback_data="role_provider")
    )
    return markup

def provider_menu_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("➕ Add New Service", callback_data="prov_add"),
        InlineKeyboardButton("✏️ Update Service", callback_data="prov_update"),
        InlineKeyboardButton("🗑️ Delete / Status", callback_data="prov_status"),
        InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")
    )
    return markup

def passenger_count_keyboard():
    markup = InlineKeyboardMarkup(row_width=3)
    btns = [InlineKeyboardButton(str(i), callback_data=f"pass_count_{i}") for i in range(1, 7)]
    markup.add(*btns)
    markup.add(InlineKeyboardButton("7+", callback_data="pass_count_7+"))
    return markup

def payment_toggle_keyboard(current_selection):
    # current_selection is a list e.g. ['weekly']
    w_status = "✅" if "weekly" in current_selection else "⬜"
    m_status = "✅" if "monthly" in current_selection else "⬜"
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(f"{w_status} Weekly", callback_data="toggle_pay_weekly"),
        InlineKeyboardButton(f"{m_status} Monthly", callback_data="toggle_pay_monthly")
    )
    markup.add(InlineKeyboardButton("💾 Save & Finish", callback_data="prov_save_service"))
    return markup

# --- HANDLERS ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.send_message(
        message.chat.id, 
        "👋 **Welcome to RoutAfare!**\n\nPlease select your role:",
        reply_markup=main_menu_keyboard(),
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = str(message.chat.id)
    text = message.text.strip()
    
    if chat_id not in sessions:
        send_welcome(message)
        return

    step = sessions[chat_id].get('step')

    # --- PROVIDER AUTH ---
    if step == 'provider_auth':
        if text == PROVIDER_PASSWORD:
            sessions[chat_id]['step'] = 'provider_menu'
            save_sessions()
            bot.send_message(int(chat_id), "🔓 **Access Granted**\nManage your fleet:", reply_markup=provider_menu_keyboard(), parse_mode='Markdown')
        else:
            bot.send_message(int(chat_id), "❌ Wrong Password. Try again or /start")

    # --- PROVIDER ADDING DATA FLOW ---
    elif step == 'prov_enter_route':
        sessions[chat_id]['temp_service']['route'] = text
        sessions[chat_id]['step'] = 'prov_enter_driver'
        save_sessions()
        bot.send_message(int(chat_id), "👤 Enter **Driver Name**:")

    elif step == 'prov_enter_driver':
        sessions[chat_id]['temp_service']['driver'] = text
        sessions[chat_id]['step'] = 'prov_enter_price'
        save_sessions()
        bot.send_message(int(chat_id), "💵 Enter **Fare Price** (e.g., 150.00):")

    elif step == 'prov_enter_price':
        sessions[chat_id]['temp_service']['price'] = text
        sessions[chat_id]['step'] = 'prov_enter_contact'
        save_sessions()
        bot.send_message(int(chat_id), "📞 Enter **Contact Number**:")

    elif step == 'prov_enter_contact':
        sessions[chat_id]['temp_service']['contact'] = text
        sessions[chat_id]['step'] = 'prov_select_payment'
        sessions[chat_id]['temp_service']['payment_methods'] = [] # Init list
        save_sessions()
        bot.send_message(
            int(chat_id), 
            "💳 **Payment Options**\nToggle allowed methods:",
            reply_markup=payment_toggle_keyboard([]),
            parse_mode='Markdown'
        )

    # --- PASSENGER SEARCH FLOW (Text Input Parts) ---
    elif step == 'pass_enter_time':
        # (Existing time logic from V2 would go here)
        # For this UI demo, we assume time was selected via buttons or text
        pass

# --- CALLBACK QUERY HANDLER (The Core UI Logic) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    chat_id = str(call.message.chat.id)
    data = call.data
    
    # --- 1. MAIN MENU ---
    if data == "menu_main":
        clear_session(chat_id)
        bot.edit_message_text("👋 **Welcome to RoutAfare!**\n\nPlease select your role:", 
                              call.message.chat.id, call.message.message_id, 
                              reply_markup=main_menu_keyboard(), parse_mode='Markdown')

    elif data == "role_passenger":
        # Start Passenger Flow
        routes = get_all_routes()
        if not routes:
            bot.answer_callback_query(call.id, "No routes available.")
            return
            
        sessions[chat_id] = {'step': 'pass_select_route', 'data': {}}
        save_sessions()
        
        kb = InlineKeyboardMarkup(row_width=2)
        for r in routes:
            kb.add(InlineKeyboardButton(r, callback_data=f"route_{r}"))
        kb.add(InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
        
        bot.edit_message_text("📍 **Select your Route:**", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode='Markdown')

    elif data == "role_provider":
        sessions[chat_id] = {'step': 'provider_auth'}
        save_sessions()
        bot.send_message(int(chat_id), "🔒 Enter Provider Password:")

    # --- 2. PASSENGER FLOW ---
    elif data.startswith("route_"):
        route = data.split("_")[1]
        sessions[chat_id]['data']['route'] = route
        sessions[chat_id]['step'] = 'pass_count'
        save_sessions()
        bot.edit_message_text(f"Selected: **{route}**\n\n👥 **How many passengers?**", 
                              call.message.chat.id, call.message.message_id, 
                              reply_markup=passenger_count_keyboard(), parse_mode='Markdown')

    elif data.startswith("pass_count_"):
        count = data.replace("pass_count_", "")
        sessions[chat_id]['data']['passengers'] = count
        
        # SEARCH LOGIC (Mocked for UI Demo)
        route = sessions[chat_id]['data']['route']
        
        # Combine CSV results and JSON results
        results_text = f"🚍 **Available Buses for {route}**\n(Passengers: {count})\n\n"
        
        # Check Provider DB
        found = False
        for svc in services_db['services']:
            if svc['route'] == route and svc.get('status') != 'unavailable':
                pay_str = ", ".join([p.capitalize() for p in svc.get('payment_methods', [])])
                results_text += (f"🔹 **{svc.get('driver')}**\n"
                                 f"   💰 Fare: {svc.get('price')}\n"
                                 f"   📞 {svc.get('contact')}\n"
                                 f"   💳 {pay_str}\n\n")
                found = True
        
        if not found:
            results_text += "No specific provider services found. Checking public schedule...\n(CSV Data would appear here)"

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔄 New Search", callback_data="role_passenger"))
        
        bot.edit_message_text(results_text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode='Markdown')

    # --- 3. PROVIDER MENU FLOW ---
    elif data == "prov_add":
        sessions[chat_id]['step'] = 'prov_enter_route'
        sessions[chat_id]['temp_service'] = {}
        save_sessions()
        bot.send_message(int(chat_id), "🆕 **Add Service**\n\nEnter the **Route Name** (e.g., Kandy-Colombo):", parse_mode='Markdown')

    elif data.startswith("toggle_pay_"):
        # Handling the multi-select checkboxes
        method = data.replace("toggle_pay_", "")
        current = sessions[chat_id]['temp_service'].get('payment_methods', [])
        
        if method in current:
            current.remove(method)
        else:
            current.append(method)
            
        sessions[chat_id]['temp_service']['payment_methods'] = current
        save_sessions()
        
        # Refresh the keyboard to show new checkmarks
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, 
                                      reply_markup=payment_toggle_keyboard(current))

    elif data == "prov_save_service":
        new_service = sessions[chat_id]['temp_service']
        new_service['id'] = str(int(time.time())) # Simple ID
        new_service['status'] = 'active'
        
        services_db['services'].append(new_service)
        save_services()
        
        bot.edit_message_text("✅ **Service Saved Successfully!**", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        # Return to menu
        time.sleep(1)
        bot.send_message(int(chat_id), "Manage your fleet:", reply_markup=provider_menu_keyboard())

    elif data == "prov_status":
        # Show list of services to toggle status
        if not services_db['services']:
            bot.answer_callback_query(call.id, "No services added yet.")
            return

        kb = InlineKeyboardMarkup()
        for svc in services_db['services']:
            status_icon = "🟢" if svc.get('status') == 'active' else "🔴"
            kb.add(InlineKeyboardButton(f"{status_icon} {svc['route']} - {svc['driver']}", callback_data=f"toggle_stat_{svc['id']}"))
        kb.add(InlineKeyboardButton("🔙 Back", callback_data="provider_menu_return"))
        
        bot.edit_message_text("Tap to toggle availability (Holiday/Weather):", call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data.startswith("toggle_stat_"):
        s_id = data.split("_")[2]
        for svc in services_db['services']:
            if svc['id'] == s_id:
                curr = svc.get('status', 'active')
                svc['status'] = 'unavailable' if curr == 'active' else 'active'
                save_services()
                break
        
        # Refresh list
        handle_query(call) # Re-run the list generator (lazy hack for refresh)
        bot.answer_callback_query(call.id, "Status Updated")
        
    elif data == "provider_menu_return":
         bot.edit_message_text("Manage your fleet:", call.message.chat.id, call.message.message_id, reply_markup=provider_menu_keyboard())

    bot.answer_callback_query(call.id)

# --- Server & Webhook (Standard) ---
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'Unsupported media type', 415

@app.route('/', methods=['GET'])
def index():
    return 'routAfare Bot V3 Running', 200

if __name__ == '__main__':
    print('Starting routAfare Bot V3...')
    # For local testing uncomment:
    # bot.remove_webhook()
    # bot.polling()
    # For Render:
    # app.run(host='0.0.0.0', port=SERVER_PORT)








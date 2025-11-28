#!/usr/bin/env python3
"""
routAfare_botFINAL.py
Final Integrated Version: Includes Role-based Menus, Combined Data Search, 
Bus Service Name Input, Final Booking Confirmation, and fix for RecursionError 
in Provider Status Toggle.
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
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    import pandas as pd
    
    # Vertex AI / Google Cloud Imports (kept for context)
    try:
        from google.cloud import aiplatform
        from google.oauth2 import service_account
    except ImportError:
        aiplatform = None
        service_account = None

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
SERVICES_FILE = os.path.join(BASE_DIR, 'services.json') 
CSV_FILE_NAME = os.getenv('CSV_FILE_NAME', 'final routa dataset for bus routes.csv')
CSV_FILE_PATH = os.path.join(BASE_DIR, CSV_FILE_NAME)

PROVIDER_PASSWORD = os.getenv('PROVIDER_PASSWORD', 'admin') 
DEFAULT_DISTANCE_KM = 5.0

# --- Data Persistence Helpers ---
def safe_write_json(file_path, data):
    try:
        os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
        with open(file_path, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {file_path}: {e}")

def safe_read_json(file_path, fallback):
    if not os.path.exists(file_path):
        return fallback
    try:
        with open(file_path, 'r', encoding='utf8') as f:
            content = f.read().strip()
            return json.loads(content or "{}")
    except Exception:
        return fallback

# Load data stores
sessions = safe_read_json(SESSIONS_FILE, {})
services_db = safe_read_json(SERVICES_FILE, {"services": []}) # Dynamic provider data

def save_sessions(): safe_write_json(SESSIONS_FILE, sessions)
def save_services(): safe_write_json(SERVICES_FILE, services_db)
def clear_session(chat_id):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

# --- CSV & Data Loading ---

def time_to_minutes(t: str):
    m = re.match(r'^(\d{1,2}):(\d{2})$', t)
    if not m: return None
    try: return int(m.group(1)) * 60 + int(m.group(2))
    except ValueError: return None

def generate_departure_times(time_slot, interval_minutes=60):
    m = re.match(r'(\d{1,2}:\d{2})-(\d{1,2}:\d{2})', str(time_slot))
    if not m: return []
    start_str, end_str = m.groups()
    try:
        start_time = datetime.strptime(start_str, '%H:%M')
        end_time = datetime.strptime(end_str, '%H:%M')
    except ValueError: return []
    times = []
    current_time = start_time
    while current_time <= end_time:
        times.append(current_time.strftime('%H:%M'))
        current_time += timedelta(minutes=interval_minutes)
    return times

def load_bus_data(csv_file_path):
    if not os.path.exists(csv_file_path): return []
    try:
        df = pd.read_csv(csv_file_path)
    except Exception: return []
    unique_bus_cols = [c for c in ['route_id', 'bus_route', 'bus_type_num', 'direction'] if c in df.columns]
    if not unique_bus_cols: return []

    df_unique = df.groupby(unique_bus_cols).agg(times=('time_slot', lambda x: list(x.dropna().unique()))).reset_index()
    bus_data_list = []
    bus_id_counter = 1
    for _, row in df_unique.iterrows():
        all_times = set()
        for slot in row['times']: all_times.update(generate_departure_times(slot))
        if not all_times: continue
        sorted_times = sorted(list(all_times), key=lambda s: time_to_minutes(s))
        bus_data_list.append({
            'id': f'BUS-{bus_id_counter}',
            'route_id': row.get('route_id'),
            'name': row.get('bus_route') if 'bus_route' in row else str(row.get('route_id')),
            'bus_type_num': int(row.get('bus_type_num')) if 'bus_type_num' in row and pd.notna(row.get('bus_type_num')) else 1,
            'capacity': 50,
            'times': sorted_times
        })
        bus_id_counter += 1
    return bus_data_list

def get_all_routes():
    csv_routes = set()
    try:
        csv_routes = {b.get('name') for b in buses if b.get('name')}
    except Exception: pass
    
    json_routes = {s['route'] for s in services_db['services']}
    
    return sorted(list(csv_routes.union(json_routes)))

buses = load_bus_data(CSV_FILE_PATH)
ROUTE_NAMES = get_all_routes()


# --- Vertex AI/Fare Prediction (Fallback only for this example) ---
vertex_predictor = None # Assumed not initialized for simplicity

def get_fare_prediction_safe(data, predictor):
    # This is a fallback calculation
    try: distance_km = float(data.get('distance_km', DEFAULT_DISTANCE_KM))
    except Exception: distance_km = DEFAULT_DISTANCE_KM
    base = 20.0
    per_km = 5.0
    traffic_mult = 1.0 + (0.1 * int(data.get('traffic_level_num', 1)))
    fare = max(5.0, round((base + distance_km * per_km) * traffic_mult, 2))
    return fare


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
        InlineKeyboardButton("✏️ Update Service (Mock)", callback_data="prov_update_mock"),
        InlineKeyboardButton("🗑️ Delete / Status Toggle", callback_data="prov_status"),
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
    w_status = "✅" if "weekly" in current_selection else "⬜"
    m_status = "✅" if "monthly" in current_selection else "⬜"
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(f"{w_status} Weekly", callback_data="toggle_pay_weekly"),
        InlineKeyboardButton(f"{m_status} Monthly", callback_data="toggle_pay_monthly")
    )
    markup.add(InlineKeyboardButton("💾 Save & Finish", callback_data="prov_save_service"))
    return markup


# --- MESSAGE HANDLERS (Text Input) ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    clear_session(message.chat.id)
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
        sessions[chat_id]['step'] = 'prov_enter_service_name' 
        save_sessions()
        bot.send_message(int(chat_id), "🚍 Enter the **Bus Service/Company Name** (e.g., ABC Express, Private Bus):")

    elif step == 'prov_enter_service_name':
        sessions[chat_id]['temp_service']['service_name'] = text
        sessions[chat_id]['step'] = 'prov_enter_driver'
        save_sessions()
        bot.send_message(int(chat_id), "🧑 Enter **Driver Name**:")

    elif step == 'prov_enter_driver':
        sessions[chat_id]['temp_service']['driver'] = text
        sessions[chat_id]['step'] = 'prov_enter_price'
        save_sessions()
        bot.send_message(int(chat_id), "💵 Enter **Fare Price** (e.g., 150.00):")

    elif step == 'prov_enter_price':
        try:
            price = float(text)
            sessions[chat_id]['temp_service']['price'] = text
            sessions[chat_id]['step'] = 'prov_enter_contact'
            save_sessions()
            bot.send_message(int(chat_id), "📞 Enter **Contact Number**:")
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid price format. Please enter a number (e.g., 150.00).")


    elif step == 'prov_enter_contact':
        if re.match(r'^\+?[\d\s-]{5,}$', text):
            sessions[chat_id]['temp_service']['contact'] = text
            sessions[chat_id]['step'] = 'prov_select_payment'
            sessions[chat_id]['temp_service']['payment_methods'] = [] 
            save_sessions()
            bot.send_message(
                int(chat_id), 
                "💳 **Payment Options**\nToggle allowed methods:",
                reply_markup=payment_toggle_keyboard([]),
                parse_mode='Markdown'
            )
        else:
            bot.send_message(int(chat_id), "❌ Invalid contact format. Please enter a valid number.")

    # --- PASSENGER SEARCH FLOW (Time Input) ---
    elif step == 'await_time':
        m = re.match(r'^([0-1]?\d|2[0-3]):([0-5]\d)$', text)
        if not m:
            bot.send_message(int(chat_id), "❌ Invalid time format. Use HH:MM (e.g., 13:45).")
            return

        sessions[chat_id]['data']['time'] = text
        save_sessions()

        try:
            predicted_fare = get_fare_prediction_safe(sessions[chat_id]['data'], vertex_predictor)
            sessions[chat_id]['data']['predicted_fare'] = predicted_fare
            save_sessions()
        except Exception:
            predicted_fare = get_fare_prediction_safe({}, None) # Fallback hard
        
        bot.send_message(int(chat_id), 'Calculating fare and searching for matching buses... 🔎')
        
        s_data = sessions[chat_id]['data']
        route_name = s_data.get('selected_route')
        time_input = s_data.get('time')
        
        # 1. Search static CSV data
        csv_matching = [
            bus for bus in buses 
            if bus.get('name') == route_name 
            and time_input in bus.get('times', [])
        ]
        
        # 2. Search dynamic Provider data
        provider_matching = [
            svc for svc in services_db['services'] 
            if svc.get('route') == route_name 
            and svc.get('status') != 'unavailable'
        ]
        
        final_bus_list = []

        # Add CSV buses to the final list, store full details in session for later
        for bus in csv_matching:
            temp_bus_id = f"CSV-{bus['id']}" 
            bus_details = {
                'id': temp_bus_id,
                'type': 'CSV',
                'name': f"Public Bus ({bus['bus_type_num']})",
                'details_text': f"Scheduled ({', '.join(bus['times'][:2])}...) | Estimated Fare: Rs. {s_data.get('predicted_fare', 'N/A')}",
                'fare': s_data.get('predicted_fare', 'N/A')
            }
            final_bus_list.append(bus_details)

        # Add Provider buses to the final list
        for svc in provider_matching:
            pay_str = ", ".join([p.capitalize() for p in svc.get('payment_methods', [])])
            bus_details = {
                'id': svc['id'], 
                'type': 'PROVIDER',
                'name': f"{svc.get('service_name', 'N/A')} | Driver: {svc.get('driver')}",
                'details_text': f"Fare: Rs. {svc.get('price')} | Contact: {svc.get('contact')} | Payment: {pay_str}",
                'fare': svc.get('price')
            }
            final_bus_list.append(bus_details)

        if not final_bus_list:
            bot.send_message(int(chat_id), "❌ No buses found for that route/time.")
            clear_session(chat_id)
            return

        # Store the list of found buses in the session for confirmation step
        sessions[chat_id]['found_buses'] = {b['id']: b for b in final_bus_list}
        save_sessions()

        keyboard = InlineKeyboardMarkup()
        for bus in final_bus_list:
            # Use a CONFIRM callback for the next step
            keyboard.add(InlineKeyboardButton(f"✅ Book: {bus['name']} - {bus['details_text']}", callback_data=f"confirm_{bus['id']}"))

        fare_text = f"🚌 *Your Search Summary*\nRoute: {route_name}\nTime: {time_input}\nEstimated Fare: Rs\. {s_data.get('predicted_fare', 'N/A')}\n\n**Select a bus to Confirm Booking:**"
        
        bot.send_message(int(chat_id), fare_text, parse_mode='Markdown', reply_markup=keyboard)
        sessions[chat_id]['step'] = 'await_bus_select'
        save_sessions()
        return

    # NOTE: The previous fallback message has been removed.
    # The bot will now ignore text input if the current 'step' does not match any
    # of the expected input steps above.

# --- CALLBACK QUERY HANDLER (The Core UI Logic) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    chat_id = str(call.message.chat.id)
    data = call.data
    
    def edit_and_answer(text, reply_markup=None):
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              reply_markup=reply_markup, parse_mode='Markdown')
        bot.answer_callback_query(call.id)

    # --- 1. MAIN MENU/ROLES ---
    if data == "menu_main":
        clear_session(chat_id)
        edit_and_answer("👋 **Welcome to RoutAfare!**\n\nPlease select your role:", main_menu_keyboard())

    elif data == "role_passenger":
        routes = get_all_routes()
        if not routes:
            bot.answer_callback_query(call.id, "No routes available. Contact the admin.")
            return
            
        sessions[chat_id] = {'step': 'pass_select_route', 'data': {}}
        save_sessions()
        
        kb = InlineKeyboardMarkup(row_width=2)
        for r in routes:
            kb.add(InlineKeyboardButton(r, callback_data=f"route_{r}"))
        kb.add(InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
        
        edit_and_answer("📍 **Select your Route:**", kb)

    elif data == "role_provider":
        sessions[chat_id] = {'step': 'provider_auth'}
        save_sessions()
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(int(chat_id), "🔒 Enter Provider Password:")
        bot.answer_callback_query(call.id)
        return

    # --- 2. PASSENGER FLOW (Route, Count, and CONFIRM) ---
    elif data.startswith("route_"):
        route = data.split("_")[1]
        sessions[chat_id]['data']['selected_route'] = route
        sessions[chat_id]['step'] = 'pass_count'
        save_sessions()
        edit_and_answer(f"Selected: **{route}**\n\n👥 **How many passengers?**", passenger_count_keyboard())

    elif data.startswith("pass_count_"):
        count = data.replace("pass_count_", "")
        sessions[chat_id]['data']['passengers'] = count
        sessions[chat_id]['step'] = 'await_time'
        sessions[chat_id]['data']['distance_km'] = DEFAULT_DISTANCE_KM
        save_sessions()
        edit_and_answer("⏰ Enter your departure time in **HH:MM** (example: 13:45):")

    elif data.startswith("confirm_"):
        # --- BOOKING CONFIRMATION STEP ---
        bus_id = data.replace('confirm_', '', 1)
        found_buses = sessions[chat_id].get('found_buses', {})
        selected_bus = found_buses.get(bus_id)
        
        if not selected_bus:
            edit_and_answer("❌ Error: Bus details not found. Starting over.", main_menu_keyboard())
            clear_session(chat_id)
            return

        fare_display = f"Rs. {selected_bus['fare']}" if selected_bus['fare'] != 'N/A' else "Estimated (Check operator)"
        
        # Extract Contact from details_text if available
        contact = "N/A"
        if selected_bus['type'] == 'PROVIDER':
             # Looks for 'Contact: XXXX | Payment:'
            contact_match = re.search(r'Contact: (.*?) \| Payment:', selected_bus['details_text'])
            if contact_match:
                contact = contact_match.group(1).strip()
        
        confirmation_message = (
            f"✅ **BOOKING CONFIRMED!** 🎉\n\n"
            f"🚍 **Service:** {selected_bus['name']}\n"
            f"💲 **Fare:** {fare_display}\n"
            f"📞 **Contact:** {contact}\n\n"
            f"Thank you for using RoutAfare. Please be ready to board at the departure time."
        )

        edit_and_answer(confirmation_message, InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 New Search", callback_data="menu_main")))
        clear_session(chat_id)
        return

    # --- 3. PROVIDER MENU FLOW ---
    elif data == "prov_add":
        sessions[chat_id]['step'] = 'prov_enter_route'
        sessions[chat_id]['temp_service'] = {}
        save_sessions()
        edit_and_answer("🆕 **Add Service**\n\nEnter the **Route Name** (e.g., Kandy-Colombo):")

    elif data.startswith("toggle_pay_"):
        method = data.replace("toggle_pay_", "")
        current = sessions[chat_id]['temp_service'].get('payment_methods', [])
        
        if method in current: current.remove(method)
        else: current.append(method)
            
        sessions[chat_id]['temp_service']['payment_methods'] = current
        save_sessions()
        
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, 
                                      reply_markup=payment_toggle_keyboard(current))
        bot.answer_callback_query(call.id)
        return

    elif data == "prov_save_service":
        new_service = sessions[chat_id]['temp_service']
        new_service['id'] = str(int(time.time()))
        new_service['status'] = 'active'
        
        services_db['services'].append(new_service)
        save_services()
        
        edit_and_answer("✅ **Service Saved Successfully!**", provider_menu_keyboard())
        clear_session(chat_id)
        return

    elif data == "prov_status":
        if not services_db['services']:
            bot.answer_callback_query(call.id, "No services added yet.")
            return

        kb = InlineKeyboardMarkup()
        for svc in services_db['services']:
            status_icon = "🟢 ACTIVE" if svc.get('status') == 'active' else "🔴 UNAVAILABLE"
            kb.add(InlineKeyboardButton(f"{svc['service_name']} - {svc['route']} ({status_icon})", callback_data=f"toggle_stat_{svc['id']}"))
        kb.add(InlineKeyboardButton("🔙 Back to Provider Menu", callback_data="provider_menu_return"))
        
        edit_and_answer("Tap to toggle availability (Holiday/Weather):", kb)

    elif data.startswith("toggle_stat_"):
        s_id = data.split("_")[2]
        
        # 1. Update the status and save
        for svc in services_db['services']:
            if svc['id'] == s_id:
                curr = svc.get('status', 'active')
                svc['status'] = 'unavailable' if curr == 'active' else 'active'
                save_services()
                break
        
        # 2. Re-create and update the message with the new status list (Fixes RecursionError)
        if not services_db['services']:
            bot.answer_callback_query(call.id, "No services remaining.")
            edit_and_answer("Manage your fleet:", provider_menu_keyboard())
            return
            
        kb = InlineKeyboardMarkup()
        for svc in services_db['services']:
            status_icon = "🟢 ACTIVE" if svc.get('status') == 'active' else "🔴 UNAVAILABLE"
            kb.add(InlineKeyboardButton(f"{svc['service_name']} - {svc['route']} ({status_icon})", callback_data=f"toggle_stat_{svc['id']}"))
        kb.add(InlineKeyboardButton("🔙 Back to Provider Menu", callback_data="provider_menu_return"))
        
        edit_and_answer("Tap to toggle availability (Holiday/Weather):", kb)
        return

    elif data == "provider_menu_return":
        edit_and_answer("Manage your fleet:", provider_menu_keyboard())
        
    elif data == "prov_update_mock":
         bot.answer_callback_query(call.id, "Feature not yet implemented. Use 'Add' or 'Status' instead.", show_alert=True)
         return
        
    bot.answer_callback_query(call.id)


# --- Flask Server & Deployment ---
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        # Check for bot object before processing
        if bot:
            bot.process_new_updates([update])
        return '', 200
    return 'Unsupported media type', 415

@app.route('/', methods=['GET'])
def index():
    return 'RoutAfare Bot FINAL Running', 200

def set_initial_webhook():
    if bot is None: return
    full_webhook_url = f"{WEBHOOK_URL_BASE}{WEBHOOK_URL_PATH}"
    try:
        # It's good practice to get the current webhook info before removing/setting
        current_webhook = bot.get_webhook_info()
        if current_webhook.url != full_webhook_url:
            bot.remove_webhook()
            time.sleep(0.1)
            bot.set_webhook(url=full_webhook_url)
            print(f"✅ Telegram Webhook set to: {full_webhook_url}")
        else:
            print("✅ Telegram Webhook is already correctly set.")

    except Exception as e:
        print(f"FATAL: Failed to set webhook. Error: {e}")

if bot is not None:
    set_initial_webhook()

if __name__ == '__main__':
    print('Starting RoutAfare Bot FINAL...')
    print('Ready.')

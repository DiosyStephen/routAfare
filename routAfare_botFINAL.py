#!/usr/bin/env python3
"""
routAfare_botFINAL.py
Final Integrated Version: Includes all fixes and features:
- Secure Vertex AI Initialization (or local fallback)
- Data Persistence Fix (Provider services saved to services.json)
- Seat Management (Total/Remaining seats tracked and deducted)
- Robust Error Handling for random text input
- Exit Program option
- Fix for Bold Text (parse_mode='Markdown' applied consistently)
- Age-based Passenger Input and Fare Calculation
- Provider Input for Adult/Teacher/Child Fares
"""

import os
import json
import re
import sys
import time
from datetime import datetime, timedelta

# Third-party libraries
try:
    # Ensure necessary libraries are installed via pip
    from dotenv import load_dotenv
    from flask import Flask, request
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    import pandas as pd
    
    # Vertex AI / Google Cloud Imports (Optional, for advanced features)
    try:
        from google.cloud import aiplatform
        from google.oauth2 import service_account
        GCP_LIBRARIES_AVAILABLE = True
    except ImportError:
        aiplatform = None
        service_account = None
        GCP_LIBRARIES_AVAILABLE = False

except ImportError as e:
    print(f"CRITICAL: Missing required package. Error: {e}")
    sys.exit(1)

# Load environment variables from .env file
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
CSV_FILE_PATH = os.path.join(BASE_DIR, CSV_FILE_NAME) # Corrected path
# Note: Ensure 'final routa dataset for bus routes.csv' is in the same directory

PROVIDER_PASSWORD = os.getenv('PROVIDER_PASSWORD', 'admin') 
DEFAULT_DISTANCE_KM = 5.0

# --- Data Persistence Helpers ---
def safe_write_json(file_path, data):
    """Safely writes Python object to a JSON file."""
    try:
        os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
        with open(file_path, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {file_path}: {e}")

def safe_read_json(file_path, fallback):
    """Safely reads JSON file, returning fallback on error or if file is missing/empty."""
    if not os.path.exists(file_path):
        return fallback
    try:
        with open(file_path, 'r', encoding='utf8') as f:
            content = f.read().strip()
            # If the file is empty, return the fallback
            return json.loads(content) if content else fallback
    except Exception:
        # If any error occurs (e.g., malformed JSON), return the fallback
        return fallback

# Load data stores
sessions = safe_read_json(SESSIONS_FILE, {})
# Load existing services data, crucial for persistence
services_db = safe_read_json(SERVICES_FILE, {"services": []}) 

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
        # Adjusting to handle times that cross midnight if necessary, but keep it simple for now
        start_time = datetime.strptime(start_str, '%H:%M')
        end_time = datetime.strptime(end_str, '%H:%M')
    except ValueError: return []
    
    # Handle time range that spans midnight (e.g., 23:00-01:00). 
    # For simplicity, we assume same-day for now.
    
    times = []
    current_time = start_time
    while current_time <= end_time:
        times.append(current_time.strftime('%H:%M'))
        current_time += timedelta(minutes=interval_minutes)
    return times

def load_bus_data(csv_file_path):
    if not os.path.exists(csv_file_path): return []
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
    except Exception as e: 
        print(f"Error loading CSV data: {e}")
        return []
    
    # Define columns needed for unique bus identification
    unique_bus_cols = [c for c in ['route_id', 'bus_route', 'bus_type_num', 'direction'] if c in df.columns]
    if not unique_bus_cols: 
        print("Warning: CSV is missing required columns (route_id, bus_route, etc.)")
        return []

    # Group by unique route attributes and collect all time slots
    df_unique = df.groupby(unique_bus_cols).agg(times=('time_slot', lambda x: list(x.dropna().unique()))).reset_index()
    
    bus_data_list = []
    bus_id_counter = 1
    for _, row in df_unique.iterrows():
        all_times = set()
        for slot in row.get('times', []): 
            all_times.update(generate_departure_times(slot))
            
        if not all_times: continue
        
        # Sort times for consistent display
        sorted_times = sorted(list(all_times), key=lambda s: time_to_minutes(s))
        
        bus_data_list.append({
            'id': f'BUS-{bus_id_counter}',
            'route_id': row.get('route_id'),
            'name': row.get('bus_route') if 'bus_route' in row else str(row.get('route_id')),
            'bus_type_num': int(row.get('bus_type_num')) if 'bus_type_num' in row and pd.notna(row.get('bus_type_num')) else 1,
            'capacity': 50, # Default capacity for CSV/Public bus
            'times': sorted_times
        })
        bus_id_counter += 1
    return bus_data_list

def get_all_routes():
    csv_routes = set()
    try:
        csv_routes = {b.get('name') for b in buses if b.get('name')}
    except Exception: pass
    
    # Include routes from the persistent provider database
    json_routes = {s['route'] for s in services_db.get('services', [])}
    
    return sorted(list(csv_routes.union(json_routes)))

# Load bus data on startup
buses = load_bus_data(CSV_FILE_PATH)
ROUTE_NAMES = get_all_routes()


# --- Vertex AI/Fare Prediction Initialization ---
# Note: These variables must be set in your environment file (.env) for Vertex AI to work
GCP_PROJECT_ID = os.getenv('GCP_PROJECT_ID')
GCP_LOCATION = os.getenv('GCP_LOCATION')
VERTEX_ENDPOINT_ID = os.getenv('VERTEX_ENDPOINT_ID')
CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')

vertex_predictor = None

if GCP_LIBRARIES_AVAILABLE and all([GCP_PROJECT_ID, GCP_LOCATION, VERTEX_ENDPOINT_ID, CREDENTIALS_JSON]):
    try:
        credentials_info = json.loads(CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(credentials_info)
        aiplatform.init(project=GCP_PROJECT_ID, location=GCP_LOCATION, credentials=credentials)
        # Assuming the Endpoint ID is the full name or short name that can be resolved
        vertex_predictor = aiplatform.Endpoint(endpoint_name=VERTEX_ENDPOINT_ID) 
        print("✅ Vertex AI Prediction Endpoint Initialized.")

    except Exception as e:
        print(f"❌ Error initializing Vertex AI. Falling back to local calculation. Error: {e}")
        vertex_predictor = None 


# --- Fare Calculation Logic ---
def get_fare_prediction_safe(data, predictor):
    """
    Calculates/Predicts a simple standard adult fare for CSV/Public buses.
    This uses a local fallback since the Vertex AI call structure is complex.
    """
    
    try: 
        distance_km = float(data.get('distance_km', DEFAULT_DISTANCE_KM))
        # Simple count of passengers for basic prediction
        pass_count = len(data.get('passenger_ages', [])) or 1 
    except Exception: 
        distance_km = DEFAULT_DISTANCE_KM
        pass_count = 1
        
    # Local Fallback Calculation (A simple standard adult fare prediction)
    base = 20.0 * pass_count 
    per_km = 5.0 
    traffic_mult = 1.0 # Simplified
    fare = max(5.0, round((base + distance_km * per_km) * traffic_mult, 2))
    
    # Return the simple predicted fare for display purposes before booking
    return fare

def calculate_final_fare(bus_data, passenger_ages):
    """Calculates the final fare based on passenger ages and bus's fare structure."""
    if not passenger_ages: return 0.0
    
    total_fare = 0.0
    
    # 1. Private/Provider Bus Fare
    if bus_data.get('type') == 'PROVIDER':
        # Safely convert fares to float, defaulting to 0.0 if missing/invalid
        try: adult_fare = float(bus_data.get('adult_fare', 0.0))
        except ValueError: adult_fare = 0.0
        
        try: teacher_fare = float(bus_data.get('teacher_fare', adult_fare))
        except ValueError: teacher_fare = adult_fare
        
        try: child_fare = float(bus_data.get('child_fare', adult_fare))
        except ValueError: child_fare = adult_fare
        
        for age in passenger_ages:
            # Classification based on assumed standard fare categories
            if age > 12: # Adults and Teachers
                # A more complex bot might ask if the passenger is a teacher
                total_fare += adult_fare 
            elif age > 0 and age <= 12: # Children 
                total_fare += child_fare
            # Age 0 or less (infant free, etc.) is zero fare
            
        return round(total_fare, 2)
        
    # 2. Public/CSV Bus Fare 
    else: 
        # Fallback to the single predicted fare (already calculated for the group)
        return bus_data.get('fare', 0.0)


# --- Bot Initialization ---
bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# --- KEYBOARDS & UI HELPERS ---

def main_menu_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("👤 Passenger", callback_data="role_passenger"),
        InlineKeyboardButton("🚌 Bus Provider", callback_data="role_provider")
    )
    markup.add(
        InlineKeyboardButton("❌ Exit Program", callback_data="program_exit") # <-- EXIT OPTION ADDED
    )
    return markup

def provider_menu_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("➕ Add New Service", callback_data="prov_add"),
        InlineKeyboardButton("🗑️ Change Status (Toggle Availability)", callback_data="prov_status"),
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

def send_error_message(chat_id):
    """Sends a standardized error/help message for invalid input."""
    bot.send_message(
        chat_id, 
        "🚫 **Invalid Input!**\n\nI was expecting data for a specific step, or the `/start` command.\n\n"
        "Please use the menu below or type `/start` to begin a new process.",
        reply_markup=main_menu_keyboard(),
        parse_mode='Markdown'
    )

def edit_and_answer(call, text, reply_markup=None):
    """Helper to edit the message and answer the callback query, setting Markdown mode."""
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                          reply_markup=reply_markup, parse_mode='Markdown')
    bot.answer_callback_query(call.id)


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
    
    # List of steps where text input is expected
    active_steps = [
        'provider_auth', 'prov_enter_route', 'prov_enter_service_name', 
        'prov_enter_driver', 'prov_enter_seats', 'prov_enter_adult_fare', 
        'prov_enter_teacher_fare', 'prov_enter_child_fare', 'prov_enter_contact', 
        'await_age', 'await_time'
    ]
    
    current_step = sessions.get(chat_id, {}).get('step')

    # FIX: Robust Error Handling - If not in an active flow, show error/help message
    if current_step not in active_steps:
        send_error_message(int(chat_id))
        return

    # --- PROVIDER AUTH ---
    if current_step == 'provider_auth':
        if text == PROVIDER_PASSWORD:
            sessions[chat_id]['step'] = 'provider_menu'
            save_sessions()
            bot.send_message(int(chat_id), "🔓 **Access Granted**\nManage your fleet:", reply_markup=provider_menu_keyboard(), parse_mode='Markdown')
        else:
            bot.send_message(int(chat_id), "❌ Wrong Password. Try again or /start")

    # --- PROVIDER ADDING DATA FLOW ---
    elif current_step == 'prov_enter_route':
        sessions[chat_id]['temp_service']['route'] = text
        sessions[chat_id]['step'] = 'prov_enter_service_name' 
        save_sessions()
        bot.send_message(int(chat_id), "🚍 Enter the **Bus Service/Company Name** (e.g., ABC Express, Private Bus):", parse_mode='Markdown')

    elif current_step == 'prov_enter_service_name':
        sessions[chat_id]['temp_service']['service_name'] = text
        sessions[chat_id]['step'] = 'prov_enter_driver'
        save_sessions()
        bot.send_message(int(chat_id), "🧑 Enter **Driver Name**:", parse_mode='Markdown')

    elif current_step == 'prov_enter_driver':
        sessions[chat_id]['temp_service']['driver'] = text
        sessions[chat_id]['step'] = 'prov_enter_seats' # <-- SEAT MANAGEMENT STEP
        save_sessions()
        bot.send_message(int(chat_id), "💺 Enter the **Total Number of Seats** available (e.g., 50):", parse_mode='Markdown')

    elif current_step == 'prov_enter_seats':
        try:
            seats = int(text)
            if seats <= 0: raise ValueError
            sessions[chat_id]['temp_service']['total_seats'] = seats
            # Seat Management: Set remaining seats immediately
            sessions[chat_id]['temp_service']['remaining_seats'] = seats 
            sessions[chat_id]['step'] = 'prov_enter_adult_fare' 
            save_sessions()
            
            # Start fare collection flow
            bot.send_message(int(chat_id), "💵 Enter **Adult/Standard Fare Price** (e.g., 150.00):", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid number of seats. Please enter a positive whole number.")

    elif current_step == 'prov_enter_adult_fare':
        try:
            fare = float(text)
            sessions[chat_id]['temp_service']['adult_fare'] = text
            sessions[chat_id]['step'] = 'prov_enter_teacher_fare' 
            save_sessions()
            # Prompt for Teacher Fare
            bot.send_message(int(chat_id), "👨‍🏫 Enter **Teacher Fare Price** (e.g., 100.00). Enter the same as Adult Fare if no discount:", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 150.00).")

    elif current_step == 'prov_enter_teacher_fare':
        try:
            fare = float(text)
            sessions[chat_id]['temp_service']['teacher_fare'] = text
            sessions[chat_id]['step'] = 'prov_enter_child_fare' 
            save_sessions()
            # Prompt for Child Fare
            bot.send_message(int(chat_id), "👶 Enter **Child Fare Price** (e.g., 75.00). Enter 0 if children travel free:", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 100.00).")

    elif current_step == 'prov_enter_child_fare':
        try:
            fare = float(text)
            sessions[chat_id]['temp_service']['child_fare'] = text
            sessions[chat_id]['step'] = 'prov_enter_contact'
            save_sessions()
            # Proceed to contact
            bot.send_message(int(chat_id), "📞 Enter **Contact Number** (e.g., +9477xxxxxxx):", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 75.00).")


    elif current_step == 'prov_enter_contact':
        # Simple validation for a phone number
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

    # --- PASSENGER AGE INPUT FLOW ---
    elif current_step == 'await_age':
        current_data = sessions[chat_id]['data']
        passengers_to_enter = current_data['passengers_to_enter']
        current_passenger_num = current_data['current_passenger_num']
        
        try:
            age = int(text)
            if age < 0 or age > 120: raise ValueError
            
            # Store the age
            current_data['passenger_ages'].append(age)
            current_data['current_passenger_num'] += 1
            
            # Check if all ages are entered
            if current_data['current_passenger_num'] > passengers_to_enter:
                # All ages entered, move to time step
                sessions[chat_id]['step'] = 'await_time'
                save_sessions()
                # Prompt for time
                bot.send_message(int(chat_id), "⏰ Enter your departure time in **HH:MM** (example: 13:45):", parse_mode='Markdown')
            else:
                # Continue asking for the next passenger's age
                sessions[chat_id]['step'] = 'await_age'
                save_sessions()
                bot.send_message(
                    int(chat_id), 
                    f"🧑 Enter the **age** for Passenger #{current_data['current_passenger_num']}:",
                    parse_mode='Markdown'
                )
            
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid age. Please enter a whole number between 0 and 120.")
            return

    # --- PASSENGER SEARCH FLOW (Time Input) ---
    elif current_step == 'await_time':
        m = re.match(r'^([0-1]?\d|2[0-3]):([0-5]\d)$', text)
        if not m:
            bot.send_message(int(chat_id), "❌ Invalid time format. Use HH:MM (e.g., 13:45).")
            return

        sessions[chat_id]['data']['time'] = text
        save_sessions()

        # Predict a simple fare for *display* on CSV/Public routes
        predicted_fare_placeholder = get_fare_prediction_safe(sessions[chat_id]['data'], vertex_predictor)
        sessions[chat_id]['data']['predicted_fare'] = predicted_fare_placeholder
        save_sessions()
        
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
            svc for svc in services_db.get('services', [])
            if svc.get('route') == route_name 
            and svc.get('status') == 'active' # Only show active services
        ]
        
        final_bus_list = []

        # Add CSV buses to the final list
        for bus in csv_matching:
            temp_bus_id = f"CSV-{bus['id']}" 
            bus_details = {
                'id': temp_bus_id,
                'type': 'CSV',
                'name': f"Public Bus ({bus.get('bus_type_num', 1)})",
                'details_text': f"Scheduled ({', '.join(bus['times'][:2])}...) | Estimated Fare: Rs. {predicted_fare_placeholder}",
                'fare': predicted_fare_placeholder 
            }
            final_bus_list.append(bus_details)

        # Add Provider buses to the final list
        for svc in provider_matching:
            
            # --- CALCULATE FARE FOR PROVIDER BUS USING AGE DATA ---
            mock_bus_data = {
                'type': 'PROVIDER', 
                'adult_fare': svc.get('adult_fare', 0.0),
                'teacher_fare': svc.get('teacher_fare', 0.0),
                'child_fare': svc.get('child_fare', 0.0)
            }
            
            final_calculated_fare = calculate_final_fare(mock_bus_data, s_data.get('passenger_ages', []))
            
            pay_str = ", ".join([p.capitalize() for p in svc.get('payment_methods', [])])
            # Seat Management: Display remaining seats
            seats_info = f"Seats: {svc.get('remaining_seats', 'N/A')}" 
            
            bus_details = {
                'id': svc['id'], 
                'type': 'PROVIDER',
                'name': f"{svc.get('service_name', 'N/A')} | Driver: {svc.get('driver')}",
                'details_text': f"Calculated Fare: Rs. {final_calculated_fare} | Contact: {svc.get('contact')} | Payment: {pay_str} | {seats_info}",
                'fare': final_calculated_fare
            }
            final_bus_list.append(bus_details)

        if not final_bus_list:
            bot.send_message(int(chat_id), "❌ No buses found for that route/time.")
            clear_session(chat_id)
            return

        sessions[chat_id]['found_buses'] = {b['id']: b for b in final_bus_list}
        save_sessions()

        keyboard = InlineKeyboardMarkup()
        for bus in final_bus_list:
            keyboard.add(InlineKeyboardButton(f"✅ Book: {bus['name']} - {bus['details_text']}", callback_data=f"confirm_{bus['id']}"))

        fare_text = f"🚌 *Your Search Summary*\nRoute: {route_name}\nTime: {time_input}\nPassengers: {len(s_data.get('passenger_ages', []))}\n\n**Select a bus to Confirm Booking:**"
        
        bot.send_message(int(chat_id), fare_text, parse_mode='Markdown', reply_markup=keyboard)
        sessions[chat_id]['step'] = 'await_bus_select'
        save_sessions()
        return

# --- CALLBACK QUERY HANDLER (The Core UI Logic) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    chat_id = str(call.message.chat.id)
    data = call.data
    
    # --- 1. MAIN MENU/ROLES ---
    if data == "menu_main":
        clear_session(chat_id)
        edit_and_answer(call, "👋 **Welcome to routAfare!**\n\nPlease select your role:", main_menu_keyboard())

    # --- EXIT OPTION ---
    elif data == "program_exit": # <-- EXIT PROGRAM IMPLEMENTATION
        clear_session(chat_id)
        bot.send_message(
            int(chat_id), 
            "👋 **Goodbye!**\n\nYour session has been cleared. Type `/start` to begin a new journey.",
            parse_mode='Markdown'
        )
        try:
            # Attempt to delete the message with the exit button
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Exiting program.")
        return

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
        
        edit_and_answer(call, "📍 **Select your Route:**", kb)

    elif data == "role_provider":
        sessions[chat_id] = {'step': 'provider_auth'}
        save_sessions()
        # Delete the previous menu message to prompt password entry
        bot.delete_message(call.message.chat.id, call.message.message_id) 
        bot.send_message(int(chat_id), "🔒 Enter **Provider Password**:", parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    # --- 2. PASSENGER FLOW (Booking Confirmation) ---
    elif data.startswith("route_"):
        route = data.split("_")[1]
        sessions[chat_id]['data']['selected_route'] = route
        sessions[chat_id]['step'] = 'pass_count'
        save_sessions()
        edit_and_answer(call, f"Selected: **{route}**\n\n👥 **How many passengers?**", passenger_count_keyboard())

    elif data.startswith("pass_count_"):
        count_str = data.replace("pass_count_", "").replace('+', '')
        try:
            count = int(count_str)
        except ValueError:
            count = 1 

        sessions[chat_id]['data']['passengers'] = count_str
        sessions[chat_id]['data']['passengers_to_enter'] = count
        sessions[chat_id]['data']['current_passenger_num'] = 1
        sessions[chat_id]['data']['passenger_ages'] = [] 
        sessions[chat_id]['step'] = 'await_age' 
        save_sessions()
        
        edit_and_answer(
            call, 
            f"👥 **Passenger Ages**\n\nEnter the **age** for Passenger #1:",
            None
        )

    elif data.startswith("confirm_"):
        # --- BOOKING CONFIRMATION AND SEAT DEDUCTION ---
        bus_id = data.replace('confirm_', '', 1)
        found_buses = sessions[chat_id].get('found_buses', {})
        selected_bus = found_buses.get(bus_id)
        
        if not selected_bus:
            edit_and_answer(call, "❌ Error: Bus details not found. Starting over.", main_menu_keyboard())
            clear_session(chat_id)
            return

        pass_count = sessions[chat_id]['data'].get('passengers_to_enter', 1)
        
        seats_remaining_msg = ""
        final_fare_to_display = selected_bus['fare']
        contact = "N/A"
        
        if selected_bus['type'] == 'PROVIDER':
            # Find the actual service object in the services_db list
            for svc in services_db['services']:
                if svc['id'] == selected_bus['id']:
                    contact = svc.get('contact', 'N/A')
                    current_seats = svc.get('remaining_seats', svc.get('total_seats', 50))
                    
                    if current_seats < pass_count:
                        # Fail the booking due to lack of seats
                        bot.answer_callback_query(call.id, "❌ Not enough seats remaining on this service.", show_alert=True)
                        return 
                        
                    # Seat Management: Update seat count and save the persistence file
                    new_seats = current_seats - pass_count
                    svc['remaining_seats'] = new_seats
                    save_services() # <--- Seat Deduction Persistence Fix
                    
                    seats_remaining_msg = f"💺 **Seats Remaining:** {new_seats}"
                    break
        
        fare_display = f"Rs. {final_fare_to_display}" if final_fare_to_display != 'N/A' else "Estimated (Check operator)"

        confirmation_message = (
            f"✅ **BOOKING CONFIRMED for {pass_count} passenger(s)!** 🎉\n\n"
            f"🚍 **Service:** {selected_bus['name']}\n"
            f"💲 **Total Fare:** {fare_display}\n" 
            f"📞 **Contact:** {contact}\n"
            f"{seats_remaining_msg}\n\n"
            f"Thank you for using RoutAfare. Please be ready to board at the departure time."
        )

        edit_and_answer(call, confirmation_message, InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 New Search", callback_data="menu_main")))
        clear_session(chat_id)
        return

    # --- 3. PROVIDER MENU FLOW ---
    elif data == "prov_add":
        sessions[chat_id]['step'] = 'prov_enter_route'
        sessions[chat_id]['temp_service'] = {}
        save_sessions()
        edit_and_answer(call, "🆕 **Add Service**\n\nEnter the **Route Name** (e.g., Kandy-Colombo):")

    elif data.startswith("toggle_pay_"):
        method = data.replace("toggle_pay_", "")
        current = sessions[chat_id]['temp_service'].get('payment_methods', [])
        
        if method in current: current.remove(method)
        else: current.append(method)
            
        sessions[chat_id]['temp_service']['payment_methods'] = current
        save_sessions()
        
        # Only edit the reply markup, not the text
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, 
                                      reply_markup=payment_toggle_keyboard(current))
        bot.answer_callback_query(call.id)
        return

    elif data == "prov_save_service":
        new_service = sessions[chat_id]['temp_service']
        # Use a timestamp as a simple unique ID
        new_service['id'] = str(int(time.time())) 
        new_service['status'] = 'active'
        # Ensure remaining seats is set upon save
        new_service['remaining_seats'] = int(new_service.get('total_seats', 50)) 
        
        # Ensure prices are stored
        new_service['adult_fare'] = new_service.get('adult_fare', '0.0')
        new_service['teacher_fare'] = new_service.get('teacher_fare', new_service['adult_fare'])
        new_service['child_fare'] = new_service.get('child_fare', '0.0')
        
        services_db['services'].append(new_service)
        save_services() # <-- CRITICAL FIX: Save the new service to the persistent file
        
        edit_and_answer(call, "✅ **Service Saved Successfully!**", provider_menu_keyboard())
        clear_session(chat_id)
        return

    elif data == "prov_status":
        if not services_db['services']:
            bot.answer_callback_query(call.id, "No services added yet.")
            return

        kb = InlineKeyboardMarkup()
        for svc in services_db['services']:
            status_icon = "🟢 ACTIVE" if svc.get('status') == 'active' else "🔴 UNAVAILABLE"
            # Show seats remaining in the status view
            seats_info = f"({svc.get('remaining_seats', 'N/A')} seats)"
            kb.add(InlineKeyboardButton(f"{svc['service_name']} - {svc['route']} {seats_info} ({status_icon})", 
                                        callback_data=f"toggle_stat_{svc['id']}"))
        kb.add(InlineKeyboardButton("🔙 Back to Provider Menu", callback_data="provider_menu_return"))
        
        edit_and_answer(call, "Tap a service to **toggle its availability**:", kb)

    elif data.startswith("toggle_stat_"):
        s_id = data.split("_")[2]
        
        for svc in services_db['services']:
            if svc['id'] == s_id:
                curr = svc.get('status', 'active')
                svc['status'] = 'unavailable' if curr == 'active' else 'active'
                save_services() # <-- CRITICAL FIX: Save the status change to the persistent file
                break
        
        # Rebuild the menu to reflect the status change
        kb = InlineKeyboardMarkup()
        for svc in services_db['services']:
            status_icon = "🟢 ACTIVE" if svc.get('status') == 'active' else "🔴 UNAVAILABLE"
            seats_info = f"({svc.get('remaining_seats', 'N/A')} seats)"
            kb.add(InlineKeyboardButton(f"{svc['service_name']} - {svc['route']} {seats_info} ({status_icon})", 
                                        callback_data=f"toggle_stat_{svc['id']}"))
        kb.add(InlineKeyboardButton("🔙 Back to Provider Menu", callback_data="provider_menu_return"))
        
        edit_and_answer(call, "Tap a service to **toggle its availability**:", kb)
        return

    elif data == "provider_menu_return":
        edit_and_answer(call, "Manage your fleet:", provider_menu_keyboard())
        
    bot.answer_callback_query(call.id)


# --- Flask Server & Deployment ---
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    """Handles incoming Telegram updates from the webhook."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        if bot:
            bot.process_new_updates([update])
        return '', 200
    return 'Unsupported media type', 415

@app.route('/', methods=['GET'])
def index():
    """Simple status check for the server."""
    status = "Vertex AI Initialized" if vertex_predictor else "Using Local Fallback"
    # The message is a small internal note; the user's name is not actually Azan,
    # it's just a funny internal comment added from the previous step.
    return f'RoutAfare Bot FINAL Running. Status: {status}', 200

def set_initial_webhook():
    """Configures the Telegram webhook URL."""
    if bot is None: 
        print("FATAL: BOT_TOKEN not set. Cannot configure webhook.")
        return
    
    if not WEBHOOK_URL_BASE:
        print("WARNING: WEBHOOK_URL_BASE not set. Bot will only run locally or requires polling.")
        return

    full_webhook_url = f"{WEBHOOK_URL_BASE}{WEBHOOK_URL_PATH}"
    try:
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
    
    # If using local testing without a public URL, you'd switch to polling here:
    # bot.infinity_polling()
    
    # Assuming deployment environment (like Heroku or custom server)
    app.run(host='0.0.0.0', port=SERVER_PORT)

#!/usr/bin/env python3
"""
routAfare_bot_render_pg.py
Integrated Version using Render's PostgreSQL for data persistence.
- Replaces Firestore with asynchronous PostgreSQL connection (using psycopg2).
- Assumes DATABASE_URL is set in the environment (standard for Render).
- Vertex AI is kept as optional, relying on the environment variables for initialization.
"""

import os
import json
import re
import sys
import time
import asyncio
from datetime import datetime, timedelta

# Third-party libraries
try:
    # Ensure necessary libraries are installed via pip
    from dotenv import load_dotenv
    from flask import Flask, request
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    import pandas as pd
    
    # PostgreSQL Imports
    import psycopg2
    from psycopg2.extras import DictCursor, Json
    
    # Google Cloud Imports (kept for optional Vertex AI)
    from google.cloud import aiplatform
    from google.oauth2 import service_account
    
    DB_LIBRARIES_AVAILABLE = True
    GCP_LIBRARIES_AVAILABLE = True # Keep True to allow Vertex AI attempt
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

PROVIDER_PASSWORD = os.getenv('PROVIDER_PASSWORD', 'admin') 
DEFAULT_DISTANCE_KM = 5.0

# **RENDER POSTGRESQL CONFIGURATION**
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    print("CRITICAL: DATABASE_URL environment variable is missing. Cannot connect to PostgreSQL.")
    sys.exit(1)

# CSV File (remains local for bus routes)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE_NAME = os.getenv('CSV_FILE_NAME', 'final routa dataset for bus routes.csv')
CSV_FILE_PATH = os.path.join(BASE_DIR, CSV_FILE_NAME)

# --- POSTGRESQL INITIALIZATION & HELPERS ---

# Database connection pool (Postgres connections must be synchronous)
_db_pool = None

def init_db_pool():
    """Initializes the synchronous connection pool."""
    global _db_pool
    if _db_pool is None:
        try:
            # Simple connection to start, pool management is complex for this scope
            # Using simple connection for synchronous pool emulation
            _db_pool = psycopg2.connect(DATABASE_URL)
            print("✅ PostgreSQL Client Connected.")
            create_tables()
        except Exception as e:
            print(f"❌ Error connecting to PostgreSQL: {e}")
            sys.exit(1)
    return _db_pool

def create_tables():
    """Creates the necessary tables if they do not exist."""
    conn = init_db_pool()
    with conn.cursor() as cur:
        # services table stores all provider bus data
        cur.execute("""
            CREATE TABLE IF NOT EXISTS services (
                id TEXT PRIMARY KEY,
                route TEXT NOT NULL,
                service_name TEXT,
                driver TEXT,
                total_seats INTEGER,
                remaining_seats INTEGER,
                bus_type TEXT, -- ADDED: Bus type field
                adult_fare NUMERIC,
                teacher_fare NUMERIC,
                child_fare NUMERIC,
                contact TEXT,
                payment_methods JSONB,
                status TEXT
            );
        """)
        # sessions table stores temporary user session data
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY NULL,
                data JSONB
            );
        """)
        conn.commit()
    print("✅ PostgreSQL Tables Verified/Created.")

# Initialize the connection pool on startup
init_db_pool()

# --- PostgreSQL Data Persistence Helpers (Asynchronous Wrappers) ---
# We use asyncio.to_thread to run synchronous psycopg2 calls safely

async def get_session_async(chat_id):
    """Retrieves session data from Postgres."""
    def _fetch():
        conn = _db_pool
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT data FROM sessions WHERE chat_id = %s;", (str(chat_id),))
            row = cur.fetchone()
            return row['data'] if row else {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Error getting session for {chat_id}: {e}")
        return {}

async def save_session_async(chat_id, data):
    """Saves or updates session data to Postgres."""
    def _execute():
        conn = _db_pool
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (chat_id, data) 
                VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE 
                SET data = EXCLUDED.data;
                """,
                (str(chat_id), Json(data))
            )
            conn.commit()
    try:
        await asyncio.to_thread(_execute)
    except Exception as e:
        print(f"Error saving session for {chat_id}: {e}")

async def clear_session_async(chat_id):
    """Deletes session data from Postgres."""
    def _execute():
        conn = _db_pool
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE chat_id = %s;", (str(chat_id),))
            conn.commit()
    try:
        await asyncio.to_thread(_execute)
    except Exception as e:
        print(f"Error clearing session for {chat_id}: {e}")

async def get_all_services_async():
    """Retrieves all provider services from Postgres."""
    def _fetch():
        conn = _db_pool
        services = []
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM services;")
            for row in cur.fetchall():
                # Convert DictRow to standard dict and ensure numeric fields are converted
                service_dict = dict(row)
                for key in ['adult_fare', 'teacher_fare', 'child_fare']:
                    if key in service_dict and service_dict[key] is not None:
                         service_dict[key] = float(service_dict[key])
                services.append(service_dict)
            return {"services": services}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"Error getting all services: {e}")
        return {"services": []}

async def save_new_service_async(service_data):
    """Adds a new service to Postgres."""
    def _execute():
        conn = _db_pool
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO services (id, route, service_name, driver, total_seats, remaining_seats, bus_type, adult_fare, teacher_fare, child_fare, contact, payment_methods, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    service_data['id'], service_data['route'], service_data['service_name'], service_data['driver'], 
                    service_data['total_seats'], service_data['remaining_seats'], service_data['bus_type'], # Updated to include bus_type
                    service_data['adult_fare'], 
                    service_data['teacher_fare'], service_data['child_fare'], service_data['contact'], 
                    Json(service_data['payment_methods']), service_data['status']
                )
            )
            conn.commit()
    try:
        await asyncio.to_thread(_execute)
    except Exception as e:
        print(f"Error saving new service: {e}")

async def update_service_status_and_seats_async(service_id, update_data):
    """Updates status or seat count for an existing service."""
    def _execute():
        conn = _db_pool
        # Build dynamic UPDATE query
        set_clauses = []
        values = []
        for key, value in update_data.items():
            set_clauses.append(f"{key} = %s")
            # Handle JSONB for payment methods if necessary, though unlikely here
            values.append(value) 

        if not set_clauses: return # Nothing to update
        
        sql = f"UPDATE services SET {', '.join(set_clauses)} WHERE id = %s;"
        values.append(service_id)
        
        with conn.cursor() as cur:
            cur.execute(sql, tuple(values))
            conn.commit()
    try:
        await asyncio.to_thread(_execute)
    except Exception as e:
        print(f"Error updating service {service_id}: {e}")


# --- Synchronous Wrappers for Handlers ---
# Since telebot handlers are synchronous, we run the async functions inside them.

def sync_get_session(chat_id): return asyncio.run(get_session_async(chat_id))
def sync_save_session(chat_id, data): asyncio.run(save_session_async(chat_id, data))
def sync_clear_session(chat_id): asyncio.run(clear_session_async(chat_id))
def sync_get_all_services(): return asyncio.run(get_all_services_async())
def sync_save_new_service(data): asyncio.run(save_new_service_async(data))
def sync_update_service(service_id, update_data): asyncio.run(update_service_status_and_seats_async(service_id, update_data))


# --- CSV & Data Loading (Remains Local for Bus Routes) ---

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
    except Exception as e: 
        print(f"Error loading CSV data: {e}")
        return []
    
    unique_bus_cols = [c for c in ['route_id', 'bus_route', 'bus_type_num', 'direction'] if c in df.columns]
    if not unique_bus_cols: 
        print("Warning: CSV is missing required columns (route_id, bus_route, etc.)")
        return []

    df_unique = df.groupby(unique_bus_cols).agg(times=('time_slot', lambda x: list(x.dropna().unique()))).reset_index()
    
    bus_data_list = []
    bus_id_counter = 1
    for _, row in df_unique.iterrows():
        all_times = set()
        for slot in row.get('times', []): 
            all_times.update(generate_departure_times(slot))
            
        if not all_times: continue
        
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
    # Load all dynamic services from Postgres
    services_db = sync_get_all_services()
    
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
        vertex_predictor = aiplatform.Endpoint(endpoint_name=VERTEX_ENDPOINT_ID) 
        print("✅ Vertex AI Prediction Endpoint Initialized.")

    except Exception as e:
        print(f"❌ Error initializing Vertex AI. Falling back to local calculation. Error: {e}")
        vertex_predictor = None 


# --- Fare Calculation Logic ---
def get_fare_prediction_safe(data, predictor):
    """
    Calculates/Predicts a simple standard adult fare for CSV/Public buses.
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
            if age > 12: # Adults and Teachers (Lacking input to distinguish teacher/adult)
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
        InlineKeyboardButton("❌ Exit Program", callback_data="program_exit")
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
    sync_clear_session(message.chat.id)
    bot.send_message(
        message.chat.id, 
        "👋 **Welcome to routAfare!**\n\nPlease select your role:",
        reply_markup=main_menu_keyboard(),
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = str(message.chat.id)
    text = message.text.strip()
    
    # Retrieve current session data
    session_data = sync_get_session(chat_id)
    
    # List of steps where text input is expected
    active_steps = [
        'provider_auth', 'prov_enter_route', 'prov_enter_service_name', 
        'prov_enter_driver', 'prov_enter_bus_type', 'prov_enter_seats', 'prov_enter_adult_fare', # ADDED: prov_enter_bus_type
        'prov_enter_teacher_fare', 'prov_enter_child_fare', 'prov_enter_contact', 
        'await_age', 'await_time'
    ]
    
    current_step = session_data.get('step')

    # FIX: Robust Error Handling - If not in an active flow, show error/help message
    if current_step not in active_steps:
        send_error_message(int(chat_id))
        return

    # --- PROVIDER AUTH ---
    if current_step == 'provider_auth':
        if text == PROVIDER_PASSWORD:
            session_data['step'] = 'provider_menu'
            sync_save_session(chat_id, session_data)
            bot.send_message(int(chat_id), "🔓 **Access Granted**\nManage your fleet:", reply_markup=provider_menu_keyboard(), parse_mode='Markdown')
        else:
            bot.send_message(int(chat_id), "❌ Wrong Password. Try again or /start")

    # --- PROVIDER ADDING DATA FLOW ---
    elif current_step == 'prov_enter_route':
        if 'temp_service' not in session_data: session_data['temp_service'] = {}
        session_data['temp_service']['route'] = text
        session_data['step'] = 'prov_enter_service_name' 
        sync_save_session(chat_id, session_data)
        bot.send_message(int(chat_id), "🚍 Enter the **Bus Service/Company Name** (e.g., ABC Express, Private Bus):", parse_mode='Markdown')

    elif current_step == 'prov_enter_service_name':
        session_data['temp_service']['service_name'] = text
        session_data['step'] = 'prov_enter_driver'
        sync_save_session(chat_id, session_data)
        bot.send_message(int(chat_id), "🧑 Enter **Driver Name**:", parse_mode='Markdown')

    elif current_step == 'prov_enter_driver':
        session_data['temp_service']['driver'] = text
        session_data['step'] = 'prov_enter_bus_type' # NEW STEP: Bus Type
        sync_save_session(chat_id, session_data)
        bot.send_message(int(chat_id), "🚌 Enter the **Bus Type** (e.g., Luxury, Semi-Luxury, Normal):", parse_mode='Markdown')

    elif current_step == 'prov_enter_bus_type': # NEW HANDLER: Store Bus Type
        session_data['temp_service']['bus_type'] = text
        session_data['step'] = 'prov_enter_seats' 
        sync_save_session(chat_id, session_data)
        bot.send_message(int(chat_id), "💺 Enter the **Total Number of Seats** available (e.g., 50):", parse_mode='Markdown')

    elif current_step == 'prov_enter_seats':
        try:
            seats = int(text)
            if seats <= 0: raise ValueError
            session_data['temp_service']['total_seats'] = seats
            # Seat Management: Set remaining seats immediately
            session_data['temp_service']['remaining_seats'] = seats 
            session_data['step'] = 'prov_enter_adult_fare' 
            sync_save_session(chat_id, session_data)
            
            # Start fare collection flow
            bot.send_message(int(chat_id), "💵 Enter **Adult/Standard Fare Price** (e.g., 150.00):", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid number of seats. Please enter a positive whole number.")

    elif current_step == 'prov_enter_adult_fare':
        try:
            fare = float(text)
            session_data['temp_service']['adult_fare'] = fare
            session_data['step'] = 'prov_enter_teacher_fare' 
            sync_save_session(chat_id, session_data)
            # Prompt for Teacher Fare
            bot.send_message(int(chat_id), "👨‍🏫 Enter **Teacher Fare Price** (e.g., 100.00). Enter the same as Adult Fare if no discount:", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 150.00).")

    elif current_step == 'prov_enter_teacher_fare':
        try:
            fare = float(text)
            session_data['temp_service']['teacher_fare'] = fare
            session_data['step'] = 'prov_enter_child_fare' 
            sync_save_session(chat_id, session_data)
            # Prompt for Child Fare
            bot.send_message(int(chat_id), "👶 Enter **Child Fare Price** (e.g., 75.00). Enter 0 if children travel free:", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 100.00).")

    elif current_step == 'prov_enter_child_fare':
        try:
            fare = float(text)
            session_data['temp_service']['child_fare'] = fare
            session_data['step'] = 'prov_enter_contact'
            sync_save_session(chat_id, session_data)
            # Proceed to contact
            bot.send_message(int(chat_id), "📞 Enter **Contact Number** (e.g., +9477xxxxxxx):", parse_mode='Markdown')
        except ValueError:
            bot.send_message(int(chat_id), "❌ Invalid fare format. Please enter a number (e.g., 75.00).")


    elif current_step == 'prov_enter_contact':
        # Simple validation for a phone number
        if re.match(r'^\+?[\d\s-]{5,}$', text):
            session_data['temp_service']['contact'] = text
            session_data['step'] = 'prov_select_payment'
            session_data['temp_service']['payment_methods'] = [] 
            sync_save_session(chat_id, session_data)
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
        current_data = session_data['data']
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
                session_data['step'] = 'await_time'
                sync_save_session(chat_id, session_data)
                # Prompt for time
                bot.send_message(int(chat_id), "⏰ Enter your departure time in **HH:MM** (example: 13:45):", parse_mode='Markdown')
            else:
                # Continue asking for the next passenger's age
                session_data['step'] = 'await_age'
                sync_save_session(chat_id, session_data)
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

        session_data['data']['time'] = text
        
        # Predict a simple fare for *display* on CSV/Public routes
        predicted_fare_placeholder = get_fare_prediction_safe(session_data['data'], vertex_predictor)
        session_data['data']['predicted_fare'] = predicted_fare_placeholder
        sync_save_session(chat_id, session_data)
        
        bot.send_message(int(chat_id), 'Calculating fare and searching for matching buses... 🔎')
        
        s_data = session_data['data']
        route_name = s_data.get('selected_route')
        time_input = s_data.get('time')

        # Get the latest services from Postgres
        services_db = sync_get_all_services()
        
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
            
            bus_type = svc.get('bus_type', 'N/A')
            
            bus_details = {
                'id': svc['id'], 
                'type': 'PROVIDER',
                'name': f"{svc.get('service_name', 'N/A')} ({bus_type}) | Driver: {svc.get('driver')}", # Included Bus Type
                'details_text': f"Total Fare: Rs. {final_calculated_fare} | Contact: {svc.get('contact')} | Payment: {pay_str} | {seats_info}", # Explicitly display 'Total Fare'
                'fare': final_calculated_fare
            }
            final_bus_list.append(bus_details)

        if not final_bus_list:
            bot.send_message(int(chat_id), "❌ No buses found for that route/time.")
            sync_clear_session(chat_id)
            return

        session_data['found_buses'] = {b['id']: b for b in final_bus_list}
        sync_save_session(chat_id, session_data)

        keyboard = InlineKeyboardMarkup()
        for bus in final_bus_list:
            keyboard.add(InlineKeyboardButton(f"✅ Book: {bus['name']} - {bus['details_text']}", callback_data=f"confirm_{bus['id']}"))

        fare_text = f"🚌 *Your Search Summary*\nRoute: {route_name}\nTime: {time_input}\nPassengers: {len(s_data.get('passenger_ages', []))}\n\n**Select a bus to Confirm Booking:**"
        
        bot.send_message(int(chat_id), fare_text, parse_mode='Markdown', reply_markup=keyboard)
        session_data['step'] = 'await_bus_select'
        sync_save_session(chat_id, session_data)
        return

# --- CALLBACK QUERY HANDLER (The Core UI Logic) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    chat_id = str(call.message.chat.id)
    data = call.data
    
    # Retrieve current session data
    session_data = sync_get_session(chat_id)
    
    def edit_and_answer(call, text, reply_markup=None):
        """Helper to edit the message and answer the callback query, setting Markdown mode."""
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              reply_markup=reply_markup, parse_mode='Markdown')
        bot.answer_callback_query(call.id)

    # --- 1. MAIN MENU/ROLES ---
    if data == "menu_main":
        sync_clear_session(chat_id)
        edit_and_answer(call, "👋 **Welcome to routAfare!**\n\nPlease select your role:", main_menu_keyboard())

    # --- EXIT OPTION ---
    elif data == "program_exit":
        sync_clear_session(chat_id)
        bot.send_message(
            int(chat_id), 
            "👋 **Goodbye!**\n\nYour session has been cleared. Type `/start` to begin a new journey.",
            parse_mode='Markdown'
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id) 
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Exiting program.")
        return

    elif data == "role_passenger":
        # Ensure we get the latest routes which may include new Postgres services
        routes = get_all_routes()
        if not routes:
            bot.answer_callback_query(call.id, "No routes available. Contact the admin.")
            return
            
        session_data = {'step': 'pass_select_route', 'data': {}}
        sync_save_session(chat_id, session_data)
        
        kb = InlineKeyboardMarkup(row_width=2)
        for r in routes:
            kb.add(InlineKeyboardButton(r, callback_data=f"route_{r}"))
        kb.add(InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
        
        edit_and_answer(call, "📍 **Select your Route:**", kb)

    elif data == "role_provider":
        session_data = {'step': 'provider_auth'}
        sync_save_session(chat_id, session_data)
        # Delete the previous menu message to prompt password entry
        bot.delete_message(call.message.chat.id, call.message.message_id) 
        bot.send_message(int(chat_id), "🔒 Enter **Provider Password**:", parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    # --- 2. PASSENGER FLOW (Booking Confirmation) ---
    elif data.startswith("route_"):
        route = data.split("_")[1]
        session_data['data']['selected_route'] = route
        session_data['step'] = 'pass_count'
        sync_save_session(chat_id, session_data)
        edit_and_answer(call, f"Selected: **{route}**\n\n👥 **How many passengers?**", passenger_count_keyboard())

    elif data.startswith("pass_count_"):
        count_str = data.replace("pass_count_", "").replace('+', '')
        try:
            count = int(count_str)
        except ValueError:
            count = 1 

        session_data['data']['passengers'] = count_str
        session_data['data']['passengers_to_enter'] = count
        session_data['data']['current_passenger_num'] = 1
        session_data['data']['passenger_ages'] = [] 
        session_data['step'] = 'await_age' 
        sync_save_session(chat_id, session_data)
        
        edit_and_answer(
            call, 
            f"👥 **Passenger Ages**\n\nEnter the **age** for Passenger #1:",
            None
        )

    elif data.startswith("confirm_"):
        # --- BOOKING CONFIRMATION AND SEAT DEDUCTION ---
        bus_id = data.replace('confirm_', '', 1)
        found_buses = session_data.get('found_buses', {})
        selected_bus = found_buses.get(bus_id)
        
        if not selected_bus:
            edit_and_answer(call, "❌ Error: Bus details not found. Starting over.", main_menu_keyboard())
            sync_clear_session(chat_id)
            return

        pass_count = session_data['data'].get('passengers_to_enter', 1)
        
        seats_remaining_msg = ""
        final_fare_to_display = selected_bus['fare']
        contact = "N/A"
        
        if selected_bus['type'] == 'PROVIDER':
            
            # Fetch the latest services data directly from Postgres
            services_db = sync_get_all_services()
            
            # Find the actual service object in the services list
            svc = next((s for s in services_db['services'] if s['id'] == selected_bus['id']), None)
            
            if svc:
                contact = svc.get('contact', 'N/A')
                current_seats = svc.get('remaining_seats', svc.get('total_seats', 50))
                
                if current_seats < pass_count:
                    # Fail the booking due to lack of seats
                    bot.answer_callback_query(call.id, "❌ Not enough seats remaining on this service.", show_alert=True)
                    return 
                    
                # Seat Management: Update seat count and save to Postgres
                new_seats = current_seats - pass_count
                
                # Update Postgres document with new remaining seats
                sync_update_service(svc['id'], {'remaining_seats': new_seats})
                
                seats_remaining_msg = f"💺 **Final Seats Remaining:** {new_seats}" # Display final remaining seats
            else:
                 bot.answer_callback_query(call.id, "❌ Service not found in database.", show_alert=True)
                 return
        elif selected_bus['type'] == 'CSV':
            seats_remaining_msg = "💺 **Seat availability for Public Bus is estimated.**"


        fare_display = f"Rs. {final_fare_to_display}" if final_fare_to_display != 'N/A' else "Estimated (Check operator)"

        confirmation_message = (
            f"✅ **BOOKING CONFIRMED for {pass_count} passenger(s)!** 🎉\n\n"
            f"🚍 **Service:** {selected_bus['name']}\n"
            f"💲 **Total Predicted Bus Fare:** {fare_display}\n" # Explicitly display total predicted fare
            f"📞 **Contact:** {contact}\n"
            f"{seats_remaining_msg}\n\n"
            f"Thank you for using RoutAfare. Please be ready to board at the departure time."
        )

        edit_and_answer(call, confirmation_message, InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 New Search", callback_data="menu_main")))
        sync_clear_session(chat_id)
        return

    # --- 3. PROVIDER MENU FLOW ---
    elif data == "prov_add":
        session_data['step'] = 'prov_enter_route'
        session_data['temp_service'] = {}
        sync_save_session(chat_id, session_data)
        edit_and_answer(call, "🆕 **Add Service**\n\nEnter the **Route Name** (e.g., Kandy-Colombo):")

    elif data.startswith("toggle_pay_"):
        method = data.replace("toggle_pay_", "")
        current = session_data['temp_service'].get('payment_methods', [])
        
        if method in current: current.remove(method)
        else: current.append(method)
            
        session_data['temp_service']['payment_methods'] = current
        sync_save_session(chat_id, session_data)
        
        # Only edit the reply markup, not the text
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, 
                                      reply_markup=payment_toggle_keyboard(current))
        bot.answer_callback_query(call.id)
        return

    elif data == "prov_save_service":
        new_service = session_data['temp_service']
        # Use a timestamp as a simple unique ID
        new_service['id'] = str(int(time.time())) 
        new_service['status'] = 'active'
        
        # Ensure prices and seats are stored as numbers 
        new_service['total_seats'] = int(new_service.get('total_seats', 50))
        new_service['remaining_seats'] = new_service['total_seats'] 
        new_service['adult_fare'] = float(new_service.get('adult_fare', 0.0))
        new_service['teacher_fare'] = float(new_service.get('teacher_fare', new_service['adult_fare']))
        new_service['child_fare'] = float(new_service.get('child_fare', 0.0))
        
        # Ensure bus_type exists
        if 'bus_type' not in new_service:
             new_service['bus_type'] = 'Standard'

        sync_save_new_service(new_service) # Save the new service to Postgres
        
        edit_and_answer(call, "✅ **Service Saved Successfully!**", provider_menu_keyboard())
        sync_clear_session(chat_id)
        return

    elif data == "prov_status" or data == "provider_menu_return":
        # Get the latest services from Postgres
        services_db = sync_get_all_services()
        
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
        
        edit_and_answer(call, "Tap a service to **toggle availability** (Holiday/Weather):", kb)
        return

    elif data.startswith("toggle_stat_"):
        # --- TOGGLE SERVICE STATUS LOGIC ---
        s_id = data.split("_")[2]
        
        # 1. Fetch current status
        services_db = sync_get_all_services()
        svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
        
        if svc:
            # 2. Determine new status
            new_status = 'unavailable' if svc.get('status') == 'active' else 'active'
            
            # 3. Update Postgres table
            sync_update_service(s_id, {'status': new_status})
            
            # 4. Re-display the status menu
            # Re-calling prov_status logic to refresh the keyboard
            handle_query(call._replace(data="prov_status")) 
        else:
            bot.answer_callback_query(call.id, "Service not found.")
        
        return 

# --- FLASK/WEBHOOK SETUP (Assuming deployment on a platform like Render) ---
if BOT_TOKEN and WEBHOOK_URL_BASE:
    app = Flask(__name__)
    
    @app.route(WEBHOOK_URL_PATH, methods=['POST'])
    def webhook():
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '!', 200
        else:
            return '', 200 
    
    # if __name__ == '__main__':
    #     app.run(host='0.0.0.0', port=SERVER_PORT, debug=True)
    pass
else:
    print("WARNING: BOT_TOKEN or WEBHOOK_URL_BASE not set. Bot running in polling mode (for local test only).")

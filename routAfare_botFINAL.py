#!/usr/bin/env python3
"""
routAfare_bot_render_pg.py
Integrated Version using Render's PostgreSQL for data persistence.
- Replaces Firestore with asynchronous PostgreSQL connection (using psycopg2).
- Assumes DATABASE_URL is set in the environment (standard for Render).
- Vertex AI is kept as optional, relying on the environment variables for initialization.
"""
'''
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
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
# DATABASE_URL is automatically set by Render
DATABASE_URL = os.getenv('DATABASE_URL')
# Max connection retries for PostgreSQL
MAX_DB_RETRIES = 5
RETRY_DELAY = 5 # seconds

# Vertex AI (Optional)
GCP_PROJECT = os.getenv('GCP_PROJECT')
GCP_LOCATION = os.getenv('GCP_LOCATION')
VERTEX_MODEL = os.getenv('VERTEX_MODEL')
SERVICE_ACCOUNT_JSON = os.getenv('SERVICE_ACCOUNT_JSON')

# --- Global State and Initialization ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='Markdown') if BOT_TOKEN else None
vertex_predictor = None

if GCP_LIBRARIES_AVAILABLE and GCP_PROJECT and GCP_LOCATION and VERTEX_MODEL and SERVICE_ACCOUNT_JSON:
    try:
        # Initialize Vertex AI client using service account key
        credentials = service_account.Credentials.from_service_account_info(json.loads(SERVICE_ACCOUNT_JSON))
        aiplatform.init(project=GCP_PROJECT, location=GCP_LOCATION, credentials=credentials)
        
        # Load the custom model endpoint
        # Assuming VERTEX_MODEL holds the full endpoint name or ID
        vertex_predictor = aiplatform.Endpoint(VERTEX_MODEL)
        print("✅ Vertex AI Predictor Initialized.")
    except Exception as e:
        print(f"❌ Vertex AI Initialization Failed: {e}")
        vertex_predictor = None
else:
    print("⚠️ Vertex AI environment variables missing or libraries unavailable. Using local fallbacks.")


# --- PostgreSQL Connection Management ---

def get_db_connection():
    """
    Establishes a PostgreSQL connection. Retries on failure.
    Returns: A psycopg2 connection object or None.
    """
    if not DATABASE_URL:
        print("FATAL: DATABASE_URL is not set.")
        return None
    
    # Render's DATABASE_URL is typically a postgres:// URL, but psycopg2
    # prefers the connection parameters directly. We will rely on psycopg2
    # being able to parse the URL string directly or using a helper.
    # For a standard Render setup, the URL string should work directly.

    for i in range(MAX_DB_RETRIES):
        try:
            # Attempt to connect using the URL
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = True
            print("✅ PostgreSQL Client Connected.")
            return conn
        except Exception as e:
            print(f"❌ Error connecting to PostgreSQL (Attempt {i+1}/{MAX_DB_RETRIES}): {e}")
            if i < MAX_DB_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                print("FATAL: Failed to connect to PostgreSQL after multiple retries.")
                return None
    return None

def sync_execute_db_operation(sql_query, params=None, fetch=False):
    """Executes a database operation (sync)."""
    conn = get_db_connection()
    if conn is None:
        return None if fetch else False

    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(sql_query, params)
            if fetch:
                # If fetching, get all results
                return cur.fetchall()
            return True
    except Exception as e:
        print(f"Database operation failed: {e}")
        return None if fetch else False
    finally:
        if conn:
            conn.close()

def sync_create_tables():
    """Creates necessary PostgreSQL tables if they don't exist."""
    
    # IMPORTANT FIX: Removed 'NULL' from PRIMARY KEY constraint, 
    # as PRIMARY KEY implies NOT NULL and the 'TEXT PRIMARY NULL' syntax is invalid.
    # Corrected SQL statements below.
    create_bot_state_table = """
    CREATE TABLE IF NOT EXISTS bot_state (
        chat_id TEXT PRIMARY KEY, 
        state_key TEXT NOT NULL,
        data JSONB,
        last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """

    create_services_table = """
    CREATE TABLE IF NOT EXISTS services (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        name TEXT NOT NULL,
        route TEXT,
        fare JSONB,
        total_seats INTEGER,
        remaining_seats INTEGER,
        status TEXT,
        last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    if sync_execute_db_operation(create_bot_state_table) and \
       sync_execute_db_operation(create_services_table):
        print("✅ PostgreSQL tables ensured (bot_state, services).")
        return True
    else:
        print("❌ Failed to create PostgreSQL tables.")
        return False

# Ensure tables exist on startup
if DATABASE_URL:
    sync_create_tables()


# --- Database CRUD Operations ---

# State functions
def sync_set_state(chat_id, state_key, data=None):
    """Sets the state and data for a given chat_id."""
    sql = """
    INSERT INTO bot_state (chat_id, state_key, data) 
    VALUES (%s, %s, %s)
    ON CONFLICT (chat_id) 
    DO UPDATE SET state_key = EXCLUDED.state_key, 
                  data = EXCLUDED.data,
                  last_updated = CURRENT_TIMESTAMP;
    """
    # Use Json() to ensure psycopg2 correctly formats the Python dict/None to JSONB
    return sync_execute_db_operation(sql, (str(chat_id), state_key, Json(data or {})))

def sync_get_state(chat_id):
    """Retrieves the state and data for a given chat_id."""
    sql = "SELECT state_key, data FROM bot_state WHERE chat_id = %s;"
    result = sync_execute_db_operation(sql, (str(chat_id),), fetch=True)
    if result:
        # DictCursor returns a list of dictionaries, we want the first one
        return result[0]['state_key'], result[0]['data']
    return None, {} # Default state and empty data

# Service functions
def sync_save_service(service_data):
    """Saves a new service to the services table."""
    sql = """
    INSERT INTO services (id, provider_id, name, route, fare, total_seats, remaining_seats, status) 
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) 
    DO UPDATE SET provider_id = EXCLUDED.provider_id,
                  name = EXCLUDED.name,
                  route = EXCLUDED.route,
                  fare = EXCLUDED.fare,
                  total_seats = EXCLUDED.total_seats,
                  remaining_seats = EXCLUDED.remaining_seats,
                  status = EXCLUDED.status,
                  last_updated = CURRENT_TIMESTAMP;
    """
    # Fare is stored as JSONB
    return sync_execute_db_operation(sql, (
        service_data['id'], 
        service_data['provider_id'], 
        service_data['name'], 
        service_data['route'], 
        Json(service_data.get('fare', {})), # Ensure fare is handled as Json
        service_data.get('total_seats'),
        service_data.get('remaining_seats'),
        service_data.get('status') or 'active'
    ))

def sync_get_all_services(provider_id=None):
    """Retrieves all services, optionally filtered by provider_id."""
    if provider_id:
        sql = "SELECT * FROM services WHERE provider_id = %s ORDER BY id;"
        results = sync_execute_db_operation(sql, (provider_id,), fetch=True)
    else:
        sql = "SELECT * FROM services ORDER BY id;"
        results = sync_execute_db_operation(sql, fetch=True)
        
    # Result is a list of DictRow objects; convert to standard list of dicts for consistency
    services_list = [dict(row) for row in results] if results else []
    return {"services": services_list}

def sync_update_service(service_id, updates):
    """Updates specific fields of a service."""
    set_clauses = []
    params = []
    
    for key, value in updates.items():
        # Handle JSONB fields specially
        if key == 'fare':
            set_clauses.append(f"{key} = %s")
            params.append(Json(value))
        elif key in ['total_seats', 'remaining_seats', 'name', 'route', 'status']:
            set_clauses.append(f"{key} = %s")
            params.append(value)
            
    if not set_clauses:
        return False

    set_clause_str = ", ".join(set_clauses)
    params.append(service_id)
    
    sql = f"UPDATE services SET {set_clause_str}, last_updated = CURRENT_TIMESTAMP WHERE id = %s;"
    return sync_execute_db_operation(sql, tuple(params))


# --- Bot State Keys ---
STATE_START = 'start'
STATE_AWAIT_CHOICE = 'await_choice'
STATE_AWAIT_SERVICE_NAME = 'await_service_name'
STATE_AWAIT_ROUTE = 'await_route'
STATE_AWAIT_SEATS = 'await_seats'
STATE_AWAIT_ADULT_FARE = 'await_adult_fare'
STATE_AWAIT_CHILD_FARE = 'await_child_fare'
STATE_AWAIT_TEACHER_FARE = 'await_teacher_fare'
STATE_AWAIT_PASSENGER_COUNT = 'await_passenger_count'
STATE_AWAIT_PASSENGER_ADULT = 'await_passenger_adult'
STATE_AWAIT_PASSENGER_CHILD = 'await_passenger_child'
STATE_AWAIT_PASSENGER_TEACHER = 'await_passenger_teacher'


# --- Core Markup Builders ---

def build_main_menu(is_provider):
    """Builds the main start menu."""
    markup = InlineKeyboardMarkup()
    
    if is_provider:
        # Provider Menu
        markup.row(InlineKeyboardButton("➕ Register New Service", callback_data="prov_register"))
        markup.row(InlineKeyboardButton("🚦 View/Toggle Status", callback_data="prov_status"))
        markup.row(InlineKeyboardButton("💰 View Revenue", callback_data="prov_revenue"))
    else:
        # Customer Menu
        markup.row(InlineKeyboardButton("🚌 Search Services", callback_data="cust_search"))
        markup.row(InlineKeyboardButton("🎫 View My Bookings", callback_data="cust_bookings"))
    
    markup.row(InlineKeyboardButton("🔄 Change Role", callback_data="change_role"))
    markup.row(InlineKeyboardButton("🚪 Exit Program", callback_data="exit"))
    return markup

def build_service_list_markup(services, callback_prefix):
    """Builds a list of services as inline buttons."""
    markup = InlineKeyboardMarkup()
    if not services:
        markup.add(InlineKeyboardButton("No services found.", callback_data="ignore"))
    else:
        for svc in services:
            # Display name, route, and status
            status_emoji = '🟢' if svc.get('status', 'active') == 'active' else '🔴'
            button_text = f"{status_emoji} {svc['name']} ({svc['route'] or 'N/A'})"
            callback_data = f"{callback_prefix}:{svc['id']}"
            markup.row(InlineKeyboardButton(button_text, callback_data=callback_data))
    
    markup.row(InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start"))
    return markup


# --- Text Sanitization ---

def sanitize_text(text):
    """Removes non-standard characters that can break Telegram Markdown."""
    # Escape characters that have meaning in Telegram Markdown V2: _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !
    # We only escape the commonly problematic ones for simple text input.
    if text is None:
        return ""
    
    # Replace single backslashes with double backslashes for all commonly used markdown characters
    return re.sub(r'([_*\[\]()~`>#+\-={}.!])', r'\\\1', text)


# --- State Management Decorator ---

def with_state(handler):
    """Decorator to load and pass chat state and data to the handler."""
    def wrapper(message, *args, **kwargs):
        chat_id = message.chat.id
        current_state, state_data = sync_get_state(chat_id)
        
        # Check if the incoming message type matches the expected handler's type
        # For text messages
        if isinstance(message, telebot.types.Message):
            # If the current state matches the handler's name, proceed
            if current_state == handler.__name__:
                # Pass message, state_data
                return handler(message, state_data)
        
        # For callback queries, the state check happens inside handle_query
        # This decorator is primarily for text handlers
        
        # If the state doesn't match, or it's an unexpected message type,
        # fallback to the main start menu.
        else:
            # If it's a message but not matching state, maybe send the main menu
            # or ignore, depending on the bot's flow. We'll handle state transitions
            # explicitly in each function.
            pass

    return wrapper


# --- Helper Functions ---

def format_fare_info(fare_data):
    """Formats fare data for display."""
    lines = []
    if 'Adult' in fare_data:
        lines.append(f"• Adult: *GH¢{fare_data['Adult']:.2f}*")
    if 'Child' in fare_data:
        lines.append(f"• Child: *GH¢{fare_data['Child']:.2f}*")
    if 'Teacher' in fare_data:
        lines.append(f"• Teacher/Student: *GH¢{fare_data['Teacher']:.2f}*")
    return "\n".join(lines)


# --- Start/Role Selection ---

@bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    """Handles /start command and sends the initial role selection."""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Check if user has a role preference in state data
    state, data = sync_get_state(chat_id)
    role = data.get('role')

    if role in ['provider', 'customer']:
        # If role is already set, go to the main menu for that role
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, data)
        text = f"Welcome back! You are currently signed in as a *{role.capitalize()}*.\n\nWhat would you like to do?"
        bot.send_message(chat_id, text, reply_markup=build_main_menu(role == 'provider'))
    else:
        # Ask for role selection
        sync_set_state(chat_id, STATE_START)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🚌 I am a Customer", callback_data="select_role:customer"))
        markup.row(InlineKeyboardButton("🛠️ I am a Service Provider", callback_data="select_role:provider"))
        
        bot.send_message(chat_id, 
                         "*Welcome to RoutAfare!* \n\nPlease select your role to proceed.", 
                         reply_markup=markup)


# --- Callback Query Handler ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    """Handles all incoming inline keyboard button presses."""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data
    
    # 1. Handle common/role selection queries first
    if data.startswith("select_role:"):
        role = data.split(":")[1]
        
        # Update user's role in state data
        state, state_data = sync_get_state(chat_id)
        state_data['role'] = role
        
        # Transition to main menu state
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        
        bot.edit_message_text(f"Role set to *{role.capitalize()}*.\n\nWhat would you like to do?", 
                              chat_id, message_id, 
                              reply_markup=build_main_menu(role == 'provider'))
        bot.answer_callback_query(call.id, f"Role switched to {role.capitalize()}")
        return

    # 2. Handle main menu and navigation queries
    elif data == "start":
        # Re-triggering the start logic to get the correct menu based on role
        send_welcome(call.message)
        bot.answer_callback_query(call.id)
        return

    elif data == "change_role":
        # Remove role from state and restart
        state, state_data = sync_get_state(chat_id)
        state_data.pop('role', None)
        sync_set_state(chat_id, STATE_START, state_data)
        
        # Edit message to show role selection again
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🚌 I am a Customer", callback_data="select_role:customer"))
        markup.row(InlineKeyboardButton("🛠️ I am a Service Provider", callback_data="select_role:provider"))
        
        bot.edit_message_text("*Please select your new role to proceed.*", 
                              chat_id, message_id, 
                              reply_markup=markup)
        bot.answer_callback_query(call.id, "Changing role...")
        return
        
    elif data == "exit":
        # Clear state and data
        sync_set_state(chat_id, STATE_START, {})
        bot.edit_message_text("Thank you for using RoutAfare! Type /start to begin again.", 
                              chat_id, message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Program exited.")
        return
        
    elif data == "ignore":
        bot.answer_callback_query(call.id, "Nothing to do here.")
        return

    # 3. Handle Provider specific queries
    state, state_data = sync_get_state(chat_id)
    role = state_data.get('role')
    
    if role == 'provider':
        if data == "prov_register":
            # 1. Initialize data for new service
            state_data['new_service'] = {'provider_id': str(call.from_user.id)}
            sync_set_state(chat_id, STATE_AWAIT_SERVICE_NAME, state_data)
            
            # 2. Ask for name
            bot.edit_message_text("📝 *Service Registration - Step 1/6: Name*\n\nPlease enter the unique name for your new service (e.g., Accra Express, Daily Commute 01).", 
                                  chat_id, message_id, 
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))
            bot.answer_callback_query(call.id)
            return

        elif data == "prov_status":
            # Display all provider's services and their status
            provider_id = str(call.from_user.id)
            services_db = sync_get_all_services(provider_id=provider_id)
            services = services_db['services']
            
            # Markup to toggle status
            markup = build_service_list_markup(services, "toggle_status")
            
            status_text = "*🚦 Service Status Management*\n\nTap a service below to instantly toggle its availability (Active 🟢 / Unavailable 🔴)."
            
            # Check if we are editing an existing message or sending new
            try:
                bot.edit_message_text(status_text, chat_id, message_id, reply_markup=markup)
            except Exception:
                bot.send_message(chat_id, status_text, reply_markup=markup)
                
            bot.answer_callback_query(call.id)
            return
            
        elif data.startswith("toggle_status:"):
            # Logic to toggle the status of a service
            s_id = data.split(":")[1]
            
            # 1. Fetch current status
            services_db = sync_get_all_services()
            # Need to search all services since we only fetched provider's services previously
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
            
        elif data == "prov_revenue":
            # Placeholder for revenue logic
            provider_id = str(call.from_user.id)
            # In a real app, this would query a 'bookings' or 'transactions' table
            
            # Simple placeholder calculation based on total seats for now
            services_db = sync_get_all_services(provider_id=provider_id)
            total_potential_revenue = 0
            
            for svc in services_db['services']:
                # Assuming all remaining seats were booked at adult fare for simplicity
                adult_fare = svc.get('fare', {}).get('Adult', 0)
                seats_used = svc.get('total_seats', 0) - svc.get('remaining_seats', 0)
                total_potential_revenue += seats_used * adult_fare
                
            revenue_text = f"*💰 Your Revenue Report (Simplified)*\n\nTotal Potential Revenue (Based on seats used at Adult Fare): *GH¢{total_potential_revenue:.2f}*\n\n_Note: This is a placeholder. A real system requires booking logs._"

            # Check if we are editing an existing message or sending new
            try:
                bot.edit_message_text(revenue_text, chat_id, message_id, reply_markup=build_main_menu(True))
            except Exception:
                bot.send_message(chat_id, revenue_text, reply_markup=build_main_menu(True))
            
            bot.answer_callback_query(call.id)
            return

    # 4. Handle Customer specific queries
    elif role == 'customer':
        if data == "cust_search":
            # Go to Service Selection
            services_db = sync_get_all_services()
            available_services = [s for s in services_db['services'] if s.get('status', 'active') == 'active']
            
            if not available_services:
                bot.edit_message_text("⚠️ No services are currently active. Please check back later.",
                                      chat_id, message_id, 
                                      reply_markup=build_main_menu(False))
                bot.answer_callback_query(call.id, "No active services.")
                return
                
            # Store available services in state data (to refer to when selecting)
            state_data['available_services'] = [s['id'] for s in available_services]
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            
            # Build list for customer selection
            markup = build_service_list_markup(available_services, "select_service")
            
            bot.edit_message_text("*🚌 Available RoutAfare Services*\n\nSelect a service to view details and book seats:",
                                  chat_id, message_id, 
                                  reply_markup=markup)
            bot.answer_callback_query(call.id)
            return

        elif data.startswith("select_service:"):
            # Service Details View
            s_id = data.split(":")[1]
            services_db = sync_get_all_services()
            svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
            
            if svc:
                # Update state data with selected service ID
                state_data['selected_service_id'] = s_id
                sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
                
                fare_info = format_fare_info(svc.get('fare', {}))
                
                details_text = (
                    f"*Service Details: {sanitize_text(svc['name'])}*\n"
                    f"Route: *{sanitize_text(svc.get('route', 'N/A'))}*\n"
                    f"Seats Available: *{svc.get('remaining_seats', 'N/A')}*\n"
                    f"\n*Fare Structure:*\n{fare_info}\n\n"
                    "How many passengers are you booking for?"
                )
                
                markup = InlineKeyboardMarkup()
                # Max 5 passengers for simplicity
                for i in range(1, 6):
                    markup.add(InlineKeyboardButton(f"{i} Passenger(s)", callback_data=f"book_count:{i}"))
                markup.row(InlineKeyboardButton("⬅️ Back to Search", callback_data="cust_search"))

                bot.edit_message_text(details_text, chat_id, message_id, reply_markup=markup)
            else:
                bot.answer_callback_query(call.id, "Service not found or is unavailable.")
            return

        elif data.startswith("book_count:"):
            # Start passenger count input process
            count = int(data.split(":")[1])
            s_id = state_data.get('selected_service_id')
            
            services_db = sync_get_all_services()
            svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
            
            if not svc or svc.get('remaining_seats', 0) < count:
                bot.answer_callback_query(call.id, "Not enough seats available or service is gone.")
                # Fallback to search
                handle_query(call._replace(data="cust_search"))
                return
            
            # Initialize booking details in state data
            state_data['booking'] = {
                'service_id': s_id,
                'total_passengers': count,
                'current_passenger': 1,
                'Adult': 0,
                'Child': 0,
                'Teacher': 0
            }
            sync_set_state(chat_id, STATE_AWAIT_PASSENGER_COUNT, state_data)
            
            # Start input for the first passenger
            # Jump to the next step, which is asking for age type
            # We use an internal mechanism to avoid a new callback query
            # and move straight to the next user interaction (text input).
            
            # Update state for the next step, which is awaiting the age type for the first passenger
            sync_set_state(chat_id, STATE_AWAIT_PASSENGER_ADULT, state_data)

            # Send the first input prompt
            next_step_text = (
                f"👤 *Passenger 1 of {count}*\n\n"
                f"Enter the *age (in years)* of passenger 1. \n"
                f"_Example: 30, 10, 15_ (If a student/teacher, enter age + type). "
                f"This determines the fare."
            )
            bot.edit_message_text(next_step_text, chat_id, message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search")]]))

            bot.answer_callback_query(call.id)
            return

        elif data == "cust_bookings":
            # Placeholder for viewing customer bookings
            user_id = str(call.from_user.id)
            bookings_text = f"*🎫 My Bookings*\n\n_Note: Booking persistence is not fully implemented in this demo, as it requires a separate 'bookings' table._\n\nUser ID: `{user_id}`\n\nAny confirmed bookings would be listed here."
            
            # Check if we are editing an existing message or sending new
            try:
                bot.edit_message_text(bookings_text, chat_id, message_id, reply_markup=build_main_menu(False))
            except Exception:
                bot.send_message(chat_id, bookings_text, reply_markup=build_main_menu(False))

            bot.answer_callback_query(call.id)
            return

    # 5. Handle all other unknown queries
    bot.answer_callback_query(call.id, "Unknown command or context mismatch. Use /start.")


# --- Text Message Handlers (Stateful Logic) ---

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    """General text message handler that directs based on current state."""
    chat_id = message.chat.id
    current_state, state_data = sync_get_state(chat_id)
    
    # Check if the text matches a registered state handler
    if current_state == STATE_AWAIT_SERVICE_NAME:
        handle_service_name_input(message, state_data)
    elif current_state == STATE_AWAIT_ROUTE:
        handle_route_input(message, state_data)
    elif current_state == STATE_AWAIT_SEATS:
        handle_seats_input(message, state_data)
    elif current_state == STATE_AWAIT_ADULT_FARE:
        handle_adult_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_CHILD_FARE:
        handle_child_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_TEACHER_FARE:
        handle_teacher_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_PASSENGER_ADULT:
        handle_passenger_age_input(message, state_data)
    else:
        # Default fallback to start menu
        bot.send_message(chat_id, "I'm not sure what you mean. Please use the menu buttons or type /start to begin.")
        send_welcome(message)


# --- Provider Registration Handlers ---

@with_state
def handle_service_name_input(message, state_data):
    """Step 1: Service Name Input"""
    chat_id = message.chat.id
    name = message.text.strip()
    
    # Validation: Simple check
    if not name or len(name) > 50:
        bot.send_message(chat_id, "⚠️ Invalid name. Please enter a service name up to 50 characters long.")
        return
        
    state_data['new_service']['name'] = name
    sync_set_state(chat_id, STATE_AWAIT_ROUTE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 2/6: Route*\n\nPlease enter the route (e.g., Accra - Kumasi or Legon - Madina).", 
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_route_input(message, state_data):
    """Step 2: Route Input"""
    chat_id = message.chat.id
    route = message.text.strip()
    
    if not route or len(route) > 100:
        bot.send_message(chat_id, "⚠️ Invalid route. Please enter a route up to 100 characters long.")
        return
        
    state_data['new_service']['route'] = route
    sync_set_state(chat_id, STATE_AWAIT_SEATS, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 3/6: Seats*\n\nPlease enter the *total number of available seats* (e.g., 25).",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_seats_input(message, state_data):
    """Step 3: Total Seats Input"""
    chat_id = message.chat.id
    try:
        seats = int(message.text.strip())
        if seats <= 0 or seats > 100:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid number. Please enter a valid number of seats (1-100).")
        return

    state_data['new_service']['total_seats'] = seats
    state_data['new_service']['remaining_seats'] = seats # Initially all seats are remaining
    state_data['new_service']['fare'] = {} # Initialize fare dictionary
    sync_set_state(chat_id, STATE_AWAIT_ADULT_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 4/6: Adult Fare*\n\nPlease enter the *Adult Fare* (in GHC, e.g., 5.50).",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_adult_fare_input(message, state_data):
    """Step 4: Adult Fare Input"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 5.50).")
        return

    state_data['new_service']['fare']['Adult'] = round(fare, 2)
    sync_set_state(chat_id, STATE_AWAIT_CHILD_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 5/6: Child Fare*\n\nPlease enter the *Child Fare* (in GHC, e.g., 3.00) or enter *0* if no child discount applies.",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_child_fare_input(message, state_data):
    """Step 5: Child Fare Input"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 3.00).")
        return

    state_data['new_service']['fare']['Child'] = round(fare, 2)
    sync_set_state(chat_id, STATE_AWAIT_TEACHER_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 6/6: Teacher/Student Fare*\n\nPlease enter the *Teacher/Student Fare* (in GHC, e.g., 4.50) or enter *0* if no special discount applies.",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_teacher_fare_input(message, state_data):
    """Step 6: Teacher/Student Fare Input and Final Save"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 4.50).")
        return

    state_data['new_service']['fare']['Teacher'] = round(fare, 2)
    service_data = state_data['new_service']
    
    # 1. Generate unique ID (using timestamp + provider ID hash for good measure)
    unique_id = f"SVC_{int(time.time())}_{service_data['provider_id']}"
    service_data['id'] = unique_id
    service_data['status'] = 'active'
    
    # 2. Save to Postgres
    success = sync_save_service(service_data)

    # 3. Final confirmation message
    if success:
        fare_info = format_fare_info(service_data['fare'])
        confirmation_text = (
            f"🎉 *Service Successfully Registered!*\n\n"
            f"Name: *{sanitize_text(service_data['name'])}*\n"
            f"Route: *{sanitize_text(service_data['route'])}*\n"
            f"Total Seats: *{service_data['total_seats']}*\n"
            f"Status: *Active 🟢*\n"
            f"\n*Fare Structure:*\n{fare_info}"
        )
        
        # Clear new_service data and return to main menu state
        state_data.pop('new_service', None)
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        
        bot.send_message(chat_id, confirmation_text, reply_markup=build_main_menu(True))
    else:
        bot.send_message(chat_id, "❌ *Registration Failed*\n\nAn error occurred while saving the service. Please try again or check logs.")
        # Revert to start state
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        bot.send_message(chat_id, "Returning to main menu.", reply_markup=build_main_menu(True))


# --- Customer Booking Handlers ---

def get_fare_type(age):
    """Determines fare type based on age."""
    if age >= 18:
        return 'Adult'
    elif 5 <= age < 18:
        return 'Child' # Defaulting student/teacher to 'Teacher' via specific check
    elif 0 < age < 5:
        return 'Infant' # Typically free/not counted, but handled here
    else:
        return None # Invalid age

@with_state
def handle_passenger_age_input(message, state_data):
    """Handles passenger age input and calculates fare, loops for next passenger."""
    chat_id = message.chat.id
    text = message.text.strip().lower()
    
    booking = state_data.get('booking', {})
    total_passengers = booking.get('total_passengers', 0)
    current_passenger = booking.get('current_passenger', 1)
    
    if current_passenger > total_passengers:
        # Should not happen, but a safeguard
        bot.send_message(chat_id, "Booking process complete or error in flow. Please use /start.")
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        return
        
    fare_type = 'Adult' # Default fallback
    
    # --- Input Parsing and Validation ---
    
    # Check for Teacher/Student/Custom Input
    if 'teacher' in text or 'student' in text or 'std' in text:
        fare_type = 'Teacher'
    else:
        # Try to parse age
        age_match = re.search(r'\d+', text)
        if age_match:
            try:
                age = int(age_match.group(0))
                
                # Determine fare based on age using helper
                inferred_type = get_fare_type(age)
                if inferred_type in ['Adult', 'Child']:
                    fare_type = inferred_type
                elif inferred_type == 'Infant':
                    bot.send_message(chat_id, 
                                     f"👶 Passenger {current_passenger} is an Infant (under 5 years old) and typically travels free, but requires a seat. We'll count this as a Child/Infant booking. If you wish to specify Teacher/Student, please type 'Student' or 'Teacher'.")
                    fare_type = 'Child'
                else:
                    bot.send_message(chat_id, "⚠️ Invalid age or input. Please try again with a clear age number (e.g., 30) or type 'Student'/'Teacher'.")
                    return
            except ValueError:
                bot.send_message(chat_id, "⚠️ Could not parse a valid age. Please try again.")
                return
        else:
            bot.send_message(chat_id, "⚠️ Please enter the passenger's age (e.g., 30) or type 'Student'/'Teacher'.")
            return
            
    # --- Process Passenger and Loop ---
    
    # Increment count for the determined type
    booking[fare_type] = booking.get(fare_type, 0) + 1
    
    # Increment passenger count for the loop
    current_passenger += 1
    booking['current_passenger'] = current_passenger
    state_data['booking'] = booking
    
    # Send confirmation for the current passenger
    bot.send_message(chat_id, 
                     f"✅ Passenger {current_passenger - 1} recorded as *{fare_type}*.", 
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search")]]))

    # Check if more passengers are left
    if current_passenger <= total_passengers:
        # Loop for next passenger
        sync_set_state(chat_id, STATE_AWAIT_PASSENGER_ADULT, state_data)
        
        next_step_text = (
            f"👤 *Passenger {current_passenger} of {total_passengers}*\n\n"
            f"Enter the *age (in years)* of passenger {current_passenger}. \n"
            f"_Example: 30, 10, 15_ (If a student/teacher, enter age + type)."
        )
        bot.send_message(chat_id, next_step_text)
        
    else:
        # --- Final Booking Summary and Confirmation ---
        
        s_id = booking['service_id']
        services_db = sync_get_all_services()
        svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
        
        if not svc:
            bot.send_message(chat_id, "❌ Error: Service details lost. Please start a new search.")
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            return

        fare_data = svc.get('fare', {})
        total_fare = 0
        summary_lines = []
        
        # Calculate total fare
        for p_type, count in [('Adult', booking['Adult']), ('Child', booking['Child']), ('Teacher', booking['Teacher'])]:
            if count > 0 and p_type in fare_data:
                price = fare_data[p_type]
                subtotal = count * price
                total_fare += subtotal
                summary_lines.append(f"• {p_type} x {count}: GH¢{price:.2f} each = *GH¢{subtotal:.2f}*")

        # Confirmation message
        confirmation_text = (
            f"🎉 *Booking Summary*\n"
            f"Service: *{sanitize_text(svc['name'])}* on route *{sanitize_text(svc['route'])}*\n\n"
            f"*Passenger Breakdown:*\n"
            f"{'\n'.join(summary_lines)}\n\n"
            f"*TOTAL FARE: GH¢{total_fare:.2f}*\n\n"
            "Ready to confirm your booking and secure your seats?"
        )
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✅ Confirm & Pay (Demo)", callback_data="confirm_booking"))
        markup.row(InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search"))
        
        bot.send_message(chat_id, confirmation_text, reply_markup=markup)
        
        # Save state for confirmation step (finalizing the transaction)
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)

@bot.callback_query_handler(func=lambda call: call.data == "confirm_booking")
def handle_confirm_booking(call):
    """Handles the final confirmation of the booking."""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    state, state_data = sync_get_state(chat_id)
    booking = state_data.get('booking')

    if not booking:
        bot.answer_callback_query(call.id, "Booking data lost. Please restart.")
        handle_query(call._replace(data="cust_search"))
        return

    s_id = booking['service_id']
    total_passengers = booking['total_passengers']

    # 1. Deduct seats
    services_db = sync_get_all_services()
    svc = next((s for s in services_db['services'] if s['id'] == s_id), None)

    if svc and svc.get('remaining_seats', 0) >= total_passengers:
        new_remaining_seats = svc['remaining_seats'] - total_passengers
        
        # 2. Update database (Deduct seats)
        update_success = sync_update_service(s_id, {'remaining_seats': new_remaining_seats})
        
        if update_success:
            # 3. Final Confirmation Message
            final_text = (
                f"🌟 *Booking Confirmed!* 🌟\n\n"
                f"You have successfully booked *{total_passengers} seats* on the "
                f"*{sanitize_text(svc['name'])}* service.\n\n"
                f"New seats remaining: *{new_remaining_seats}*\n\n"
                f"_Note: In a live environment, payment would be processed and a ticket issued._"
            )
            
            # 4. Clear booking state and return to main menu
            state_data.pop('booking', None)
            state_data.pop('selected_service_id', None)
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            
            bot.edit_message_text(final_text, chat_id, message_id, reply_markup=build_main_menu(False))
            bot.answer_callback_query(call.id, "Booking successful!")
            return
        else:
            final_text = "❌ *Booking Failed*\n\nAn error occurred while securing your seats in the database. Please try again."
    else:
        final_text = "❌ *Booking Failed*\n\nIt looks like someone just booked the last few seats. Not enough seats are remaining. Please try a different service."

    # Handle failures
    state_data.pop('booking', None)
    state_data.pop('selected_service_id', None)
    sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
    bot.edit_message_text(final_text, chat_id, message_id, reply_markup=build_main_menu(False))
    bot.answer_callback_query(call.id)


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

    @app.route('/')
    def index():
        """Simple health check endpoint."""
        status = "Using Vertex AI" if vertex_predictor else "Using Local Fallback"
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

    # Set webhook on startup
    with app.app_context():
        set_initial_webhook()

    # The entry point for Render is typically `gunicorn routAfare_botFINAL:app`
    # and it uses the `app` object defined here.
    
else:
    # If not using webhooks (e.g., local polling)
    if bot:
        print("Starting bot in polling mode (local development).")
        # Polling is typically used for local development, NOT for Render/production.
        # bot.infinity_polling()
        print("Bot is configured for Webhook. To run locally, comment out webhook setup and use bot.infinity_polling().")

if __name__ == '__main__':
    # This block is for local testing with Flask's built-in server, 
    # not typically used in a Render/Gunicorn production setup.
    if BOT_TOKEN and WEBHOOK_URL_BASE:
        # In a production environment like Render, Gunicorn runs the Flask app.
        print('RoutAfare Bot is configured for Webhook deployment (Gunicorn will run app).')
    else:
        print("FATAL: BOT_TOKEN or WEBHOOK_URL_BASE not set. Cannot run bot.")'''



#NEW CODE FOR PRE AL

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

# Third-party libraries
try:
    from dotenv import load_dotenv
    from flask import Flask, request
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    
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
# Guard webhook path to prevent Flask routing collisions when BOT_TOKEN is empty/missing
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}" if BOT_TOKEN else "/webhook-fallback"
# DATABASE_URL is automatically set by Render
DATABASE_URL = os.getenv('DATABASE_URL')
# Max connection retries for PostgreSQL
MAX_DB_RETRIES = 5
RETRY_DELAY = 5 # seconds

# Vertex AI (Optional - Dead code preserved for compatibility/future work)
GCP_PROJECT = os.getenv('GCP_PROJECT')
GCP_LOCATION = os.getenv('GCP_LOCATION')
VERTEX_MODEL = os.getenv('VERTEX_MODEL')
SERVICE_ACCOUNT_JSON = os.getenv('SERVICE_ACCOUNT_JSON')

# --- Global State and Initialization ---
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='Markdown') if BOT_TOKEN else None
vertex_predictor = None

if GCP_LIBRARIES_AVAILABLE and GCP_PROJECT and GCP_LOCATION and VERTEX_MODEL and SERVICE_ACCOUNT_JSON:
    try:
        # Initialize Vertex AI client using service account key
        credentials = service_account.Credentials.from_service_account_info(json.loads(SERVICE_ACCOUNT_JSON))
        aiplatform.init(project=GCP_PROJECT, location=GCP_LOCATION, credentials=credentials)
        
        # Load the custom model endpoint
        vertex_predictor = aiplatform.Endpoint(VERTEX_MODEL)
        print("✅ Vertex AI Predictor Initialized.")
    except Exception as e:
        print(f"❌ Vertex AI Initialization Failed: {e}")
        vertex_predictor = None
else:
    print("⚠️ Vertex AI environment variables missing or libraries unavailable. Using local fallbacks.")


# --- PostgreSQL Connection Management ---

def get_db_connection():
    """
    Establishes a PostgreSQL connection. Retries on failure.
    Returns: A psycopg2 connection object or None.
    """
    if not DATABASE_URL:
        print("FATAL: DATABASE_URL is not set.")
        return None
    
    for i in range(MAX_DB_RETRIES):
        try:
            # Attempt to connect using the URL
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = True
            print("✅ PostgreSQL Client Connected.")
            return conn
        except Exception as e:
            print(f"❌ Error connecting to PostgreSQL (Attempt {i+1}/{MAX_DB_RETRIES}): {e}")
            if i < MAX_DB_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                print("FATAL: Failed to connect to PostgreSQL after multiple retries.")
                return None
    return None

def sync_execute_db_operation(sql_query, params=None, fetch=False):
    """Executes a database operation (sync)."""
    conn = get_db_connection()
    if conn is None:
        return None if fetch else False

    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(sql_query, params)
            if fetch:
                return cur.fetchall()
            return True
    except Exception as e:
        print(f"Database operation failed: {e}")
        return None if fetch else False
    finally:
        if conn:
            conn.close()

def sync_create_tables():
    """Creates necessary PostgreSQL tables if they don't exist."""
    create_bot_state_table = """
    CREATE TABLE IF NOT EXISTS bot_state (
        chat_id TEXT PRIMARY KEY, 
        state_key TEXT NOT NULL,
        data JSONB,
        last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """

    create_services_table = """
    CREATE TABLE IF NOT EXISTS services (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        name TEXT NOT NULL,
        route TEXT,
        fare JSONB,
        total_seats INTEGER DEFAULT 0,
        remaining_seats INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    if sync_execute_db_operation(create_bot_state_table) and \
       sync_execute_db_operation(create_services_table):
        print("✅ PostgreSQL tables ensured (bot_state, services).")
        return True
    else:
        print("❌ Failed to create PostgreSQL tables.")
        return False

# Ensure tables exist on startup
if DATABASE_URL:
    sync_create_tables()


# --- Database CRUD Operations ---

# State functions
def sync_set_state(chat_id, state_key, data=None):
    """Sets the state and data for a given chat_id."""
    sql = """
    INSERT INTO bot_state (chat_id, state_key, data) 
    VALUES (%s, %s, %s)
    ON CONFLICT (chat_id) 
    DO UPDATE SET state_key = EXCLUDED.state_key, 
                  data = EXCLUDED.data,
                  last_updated = CURRENT_TIMESTAMP;
    """
    return sync_execute_db_operation(sql, (str(chat_id), state_key, Json(data or {})))

def sync_get_state(chat_id):
    """Retrieves the state and data for a given chat_id."""
    sql = "SELECT state_key, data FROM bot_state WHERE chat_id = %s;"
    result = sync_execute_db_operation(sql, (str(chat_id),), fetch=True)
    if result:
        return result[0]['state_key'], result[0]['data']
    return None, {} # Default state and empty data

# Service functions
def sync_save_service(service_data):
    """Saves a new service to the services table."""
    sql = """
    INSERT INTO services (id, provider_id, name, route, fare, total_seats, remaining_seats, status) 
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) 
    DO UPDATE SET provider_id = EXCLUDED.provider_id,
                  name = EXCLUDED.name,
                  route = EXCLUDED.route,
                  fare = EXCLUDED.fare,
                  total_seats = EXCLUDED.total_seats,
                  remaining_seats = EXCLUDED.remaining_seats,
                  status = EXCLUDED.status,
                  last_updated = CURRENT_TIMESTAMP;
    """
    return sync_execute_db_operation(sql, (
        service_data['id'], 
        service_data['provider_id'], 
        service_data['name'], 
        service_data['route'], 
        Json(service_data.get('fare', {})),
        service_data.get('total_seats'),
        service_data.get('remaining_seats'),
        service_data.get('status') or 'active'
    ))

def sync_get_all_services(provider_id=None):
    """Retrieves all services, optionally filtered by provider_id."""
    if provider_id:
        sql = "SELECT * FROM services WHERE provider_id = %s ORDER BY id;"
        results = sync_execute_db_operation(sql, (provider_id,), fetch=True)
    else:
        sql = "SELECT * FROM services ORDER BY id;"
        results = sync_execute_db_operation(sql, fetch=True)
        
    services_list = [dict(row) for row in results] if results else []
    return {"services": services_list}

def sync_update_service(service_id, updates):
    """Updates specific fields of a service."""
    set_clauses = []
    params = []
    
    for key, value in updates.items():
        if key == 'fare':
            set_clauses.append(f"{key} = %s")
            params.append(Json(value))
        elif key in ['total_seats', 'remaining_seats', 'name', 'route', 'status']:
            set_clauses.append(f"{key} = %s")
            params.append(value)
            
    if not set_clauses:
        return False

    set_clause_str = ", ".join(set_clauses)
    params.append(service_id)
    
    sql = f"UPDATE services SET {set_clause_str}, last_updated = CURRENT_TIMESTAMP WHERE id = %s;"
    return sync_execute_db_operation(sql, tuple(params))


# --- Bot State Keys ---
STATE_START = 'start'
STATE_AWAIT_CHOICE = 'await_choice'
STATE_AWAIT_SERVICE_NAME = 'await_service_name'
STATE_AWAIT_ROUTE = 'await_route'
STATE_AWAIT_SEATS = 'await_seats'
STATE_AWAIT_ADULT_FARE = 'await_adult_fare'
STATE_AWAIT_CHILD_FARE = 'await_child_fare'
STATE_AWAIT_TEACHER_FARE = 'await_teacher_fare'
STATE_AWAIT_PASSENGER_COUNT = 'await_passenger_count'
STATE_AWAIT_PASSENGER_ADULT = 'await_passenger_adult'
STATE_AWAIT_PASSENGER_CHILD = 'await_passenger_child'
STATE_AWAIT_PASSENGER_TEACHER = 'await_passenger_teacher'


# --- Core Markup Builders ---

def build_main_menu(is_provider):
    """Builds the main start menu."""
    markup = InlineKeyboardMarkup()
    
    if is_provider:
        # Provider Menu
        markup.row(InlineKeyboardButton("➕ Register New Service", callback_data="prov_register"))
        markup.row(InlineKeyboardButton("🚦 View/Toggle Status", callback_data="prov_status"))
        markup.row(InlineKeyboardButton("💰 View Revenue", callback_data="prov_revenue"))
    else:
        # Customer Menu
        markup.row(InlineKeyboardButton("🚌 Search Services", callback_data="cust_search"))
        markup.row(InlineKeyboardButton("🎫 View My Bookings", callback_data="cust_bookings"))
    
    markup.row(InlineKeyboardButton("🔄 Change Role", callback_data="change_role"))
    markup.row(InlineKeyboardButton("🚪 Exit Program", callback_data="exit"))
    return markup

def build_service_list_markup(services, callback_prefix):
    """Builds a list of services as inline buttons."""
    markup = InlineKeyboardMarkup()
    if not services:
        markup.add(InlineKeyboardButton("No services found.", callback_data="ignore"))
    else:
        for svc in services:
            status_emoji = '🟢' if svc.get('status', 'active') == 'active' else '🔴'
            button_text = f"{status_emoji} {svc['name']} ({svc['route'] or 'N/A'})"
            callback_data = f"{callback_prefix}:{svc['id']}"
            markup.row(InlineKeyboardButton(button_text, callback_data=callback_data))
    
    markup.row(InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start"))
    return markup


# --- Text Sanitization ---

def sanitize_text(text):
    """Removes non-standard characters that can break Telegram Markdown."""
    if text is None:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-={}.!])', r'\\\1', text)


# --- State Management Decorator & Mapping ---

# Map of handler names to their corresponding database state keys to resolve the decorator mismatch.
FUNCTION_STATE_MAP = {
    'handle_service_name_input': STATE_AWAIT_SERVICE_NAME,
    'handle_route_input': STATE_AWAIT_ROUTE,
    'handle_seats_input': STATE_AWAIT_SEATS,
    'handle_adult_fare_input': STATE_AWAIT_ADULT_FARE,
    'handle_child_fare_input': STATE_AWAIT_CHILD_FARE,
    'handle_teacher_fare_input': STATE_AWAIT_TEACHER_FARE,
    'handle_passenger_age_input': STATE_AWAIT_PASSENGER_ADULT,
}

def with_state(handler):
    """Decorator to load, verify, and pass chat state and data to the handler."""
    def wrapper(message, state_data=None, *args, **kwargs):
        chat_id = message.chat.id
        current_state, db_state_data = sync_get_state(chat_id)
        
        # Optimize: reuse passed state_data if present, otherwise fetch from database
        active_data = state_data if state_data is not None else db_state_data
        
        if isinstance(message, telebot.types.Message):
            # Check the function state map to ensure correct state pairing
            expected_state = FUNCTION_STATE_MAP.get(handler.__name__)
            if current_state == expected_state:
                return handler(message, active_data)
        
    return wrapper


# --- Helper Functions ---

def format_fare_info(fare_data):
    """Formats fare data for display."""
    lines = []
    if 'Adult' in fare_data:
        lines.append(f"• Adult: *GH¢{fare_data['Adult']:.2f}*")
    if 'Child' in fare_data:
        lines.append(f"• Child: *GH¢{fare_data['Child']:.2f}*")
    if 'Teacher' in fare_data:
        lines.append(f"• Teacher/Student: *GH¢{fare_data['Teacher']:.2f}*")
    return "\n".join(lines)


# --- Start/Role Selection ---

@bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    """Handles /start command and sends the initial role selection."""
    chat_id = message.chat.id
    
    state, data = sync_get_state(chat_id)
    role = data.get('role')

    if role in ['provider', 'customer']:
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, data)
        text = f"Welcome back! You are currently signed in as a *{role.capitalize()}*.\n\nWhat would you like to do?"
        bot.send_message(chat_id, text, reply_markup=build_main_menu(role == 'provider'))
    else:
        sync_set_state(chat_id, STATE_START)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🚌 I am a Customer", callback_data="select_role:customer"))
        markup.row(InlineKeyboardButton("🛠️ I am a Service Provider", callback_data="select_role:provider"))
        
        bot.send_message(chat_id, 
                         "*Welcome to RoutAfare!* \n\nPlease select your role to proceed.", 
                         reply_markup=markup)


# --- Callback Query Handler ---

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    """Handles all incoming inline keyboard button presses."""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data
    
    # 1. Handle common/role selection queries first
    if data.startswith("select_role:"):
        role = data.split(":")[1]
        state, state_data = sync_get_state(chat_id)
        state_data['role'] = role
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        
        bot.edit_message_text(f"Role set to *{role.capitalize()}*.\n\nWhat would you like to do?", 
                              chat_id, message_id, 
                              reply_markup=build_main_menu(role == 'provider'))
        bot.answer_callback_query(call.id, f"Role switched to {role.capitalize()}")
        return

    # 2. Handle main menu and navigation queries
    elif data == "start":
        send_welcome(call.message)
        bot.answer_callback_query(call.id)
        return

    elif data == "change_role":
        state, state_data = sync_get_state(chat_id)
        state_data.pop('role', None)
        sync_set_state(chat_id, STATE_START, state_data)
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🚌 I am a Customer", callback_data="select_role:customer"))
        markup.row(InlineKeyboardButton("🛠️ I am a Service Provider", callback_data="select_role:provider"))
        
        bot.edit_message_text("*Please select your new role to proceed.*", 
                              chat_id, message_id, 
                              reply_markup=markup)
        bot.answer_callback_query(call.id, "Changing role...")
        return
        
    elif data == "exit":
        sync_set_state(chat_id, STATE_START, {})
        bot.edit_message_text("Thank you for using RoutAfare! Type /start to begin again.", 
                              chat_id, message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Program exited.")
        return
        
    elif data == "ignore":
        bot.answer_callback_query(call.id, "Nothing to do here.")
        return

    # 3. Handle Provider specific queries
    state, state_data = sync_get_state(chat_id)
    role = state_data.get('role')
    
    if role == 'provider':
        if data == "prov_register":
            state_data['new_service'] = {'provider_id': str(call.from_user.id)}
            sync_set_state(chat_id, STATE_AWAIT_SERVICE_NAME, state_data)
            
            bot.edit_message_text("📝 *Service Registration - Step 1/6: Name*\n\nPlease enter the unique name for your new service (e.g., Accra Express, Daily Commute 01).", 
                                  chat_id, message_id, 
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))
            bot.answer_callback_query(call.id)
            return

        elif data == "prov_status":
            provider_id = str(call.from_user.id)
            services_db = sync_get_all_services(provider_id=provider_id)
            services = services_db['services']
            
            markup = build_service_list_markup(services, "toggle_status")
            status_text = "*🚦 Service Status Management*\n\nTap a service below to instantly toggle its availability (Active 🟢 / Unavailable 🔴)."
            
            try:
                bot.edit_message_text(status_text, chat_id, message_id, reply_markup=markup)
            except Exception:
                bot.send_message(chat_id, status_text, reply_markup=markup)
                
            bot.answer_callback_query(call.id)
            return
            
        elif data.startswith("toggle_status:"):
            s_id = data.split(":")[1]
            services_db = sync_get_all_services()
            svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
            
            if svc:
                new_status = 'unavailable' if svc.get('status') == 'active' else 'active'
                sync_update_service(s_id, {'status': new_status})
                
                # Re-routing bug fix: Corrected ._replace attribute-error crash
                call.data = "prov_status"
                handle_query(call)
            else:
                bot.answer_callback_query(call.id, "Service not found.")
            return
            
        elif data == "prov_revenue":
            provider_id = str(call.from_user.id)
            services_db = sync_get_all_services(provider_id=provider_id)
            total_potential_revenue = 0
            
            for svc in services_db['services']:
                adult_fare = svc.get('fare', {}).get('Adult', 0)
                seats_used = svc.get('total_seats', 0) - svc.get('remaining_seats', 0)
                total_potential_revenue += seats_used * adult_fare
                
            revenue_text = f"*💰 Your Revenue Report (Simplified)*\n\nTotal Potential Revenue (Based on seats used at Adult Fare): *GH¢{total_potential_revenue:.2f}*\n\n_Note: This is a placeholder. A real system requires booking logs._"

            try:
                bot.edit_message_text(revenue_text, chat_id, message_id, reply_markup=build_main_menu(True))
            except Exception:
                bot.send_message(chat_id, revenue_text, reply_markup=build_main_menu(True))
            
            bot.answer_callback_query(call.id)
            return

    # 4. Handle Customer specific queries
    elif role == 'customer':
        if data == "cust_search":
            services_db = sync_get_all_services()
            available_services = [s for s in services_db['services'] if s.get('status', 'active') == 'active']
            
            if not available_services:
                bot.edit_message_text("⚠️ No services are currently active. Please check back later.",
                                      chat_id, message_id, 
                                      reply_markup=build_main_menu(False))
                bot.answer_callback_query(call.id, "No active services.")
                return
                
            state_data['available_services'] = [s['id'] for s in available_services]
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            
            markup = build_service_list_markup(available_services, "select_service")
            
            bot.edit_message_text("*🚌 Available RoutAfare Services*\n\nSelect a service to view details and book seats:",
                                  chat_id, message_id, 
                                  reply_markup=markup)
            bot.answer_callback_query(call.id)
            return

        elif data.startswith("select_service:"):
            s_id = data.split(":")[1]
            services_db = sync_get_all_services()
            svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
            
            if svc:
                state_data['selected_service_id'] = s_id
                sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
                
                fare_info = format_fare_info(svc.get('fare', {}))
                
                details_text = (
                    f"*Service Details: {sanitize_text(svc['name'])}*\n"
                    f"Route: *{sanitize_text(svc.get('route', 'N/A'))}*\n"
                    f"Seats Available: *{svc.get('remaining_seats', 'N/A')}*\n"
                    f"\n*Fare Structure:*\n{fare_info}\n\n"
                    "How many passengers are you booking for?"
                )
                
                markup = InlineKeyboardMarkup()
                for i in range(1, 6):
                    markup.add(InlineKeyboardButton(f"{i} Passenger(s)", callback_data=f"book_count:{i}"))
                markup.row(InlineKeyboardButton("⬅️ Back to Search", callback_data="cust_search"))

                bot.edit_message_text(details_text, chat_id, message_id, reply_markup=markup)
            else:
                bot.answer_callback_query(call.id, "Service not found or is unavailable.")
            return

        elif data.startswith("book_count:"):
            count = int(data.split(":")[1])
            s_id = state_data.get('selected_service_id')
            
            services_db = sync_get_all_services()
            svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
            
            if not svc or svc.get('remaining_seats', 0) < count:
                bot.answer_callback_query(call.id, "Not enough seats available or service is gone.")
                
                # Re-routing bug fix: Corrected ._replace attribute-error crash
                call.data = "cust_search"
                handle_query(call)
                return
            
            state_data['booking'] = {
                'service_id': s_id,
                'total_passengers': count,
                'current_passenger': 1,
                'Adult': 0,
                'Child': 0,
                'Teacher': 0
            }
            sync_set_state(chat_id, STATE_AWAIT_PASSENGER_COUNT, state_data)
            sync_set_state(chat_id, STATE_AWAIT_PASSENGER_ADULT, state_data)

            next_step_text = (
                f"👤 *Passenger 1 of {count}*\n\n"
                f"Enter the *age (in years)* of passenger 1. \n"
                f"_Example: 30, 10, 15_ (If a student/teacher, enter age + type). "
                f"This determines the fare."
            )
            bot.edit_message_text(next_step_text, chat_id, message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search")]]))

            bot.answer_callback_query(call.id)
            return

        elif data == "cust_bookings":
            user_id = str(call.from_user.id)
            bookings_text = f"*🎫 My Bookings*\n\n_Note: Booking persistence is not fully implemented in this demo, as it requires a separate 'bookings' table._\n\nUser ID: `{user_id}`\n\nAny confirmed bookings would be listed here."
            
            try:
                bot.edit_message_text(bookings_text, chat_id, message_id, reply_markup=build_main_menu(False))
            except Exception:
                bot.send_message(chat_id, bookings_text, reply_markup=build_main_menu(False))

            bot.answer_callback_query(call.id)
            return

    bot.answer_callback_query(call.id, "Unknown command or context mismatch. Use /start.")


# --- Text Message Handlers (Stateful Logic) ---

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    """General text message handler that directs based on current state."""
    chat_id = message.chat.id
    current_state, state_data = sync_get_state(chat_id)
    
    if current_state == STATE_AWAIT_SERVICE_NAME:
        handle_service_name_input(message, state_data)
    elif current_state == STATE_AWAIT_ROUTE:
        handle_route_input(message, state_data)
    elif current_state == STATE_AWAIT_SEATS:
        handle_seats_input(message, state_data)
    elif current_state == STATE_AWAIT_ADULT_FARE:
        handle_adult_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_CHILD_FARE:
        handle_child_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_TEACHER_FARE:
        handle_teacher_fare_input(message, state_data)
    elif current_state == STATE_AWAIT_PASSENGER_ADULT:
        handle_passenger_age_input(message, state_data)
    else:
        bot.send_message(chat_id, "I'm not sure what you mean. Please use the menu buttons or type /start to begin.")
        send_welcome(message)


# --- Provider Registration Handlers ---

@with_state
def handle_service_name_input(message, state_data):
    """Step 1: Service Name Input"""
    chat_id = message.chat.id
    name = message.text.strip()
    
    if not name or len(name) > 50:
        bot.send_message(chat_id, "⚠️ Invalid name. Please enter a service name up to 50 characters long.")
        return
        
    state_data['new_service']['name'] = name
    sync_set_state(chat_id, STATE_AWAIT_ROUTE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 2/6: Route*\n\nPlease enter the route (e.g., Accra - Kumasi or Legon - Madina).", 
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_route_input(message, state_data):
    """Step 2: Route Input"""
    chat_id = message.chat.id
    route = message.text.strip()
    
    if not route or len(route) > 100:
        bot.send_message(chat_id, "⚠️ Invalid route. Please enter a route up to 100 characters long.")
        return
        
    state_data['new_service']['route'] = route
    sync_set_state(chat_id, STATE_AWAIT_SEATS, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 3/6: Seats*\n\nPlease enter the *total number of available seats* (e.g., 25).",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_seats_input(message, state_data):
    """Step 3: Total Seats Input"""
    chat_id = message.chat.id
    try:
        seats = int(message.text.strip())
        if seats <= 0 or seats > 100:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid number. Please enter a valid number of seats (1-100).")
        return

    state_data['new_service']['total_seats'] = seats
    state_data['new_service']['remaining_seats'] = seats
    state_data['new_service']['fare'] = {}
    sync_set_state(chat_id, STATE_AWAIT_ADULT_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 4/6: Adult Fare*\n\nPlease enter the *Adult Fare* (in GHC, e.g., 5.50).",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_adult_fare_input(message, state_data):
    """Step 4: Adult Fare Input"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 5.50).")
        return

    state_data['new_service']['fare']['Adult'] = round(fare, 2)
    sync_set_state(chat_id, STATE_AWAIT_CHILD_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 5/6: Child Fare*\n\nPlease enter the *Child Fare* (in GHC, e.g., 3.00) or enter *0* if no child discount applies.",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_child_fare_input(message, state_data):
    """Step 5: Child Fare Input"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 3.00).")
        return

    state_data['new_service']['fare']['Child'] = round(fare, 2)
    sync_set_state(chat_id, STATE_AWAIT_TEACHER_FARE, state_data)
    
    bot.send_message(chat_id, 
                     "📝 *Service Registration - Step 6/6: Teacher/Student Fare*\n\nPlease enter the *Teacher/Student Fare* (in GHC, e.g., 4.50) or enter *0* if no special discount applies.",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="start")]]))

@with_state
def handle_teacher_fare_input(message, state_data):
    """Step 6: Teacher/Student Fare Input and Final Save"""
    chat_id = message.chat.id
    try:
        fare = float(message.text.strip())
        if fare < 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ Invalid amount. Please enter a valid non-negative number for the fare (e.g., 4.50).")
        return

    state_data['new_service']['fare']['Teacher'] = round(fare, 2)
    service_data = state_data['new_service']
    
    unique_id = f"SVC_{int(time.time())}_{service_data['provider_id']}"
    service_data['id'] = unique_id
    service_data['status'] = 'active'
    
    success = sync_save_service(service_data)

    if success:
        fare_info = format_fare_info(service_data['fare'])
        confirmation_text = (
            f"🎉 *Service Successfully Registered!*\n\n"
            f"Name: *{sanitize_text(service_data['name'])}*\n"
            f"Route: *{sanitize_text(service_data['route'])}*\n"
            f"Total Seats: *{service_data['total_seats']}*\n"
            f"Status: *Active 🟢*\n"
            f"\n*Fare Structure:*\n{fare_info}"
        )
        
        state_data.pop('new_service', None)
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        
        bot.send_message(chat_id, confirmation_text, reply_markup=build_main_menu(True))
    else:
        bot.send_message(chat_id, "❌ *Registration Failed*\n\nAn error occurred while saving the service. Please try again or check logs.")
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        bot.send_message(chat_id, "Returning to main menu.", reply_markup=build_main_menu(True))


# --- Customer Booking Handlers ---

def get_fare_type(age):
    """Determines fare type based on age."""
    if age >= 18:
        return 'Adult'
    elif 5 <= age < 18:
        return 'Child'
    elif 0 < age < 5:
        return 'Infant'
    else:
        return None

@with_state
def handle_passenger_age_input(message, state_data):
    """Handles passenger age input and calculates fare, loops for next passenger."""
    chat_id = message.chat.id
    text = message.text.strip().lower()
    
    booking = state_data.get('booking', {})
    total_passengers = booking.get('total_passengers', 0)
    current_passenger = booking.get('current_passenger', 1)
    
    if current_passenger > total_passengers:
        bot.send_message(chat_id, "Booking process complete or error in flow. Please use /start.")
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
        return
        
    fare_type = 'Adult'
    
    # Check for Teacher/Student/Custom Input
    if 'teacher' in text or 'student' in text or 'std' in text:
        fare_type = 'Teacher'
    else:
        age_match = re.search(r'\d+', text)
        if age_match:
            try:
                age = int(age_match.group(0))
                inferred_type = get_fare_type(age)
                if inferred_type in ['Adult', 'Child']:
                    fare_type = inferred_type
                elif inferred_type == 'Infant':
                    bot.send_message(chat_id, 
                                     f"👶 Passenger {current_passenger} is an Infant (under 5 years old) and typically travels free, but requires a seat. We'll count this as a Child/Infant booking. If you wish to specify Teacher/Student, please type 'Student' or 'Teacher'.")
                    fare_type = 'Child'
                else:
                    bot.send_message(chat_id, "⚠️ Invalid age or input. Please try again with a clear age number (e.g., 30) or type 'Student'/'Teacher'.")
                    return
            except ValueError:
                bot.send_message(chat_id, "⚠️ Could not parse a valid age. Please try again.")
                return
        else:
            bot.send_message(chat_id, "⚠️ Please enter the passenger's age (e.g., 30) or type 'Student'/'Teacher'.")
            return
            
    booking[fare_type] = booking.get(fare_type, 0) + 1
    current_passenger += 1
    booking['current_passenger'] = current_passenger
    state_data['booking'] = booking
    
    bot.send_message(chat_id, 
                     f"✅ Passenger {current_passenger - 1} recorded as *{fare_type}*.", 
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search")]]))

    if current_passenger <= total_passengers:
        sync_set_state(chat_id, STATE_AWAIT_PASSENGER_ADULT, state_data)
        
        next_step_text = (
            f"👤 *Passenger {current_passenger} of {total_passengers}*\n\n"
            f"Enter the *age (in years)* of passenger {current_passenger}. \n"
            f"_Example: 30, 10, 15_ (If a student/teacher, enter age + type)."
        )
        bot.send_message(chat_id, next_step_text)
        
    else:
        s_id = booking['service_id']
        services_db = sync_get_all_services()
        svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
        
        if not svc:
            bot.send_message(chat_id, "❌ Error: Service details lost. Please start a new search.")
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            return

        fare_data = svc.get('fare', {})
        total_fare = 0
        summary_lines = []
        
        for p_type, count in [('Adult', booking['Adult']), ('Child', booking['Child']), ('Teacher', booking['Teacher'])]:
            if count > 0 and p_type in fare_data:
                price = fare_data[p_type]
                subtotal = count * price
                total_fare += subtotal
                summary_lines.append(f"• {p_type} x {count}: GH¢{price:.2f} each = *GH¢{subtotal:.2f}*")

        confirmation_text = (
            f"🎉 *Booking Summary*\n"
            f"Service: *{sanitize_text(svc['name'])}* on route *{sanitize_text(svc['route'])}*\n\n"
            f"*Passenger Breakdown:*\n"
            f"{'\n'.join(summary_lines)}\n\n"
            f"*TOTAL FARE: GH¢{total_fare:.2f}*\n\n"
            "Ready to confirm your booking and secure your seats?"
        )
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✅ Confirm & Pay (Demo)", callback_data="confirm_booking"))
        markup.row(InlineKeyboardButton("⬅️ Cancel Booking", callback_data="cust_search"))
        
        bot.send_message(chat_id, confirmation_text, reply_markup=markup)
        sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_booking")
def handle_confirm_booking(call):
    """Handles the final confirmation of the booking."""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    state, state_data = sync_get_state(chat_id)
    booking = state_data.get('booking')

    if not booking:
        bot.answer_callback_query(call.id, "Booking data lost. Please restart.")
        
        # Re-routing bug fix: Corrected ._replace attribute-error crash
        call.data = "cust_search"
        handle_query(call)
        return

    s_id = booking['service_id']
    total_passengers = booking['total_passengers']

    services_db = sync_get_all_services()
    svc = next((s for s in services_db['services'] if s['id'] == s_id), None)

    if svc and svc.get('remaining_seats', 0) >= total_passengers:
        new_remaining_seats = svc['remaining_seats'] - total_passengers
        update_success = sync_update_service(s_id, {'remaining_seats': new_remaining_seats})
        
        if update_success:
            final_text = (
                f"🌟 *Booking Confirmed!* 🌟\n\n"
                f"You have successfully booked *{total_passengers} seats* on the "
                f"*{sanitize_text(svc['name'])}* service.\n\n"
                f"New seats remaining: *{new_remaining_seats}*\n\n"
                f"_Note: In a live environment, payment would be processed and a ticket issued._"
            )
            
            state_data.pop('booking', None)
            state_data.pop('selected_service_id', None)
            sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
            
            bot.edit_message_text(final_text, chat_id, message_id, reply_markup=build_main_menu(False))
            bot.answer_callback_query(call.id, "Booking successful!")
            return
        else:
            final_text = "❌ *Booking Failed*\n\nAn error occurred while securing your seats in the database. Please try again."
    else:
        final_text = "❌ *Booking Failed*\n\nIt looks like someone just booked the last few seats. Not enough seats are remaining. Please try a different service."

    state_data.pop('booking', None)
    state_data.pop('selected_service_id', None)
    sync_set_state(chat_id, STATE_AWAIT_CHOICE, state_data)
    bot.edit_message_text(final_text, chat_id, message_id, reply_markup=build_main_menu(False))
    bot.answer_callback_query(call.id)


# --- FLASK/WEBHOOK SETUP ---
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

    @app.route('/')
    def index():
        """Simple health check endpoint."""
        status = "Using Vertex AI" if vertex_predictor else "Using Local Fallback"
        return f'RoutAfare Bot FINAL Running. Status: {status}', 200

    def set_initial_webhook():
        """Configures the Telegram webhook URL."""
        if bot is None: 
            print("FATAL: BOT_TOKEN not set. Cannot configure webhook.")
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

    with app.app_context():
        set_initial_webhook()


if __name__ == '__main__':
    # Flask built-in server or local polling fallback
    if BOT_TOKEN and WEBHOOK_URL_BASE:
        print('RoutAfare Bot is configured for Webhook deployment (Flask / Gunicorn).')
        app.run(host='0.0.0.0', port=3000)
    elif bot:
        print("⚡ Starting bot in POLLING mode (local development fallback)...")
        # Ensure we clear webhook before polling to avoid conflict errors
        bot.remove_webhook()
        time.sleep(0.1)
        bot.infinity_polling()
    else:
        print("FATAL: BOT_TOKEN is not set. Cannot run bot.")

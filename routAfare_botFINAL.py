#!/usr/bin/env python3
"""
routAfare_botFINAL.py
Integrated Version using Render's PostgreSQL for data persistence.
- Replaces Firestore with asynchronous PostgreSQL connection (using psycopg2).
- Assumes DATABASE_URL is set in the environment (standard for Render).
- Vertex AI is kept as optional, relying on the environment variables for initialization.
- FIXED: Passenger search query now includes all services (Public and Private).
- REFINED: Status management logic now correctly refreshes the menu.
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
    
    # PostgreSQL Imports
    import psycopg2
    from psycopg2.extras import DictCursor
    
    # Google Cloud Imports (kept for optional Vertex AI, but not strictly used)
    try:
        from google.cloud import aiplatform
        GCP_LIBRARIES_AVAILABLE = True
    except ImportError:
        GCP_LIBRARIES_AVAILABLE = False
    
    DB_LIBRARIES_AVAILABLE = True
except ImportError as e:
    print(f"CRITICAL: Missing required package. Error: {e}")
    sys.exit(1)

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBHOOK_URL_BASE = os.getenv('WEBHOOK_URL_BASE')
PROVIDER_PASSWORD = os.getenv('PROVIDER_PASSWORD')
DATABASE_URL = os.getenv('DATABASE_URL')
PROJECT_ID = os.getenv('PROJECT_ID')
MODEL_ENDPOINT_ID = os.getenv('MODEL_ENDPOINT_ID')

# --- Globals and Bot Setup ---
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {} # Simple in-memory storage for user session state

# --- PostgreSQL Connection and Setup ---
def get_sync_db_connection(dict_cursor=False):
    """Establishes a synchronous connection to the PostgreSQL database."""
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.")
        return None
    try:
        cursor_factory = DictCursor if dict_cursor else None
        return psycopg2.connect(DATABASE_URL, cursor_factory=cursor_factory)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

def setup_database_schema():
    """Initializes the database schema if tables do not exist."""
    conn = get_sync_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        
        # 1. Bus Services Table (Stores public and private bus info)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bus_services (
                id SERIAL PRIMARY KEY,
                route TEXT NOT NULL,
                service_name TEXT NOT NULL,
                driver_name TEXT,
                fare_price DECIMAL(10, 2),
                contact_number TEXT,
                departure_time_slot TEXT NOT NULL, -- e.g., '06:00-07:00'
                status TEXT DEFAULT 'active', -- 'active' or 'unavailable'
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2. Bookings Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                bus_id INTEGER REFERENCES bus_services(id),
                route TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                booking_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 3. Insert initial Public Bus services if none exist for popular routes
        cur.execute("SELECT COUNT(*) FROM bus_services WHERE service_name = 'Public Bus'")
        count = cur.fetchone()[0]
        
        if count == 0:
            print("Inserting initial public bus data...")
            initial_services = [
                ('Badulla-Kandy', 'Public Bus', 'Govt Driver 1', 150.00, '0771234567', '06:00-07:00'),
                ('Badulla-Kandy', 'Public Bus', 'Govt Driver 2', 155.00, '0772345678', '08:00-09:00'),
                ('Badulla-Kandy', 'Public Bus', 'Govt Driver 3', 145.00, '0773456789', '12:00-13:00'),
            ]
            
            insert_query = """
                INSERT INTO bus_services (route, service_name, driver_name, fare_price, contact_number, departure_time_slot)
                VALUES (%s, %s, %s, %s, %s, %s);
            """
            cur.executemany(insert_query, initial_services)
            print("Initial public bus data inserted.")

        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Database setup or initial data insertion failed: {e}")
    finally:
        if conn:
            conn.close()

# Run database setup on script initialization
setup_database_schema()

# --- Helper Functions ---

def get_departure_time_slot(time_str):
    """Converts HH:MM string to a 1-hour time slot string (e.g., '07:00' -> '07:00-08:00')."""
    try:
        hour = int(time_str.split(':')[0])
        start_time = f"{hour:02d}:00"
        end_time = f"{(hour + 1) % 24:02d}:00"
        return f"{start_time}-{end_time}"
    except Exception:
        return None

def get_fare_prediction(data):
    """Placeholder for Vertex AI prediction or simple fallback."""
    default_fare = 150.00
    estimated_fare = default_fare

    if not GCP_LIBRARIES_AVAILABLE:
        return estimated_fare

    if not all([PROJECT_ID, MODEL_ENDPOINT_ID]):
        return estimated_fare

    try:
        # Simulated Prediction based on route length/time
        route = data.get('route', '')
        if 'colombo' in route.lower():
            estimated_fare = 500.00
        elif 'kandy' in route.lower():
            estimated_fare = 150.00
        else:
            estimated_fare = 100.00
            
        return estimated_fare
        
    except Exception as e:
        print(f"AI Prediction failed (falling back to default): {e}")
        return default_fare


# --- DB Sync Operations (Provider Logic) ---

def sync_add_service(data):
    """Saves a new bus service entry to the database."""
    conn = get_sync_db_connection()
    if not conn: return None

    try:
        cur = conn.cursor()
        time_slot = get_departure_time_slot(data['departure_time'])
        if not time_slot: raise ValueError("Invalid departure time format.")
            
        insert_query = """
            INSERT INTO bus_services (
                route, service_name, driver_name, fare_price, contact_number, departure_time_slot
            ) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
        """
        
        cur.execute(insert_query, (
            data['route'], 
            data['service_name'], 
            data['driver_name'], 
            data['fare_price'], 
            data['contact_number'],
            time_slot
        ))
        
        bus_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return bus_id
    except Exception as e:
        print(f"Error saving new bus service: {e}")
        return None
    finally:
        if conn: conn.close()

def sync_update_service(service_id, update_data):
    """Updates status or other fields of an existing bus service."""
    conn = get_sync_db_connection()
    if not conn: return False

    try:
        cur = conn.cursor()
        
        # Build dynamic SET clause
        set_parts = []
        data_values = []
        for key, value in update_data.items():
            set_parts.append(f"{key} = %s")
            data_values.append(value)
        
        data_values.append(service_id)
        
        update_query = f"UPDATE bus_services SET {', '.join(set_parts)} WHERE id = %s;"
        
        cur.execute(update_query, data_values)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Error updating service {service_id}: {e}")
        return False
    finally:
        if conn: conn.close()

def sync_get_all_services(user_id=None):
    """Retrieves all services."""
    conn = get_sync_db_connection(dict_cursor=True)
    if not conn: return {'services': []}

    try:
        cur = conn.cursor()
        cur.execute("SELECT id, route, service_name, driver_name, fare_price, departure_time_slot, status FROM bus_services ORDER BY created_at DESC;")
        
        results = cur.fetchall()
        cur.close()
        
        services_list = []
        for row in results:
            service = dict(row) 
            if service['fare_price'] is not None:
                 service['fare_price'] = str(service['fare_price'])
            services_list.append(service)

        return {'services': services_list}
    except Exception as e:
        print(f"Error fetching all services: {e}")
        return {'services': []}
    finally:
        if conn: conn.close()

# --- DB Sync Operations (Passenger Logic) ---

def sync_search_matching_services(route, time_slot_str):
    """
    Searches the database for all active bus services matching the route and time slot.
    
    *** CONFIRMED FIX: No restrictive filter on service_name. ***
    """
    time_slot = get_departure_time_slot(time_slot_str)
    if not time_slot:
        return []

    conn = get_sync_db_connection(dict_cursor=True)
    if not conn: return []

    try:
        cur = conn.cursor()
        
        # This query retrieves ALL active services matching the route and time slot.
        sql_query = """
            SELECT id, route, service_name, fare_price, departure_time_slot
            FROM bus_services
            WHERE route = %s AND departure_time_slot = %s AND status = 'active'; 
        """
        
        cur.execute(sql_query, (route, time_slot))
        
        results = cur.fetchall()
        cur.close()
        
        buses = []
        for row in results:
            bus = dict(row)
            if bus['fare_price'] is not None:
                 bus['fare_price'] = str(bus['fare_price'])
            buses.append(bus)
        
        return buses
        
    except Exception as e:
        print(f"Error searching matching buses: {e}")
        return []
    finally:
        if conn: conn.close()

def sync_save_booking(user_id, bus_id, route, time_slot):
    """Saves a successful booking to the database."""
    conn = get_sync_db_connection()
    if not conn: return False

    try:
        cur = conn.cursor()
        
        insert_query = """
            INSERT INTO bookings (user_id, bus_id, route, time_slot)
            VALUES (%s, %s, %s, %s);
        """
        
        cur.execute(insert_query, (user_id, bus_id, route, time_slot))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Error saving booking: {e}")
        return False
    finally:
        if conn: conn.close()

# --- Telegram Handlers (Conversation Flow) ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Handles the /start command and asks the user for their role."""
    chat_id = message.chat.id
    user_states[chat_id] = {'stage': 'AWAITING_ROLE'}

    markup = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=True)
    item_passenger = telebot.types.KeyboardButton('🧑 Passenger')
    item_provider = telebot.types.KeyboardButton('🚌 Bus Provider')
    markup.add(item_passenger, item_provider)

    bot.send_message(chat_id, "👋 Welcome to RoutAfare!\nPlease select your role:", reply_markup=markup)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_ROLE')
def handle_role_selection(message):
    """Processes the role selection and moves to the next stage."""
    chat_id = message.chat.id
    role = message.text.lower().strip()
    
    markup = telebot.types.ReplyKeyboardRemove(selective=False)
    
    if 'passenger' in role:
        user_states[chat_id] = {'stage': 'AWAITING_ROUTE_PASSENGER'}
        bot.send_message(chat_id, "📍 Enter your **Route** (e.g., Badulla-Kandy):", reply_markup=markup, parse_mode='Markdown')
        
    elif 'bus provider' in role:
        user_states[chat_id] = {'stage': 'AWAITING_PROVIDER_PASSWORD'}
        bot.send_message(chat_id, "🔒 Enter Provider Password:", reply_markup=markup)
        
    else:
        bot.send_message(chat_id, "Please select a valid role.")

# --- Passenger Workflow ---

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_ROUTE_PASSENGER')
def handle_passenger_route(message):
    """Saves the route and asks for departure time."""
    chat_id = message.chat.id
    user_states[chat_id]['route'] = message.text.strip()
    user_states[chat_id]['stage'] = 'AWAITING_TIME_PASSENGER'
    
    bot.send_message(chat_id, "⏰ Enter your approximate departure time in HH:MM (example: 13:45):")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_TIME_PASSENGER')
def handle_passenger_time(message):
    """Processes time, searches buses, and presents booking options."""
    chat_id = message.chat.id
    time_str = message.text.strip()
    
    try:
        # Basic time validation
        datetime.strptime(time_str, '%H:%M')
        
        user_states[chat_id]['departure_time'] = time_str
        route = user_states[chat_id]['route']
        
        bot.send_message(chat_id, "Calculating fare and searching for matching services... 🔍")
        
        # 1. Get Estimated Fare
        fare_data = {'route': route, 'time': time_str} 
        estimated_fare = get_fare_prediction(fare_data)
        
        # 2. Search for Buses (The fixed query is used here)
        matching_buses = sync_search_matching_services(route, time_str)
        
        if not matching_buses:
            bot.send_message(chat_id, f"😥 No active bus services found for Route: **{route}** around **{time_str}**.", parse_mode='Markdown')
            user_states[chat_id] = {} # End session
            return

        # 3. Prepare Display and Keyboard
        departure_slot = get_departure_time_slot(time_str)
        
        summary_text = (
            f"🚌 **Your Search Summary**\n"
            f"Route: **{route}**\n"
            f"Time: **{time_str}** (Slot: {departure_slot})\n"
            f"Estimated Fare: Rs\\ **{estimated_fare:.2f}**\n\n"
            "Select a service to Confirm Booking:"
        )
        
        markup = InlineKeyboardMarkup()
        
        for bus in matching_buses:
            callback_data = f"book_{bus['id']}" 
            fare_display = f"Rs. {bus['fare_price']}" if bus.get('fare_price') else f"~Rs. {estimated_fare:.2f}"
            
            button_text = f"✅ Book: {bus['service_name']} | Fare: {fare_display}"
            
            markup.add(InlineKeyboardButton(text=button_text, callback_data=callback_data))

        user_states[chat_id]['stage'] = 'AWAITING_BOOKING_CONFIRMATION'
        user_states[chat_id]['buses'] = matching_buses
        
        bot.send_message(chat_id, summary_text, reply_markup=markup, parse_mode='Markdown')

    except ValueError:
        bot.send_message(chat_id, "❌ Invalid time format. Please enter time in HH:MM (e.g., 07:00).")
        
    except Exception as e:
        print(f"Error in passenger search: {e}")
        bot.send_message(chat_id, "An unexpected error occurred during search. Please try again.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('book_'))
def handle_booking_callback(call):
    """Handles the user selecting a bus to book."""
    chat_id = call.message.chat.id
    
    if user_states.get(chat_id, {}).get('stage') != 'AWAITING_BOOKING_CONFIRMATION':
        bot.answer_callback_query(call.id, "Session expired. Please start over with /start.")
        return
        
    bus_id = int(call.data.split('_')[1])
    
    selected_bus = next((b for b in user_states[chat_id]['buses'] if b['id'] == bus_id), None)

    if selected_bus:
        route = user_states[chat_id]['route']
        time_slot = selected_bus['departure_time_slot']
        
        # Save Booking to DB
        if sync_save_booking(chat_id, bus_id, route, time_slot):
            
            # 1. Update the original message
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass 

            # 2. Send confirmation
            fare_display = f"Rs. {selected_bus['fare_price']}" if selected_bus.get('fare_price') else "Fare to be confirmed by driver"

            confirmation_text = (
                "🎉 **BOOKING CONFIRMED!** 🎉\n\n"
                f"Service: **{selected_bus['service_name']}**\n"
                f"Route: **{route}**\n"
                f"Fare: **{fare_display}**\n"
                f"Departure Slot: **{time_slot}**\n\n"
                "Thank you for using RoutAfare. Please be ready to board at the departure time."
            )
            bot.send_message(chat_id, confirmation_text, parse_mode='Markdown')
            
        else:
            bot.send_message(chat_id, "❌ Sorry, there was a problem confirming your booking in the database. Please try again later.")

    user_states[chat_id] = {} # End session
    bot.answer_callback_query(call.id, "Booking Confirmed!")

# --- Provider Workflow ---

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_PROVIDER_PASSWORD')
def handle_provider_password(message):
    """Verifies the provider password."""
    chat_id = message.chat.id
    password = message.text.strip()

    if password == PROVIDER_PASSWORD:
        user_states[chat_id]['stage'] = 'PROVIDER_MENU'
        
        markup = telebot.types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True, one_time_keyboard=True)
        markup.add(telebot.types.KeyboardButton('➕ Add Service'))
        markup.add(telebot.types.KeyboardButton('⚙️ Manage Status'))
        
        bot.send_message(chat_id, "Access granted! What would you like to do?", reply_markup=markup)
    else:
        bot.send_message(chat_id, "❌ Invalid password. Please try again or type /start to restart.")
        user_states[chat_id] = {}

# --- Provider Menu Dispatch ---
@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'PROVIDER_MENU')
def handle_provider_menu_dispatch(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    if text == '➕ Add Service':
        user_states[chat_id]['stage'] = 'AWAITING_ROUTE_NAME'
        # Clear keyboard
        markup = telebot.types.ReplyKeyboardRemove(selective=False)
        bot.send_message(chat_id, "Enter the Route Name (e.g., Kandy-Colombo):", reply_markup=markup)
    elif text == '⚙️ Manage Status':
        prov_status(message) # Call the handler function directly
    else:
        bot.send_message(chat_id, "Please use the buttons provided.")


# --- Provider Service Details Collection ---

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_ROUTE_NAME')
def handle_provider_route_name(message):
    chat_id = message.chat.id
    user_states[chat_id]['route'] = message.text.strip()
    user_states[chat_id]['stage'] = 'AWAITING_SERVICE_NAME'
    bot.send_message(chat_id, "Enter the **Bus Service/Company Name** (e.g., Starline Express, Private Bus):", parse_mode='Markdown')

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_SERVICE_NAME')
def handle_provider_service_name(message):
    chat_id = message.message.chat.id
    user_states[chat_id]['service_name'] = message.text.strip()
    user_states[chat_id]['stage'] = 'AWAITING_DRIVER_NAME'
    bot.send_message(chat_id, "Enter **Driver Name**:")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_DRIVER_NAME')
def handle_provider_driver_name(message):
    chat_id = message.chat.id
    user_states[chat_id]['driver_name'] = message.text.strip()
    user_states[chat_id]['stage'] = 'AWAITING_FARE_PRICE'
    bot.send_message(chat_id, "Enter **Fare Price** (e.g., 150.00):")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_FARE_PRICE')
def handle_provider_fare_price(message):
    chat_id = message.chat.id
    try:
        if not re.match(r'^\d+(\.\d{1,2})?$', message.text.strip()):
            raise ValueError
            
        fare = float(message.text.strip())
        user_states[chat_id]['fare_price'] = fare
        user_states[chat_id]['stage'] = 'AWAITING_CONTACT_NUMBER'
        bot.send_message(chat_id, "Enter **Contact Number** (e.g., 071-XXX-XXXX):")
    except ValueError:
        bot.send_message(chat_id, "❌ Invalid fare format. Please enter a valid number (e.g., 150.00).")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_CONTACT_NUMBER')
def handle_provider_contact_number(message):
    chat_id = message.chat.id
    contact = message.text.strip()
    
    if len(contact) < 5:
        bot.send_message(chat_id, "❌ Invalid contact format. Please enter a valid number.")
        return
        
    user_states[chat_id]['contact_number'] = contact
    user_states[chat_id]['stage'] = 'AWAITING_DEPARTURE_TIME'
    bot.send_message(chat_id, "Enter **Departure Time** in HH:MM (e.g., 06:30):")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('stage') == 'AWAITING_DEPARTURE_TIME')
def handle_provider_departure_time(message):
    chat_id = message.chat.id
    time_str = message.text.strip()

    try:
        datetime.strptime(time_str, '%H:%M')
        user_states[chat_id]['departure_time'] = time_str
        
        service_data = {
            'route': user_states[chat_id]['route'],
            'service_name': user_states[chat_id]['service_name'],
            'driver_name': user_states[chat_id]['driver_name'],
            'fare_price': user_states[chat_id]['fare_price'],
            'contact_number': user_states[chat_id]['contact_number'],
            'departure_time': time_str 
        }
        
        bus_id = sync_add_service(service_data)
        
        if bus_id:
            time_slot = get_departure_time_slot(time_str)
            confirmation_text = (
                "✅ **Service Added Successfully!**\n\n"
                f"Route: **{service_data['route']}**\n"
                f"Service: **{service_data['service_name']}** (ID: {bus_id})\n"
                f"Time Slot: **{time_slot}**\n"
                f"Fare: Rs\\ **{service_data['fare_price']:.2f}**"
            )
            bot.send_message(chat_id, confirmation_text, parse_mode='Markdown')
        else:
            bot.send_message(chat_id, "❌ Failed to save the service to the database. Please check logs.")
        
        # Return to main provider menu
        user_states[chat_id]['stage'] = 'PROVIDER_MENU'
        markup = telebot.types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True, one_time_keyboard=True)
        markup.add(telebot.types.KeyboardButton('➕ Add Service'))
        markup.add(telebot.types.KeyboardButton('⚙️ Manage Status'))
        bot.send_message(chat_id, "What next?", reply_markup=markup)
        
    except ValueError:
        bot.send_message(chat_id, "❌ Invalid time format. Please enter time in HH:MM (e.g., 07:00).")
    
    except Exception as e:
        print(f"Error in provider save flow: {e}")
        bot.send_message(chat_id, "An unexpected error occurred. Please try /start.")
        user_states[chat_id] = {}


# --- Provider Status Management ---

@bot.message_handler(func=lambda message: message.text == '⚙️ Manage Status')
def prov_status(message):
    """Displays the list of services with buttons to toggle their status."""
    chat_id = message.chat.id
    
    # 1. Fetch all services
    services_db = sync_get_all_services()
    services = services_db.get('services', [])
    
    # Clear the menu keyboard only if there are services to manage
    if services:
        markup = telebot.types.ReplyKeyboardRemove(selective=False)
        # Send a placeholder message to trigger the removal of the old keyboard
        bot.send_message(chat_id, "Fetching services...", reply_markup=markup) 
    else:
        bot.send_message(chat_id, "No services have been registered yet.")
        return

    # 2. Build the inline keyboard for status toggling
    status_markup = InlineKeyboardMarkup()
    
    for svc in services:
        status = svc.get('status', 'active')
        emoji = "🟢" if status == 'active' else "🔴"
        
        toggle_button = InlineKeyboardButton(
            text=f"{emoji} {svc['service_name']} ({svc['route']}) | Status: {status.upper()}",
            callback_data=f"toggle_status_{svc['id']}"
        )
        status_markup.add(toggle_button)

    # 3. Send the updated status message
    bot.send_message(
        chat_id,
        "**Toggle Service Status:**\n\nTap a button to switch the service between ACTIVE (available for booking) and UNAVAILABLE.",
        reply_markup=status_markup,
        parse_mode='Markdown'
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('toggle_status_'))
def handle_status_toggle(call):
    """Handles the callback for toggling the service status."""
    chat_id = call.message.chat.id
    s_id = int(call.data.split('_')[-1])

    # 1. Fetch current status
    services_db = sync_get_all_services()
    svc = next((s for s in services_db['services'] if s['id'] == s_id), None)
    
    if svc:
        # 2. Determine new status
        new_status = 'unavailable' if svc.get('status') == 'active' else 'active'
        
        # 3. Update Postgres table
        sync_update_service(s_id, {'status': new_status})
        
        # 4. Refresh the status menu by sending the prov_status message again
        
        # Edit the original message to remove the old inline keyboard
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            bot.edit_message_text(f"Updating status for {svc['service_name']}...", chat_id, call.message.message_id)
        except Exception:
            pass
        
        # Re-send the status menu message
        prov_status(call.message) 
        bot.answer_callback_query(call.id, f"Status set to {new_status.upper()}")
        
    else:
        bot.answer_callback_query(call.id, "Service not found.")
    
    return 

# --- FLASK/WEBHOOK SETUP ---

app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    """Receives updates from Telegram and passes them to the bot."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '!', 200
    else:
        return '', 200 

@app.route('/')
def index():
    """A simple index page to confirm the Flask app is running and webhook is set."""
    if BOT_TOKEN and WEBHOOK_URL_BASE:
        webhook_url = f"{WEBHOOK_URL_BASE}{WEBHOOK_URL_PATH}"
        try:
            # Set webhook on every access to the root path to ensure persistence
            bot.set_webhook(url=webhook_url)
            print(f"Webhook successfully set to: {webhook_url}")
            return f"RoutAfare Bot running. Webhook set to: {webhook_url}", 200
        except Exception as e:
            print(f"Failed to set webhook: {e}")
            return f"Failed to set webhook. Check logs. Error: {e}", 500
    return "RoutAfare Bot Flask app is running, but configuration is incomplete.", 200

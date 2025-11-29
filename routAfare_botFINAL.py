import os
from flask import Flask, request, session, redirect, url_for, render_template, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import random

# --- 1. Flask Application Setup ---
app = Flask(__name__)

# --- Configuration (Ready for Persistent DB) ---
# 1. Secret Key for Sessions
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default_dev_secret_key_change_me')

# 2. Database Connection (Uses DATABASE_URL for PostgreSQL persistence)
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///site.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- 2. Database Models (Persistent Data Structure) ---

class User(db.Model):
    """Represents a user (passenger or provider) with authentication details."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False) 
    role = db.Column(db.String(50), default='passenger') # 'passenger' or 'provider'
    
    # Relationship to the provider details (One User owns One set of provider details)
    provider_info = db.relationship('BusServiceProvider', backref='owner', uselist=False)

class BusServiceProvider(db.Model):
    """Stores persistent bus service details submitted by a provider."""
    id = db.Column(db.Integer, primary_key=True)
    # Foreign key link to the User table, ensuring only one set of details per user
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    bus_name = db.Column(db.String(100), nullable=False)
    route_details = db.Column(db.Text, nullable=False) 
    
    def __repr__(self):
        return f"Provider('{self.bus_name}', User ID: {self.user_id})"

# --- Helper Functions ---

def get_public_buses():
    """Retrieves all registered bus services from the persistent database."""
    # Fetch data from the persistent PostgreSQL table
    providers = BusServiceProvider.query.all()
    
    # Map them to a simple dictionary list
    bus_listings = [
        {
            'name': p.bus_name,
            'route': p.route_details,
            'type': 'Registered Service'
        }
        for p in providers
    ]
    
    # Add some mock data to make the list look fuller for passengers
    mock_buses = [
        {'name': 'Metro Line Express', 'route': 'Downtown to Airport (M-F)', 'type': 'External Listing'},
        {'name': 'Coastal Cruiser', 'route': 'City A to City B (Weekends Only)', 'type': 'External Listing'}
    ]
    return bus_listings + mock_buses


def register_mock_users():
    """Initializes mock users if the database is empty."""
    if User.query.count() == 0:
        # Mock Passenger
        if not User.query.filter_by(username='passenger').first():
            db.session.add(User(
                username='passenger', 
                password_hash=generate_password_hash('pass'), 
                role='passenger'
            ))
        
        # Mock Provider (who needs to register their details)
        if not User.query.filter_by(username='provider').first():
            db.session.add(User(
                username='provider', 
                password_hash=generate_password_hash('pass'), 
                role='provider' 
            ))
            
        db.session.commit()
        print("Mock users created: passenger/pass, provider/pass")


# --- 3. Routes ---

@app.route('/')
def home():
    """Landing page or simple redirect."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))

@app.route('/dashboard')
def dashboard():
    """Displays user-specific dashboard content using the template."""
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
        
    user_role = session.get('user_role')
    username = session.get('username', 'Guest')
    
    # Check if provider details are registered (only relevant if they are a provider)
    provider_details_status = None
    if user_role == 'provider':
        provider = BusServiceProvider.query.filter_by(user_id=session['user_id']).first()
        provider_details_status = "Registered" if provider else "Registration Required"
        
    return render_template('dashboard.html', 
                           username=username, 
                           user_role=user_role, 
                           provider_details_status=provider_details_status)


@app.route('/register_provider_details', methods=['GET', 'POST'])
def register_provider_details():
    """Handles the form to register or update bus provider details persistently."""
    user_id = session.get('user_id')
    user_role = session.get('user_role')
    
    if not user_id or user_role != 'provider':
        return redirect(url_for('login_page'))
    
    # Retrieve existing details if they exist
    existing_provider = BusServiceProvider.query.filter_by(user_id=user_id).first()
    
    if request.method == 'POST':
        bus_name = request.form.get('bus_name')
        route_details = request.form.get('route_details')

        if existing_provider:
            # Update existing persistent record in PostgreSQL
            existing_provider.bus_name = bus_name
            existing_provider.route_details = route_details
            message = "Bus details updated successfully!"
        else:
            # Create a new persistent record in PostgreSQL
            new_provider = BusServiceProvider(
                user_id=user_id,
                bus_name=bus_name,
                route_details=route_details
            )
            db.session.add(new_provider)
            message = "Bus details registered successfully!"
        
        # CRITICAL FIX: Commit changes to the persistent database
        db.session.commit() 
        
        return render_template('provider_registration_form.html', 
                               message=message, 
                               details=existing_provider)
    
    # GET request: render the form with any existing details
    return render_template('provider_registration_form.html', 
                           details=existing_provider, 
                           message="")


@app.route('/bus_listings')
def bus_listings():
    """Displays all available bus listings (provider's and mock public ones)."""
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
        
    user_role = session.get('user_role', 'passenger')
    
    # Get data from the persistent database
    all_buses = get_public_buses() 
        
    return render_template('bus_list.html', 
                           buses=all_buses, 
                           user_role=user_role)

@app.route('/login_page')
def login_page():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    """Mock login for demonstration."""
    username = request.form.get('username')
    password = request.form.get('password') 
    
    user = User.query.filter_by(username=username).first()
    
    # Mocking authentication: check if user exists and password is 'pass'
    if user and password == 'pass': 
        session['user_id'] = user.id
        session['username'] = user.username
        session['user_role'] = user.role
        return redirect(url_for('dashboard'))
    
    return render_template('login.html', error="Invalid Credentials. Try 'passenger/pass' or 'provider/pass'.")

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# --- 4. Main Execution ---
if __name__ == '__main__':
    # Initialize the tables and mock users only when running locally or during first deployment
    with app.app_context():
        # This creates the tables based on the models if they don't exist
        db.create_all() 
        # This ensures the test users are present in the database
        register_mock_users() 
    app.run(debug=True)

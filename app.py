import sys
import subprocess
import os

try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
    from apscheduler.schedulers.background import BackgroundScheduler
    from twilio.rest import Client
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Dependencies not found. Auto-installing from requirements.txt...")
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
    print("Dependencies installed successfully. Restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

import sqlite3
import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
CORS(app)

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', 'your_account_sid_here')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', 'your_auth_token_here')
TWILIO_FROM_PHONE = os.environ.get('TWILIO_FROM_PHONE', '+1234567890')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database.db')

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        phone TEXT
                    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS medicines (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        name TEXT NOT NULL,
                        dosage TEXT NOT NULL,
                        time TEXT NOT NULL,
                        frequency TEXT NOT NULL,
                        status TEXT DEFAULT 'not taken',
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        message TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )''')
    try:
        conn.execute('ALTER TABLE medicines ADD COLUMN user_id INTEGER')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('ALTER TABLE logs ADD COLUMN user_id INTEGER')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user_id = request.headers.get('X-User-Id')
        if not user_id:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(user_id, *args, **kwargs)
    return decorated

# Scheduler tasks
def check_reminders():
    # Runs every minute
    now = datetime.datetime.now()
    current_time = now.strftime("%H:%M")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    query = '''
        SELECT m.*, u.phone 
        FROM medicines m 
        JOIN users u ON m.user_id = u.id 
        WHERE m.time = ? AND m.status = "not taken"
    '''
    medicines = conn.execute(query, (current_time,)).fetchall()
    
    if medicines:
        twilio_client = None
        if TWILIO_ACCOUNT_SID != 'your_account_sid_here':
            try:
                twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            except Exception as e:
                print(f"Twilio Client Error: {e}")

        for med in medicines:
            msg = f"MediReminder 💊: It's time to take {med['dosage']} of {med['name']}!"
            
            # Save to Logs
            conn.execute('INSERT INTO logs (user_id, message, timestamp) VALUES (?, ?, ?)', 
                         (med['user_id'], msg, now.strftime("%Y-%m-%d %H:%M:%S")))
            print(f"Reminder Triggered: {msg}")
            
            # Send SMS or WhatsApp via Twilio
            if twilio_client:
                try:
                    if med['phone']:
                        # Auto-detect WhatsApp format to ensure 'from_' matches
                        from_number = TWILIO_FROM_PHONE
                        if med['phone'].startswith('whatsapp:') and not from_number.startswith('whatsapp:'):
                            from_number = 'whatsapp:' + from_number

                        message = twilio_client.messages.create(
                            body=msg,
                            from_=from_number,
                            to=med['phone']
                        )
                        print(f"Message Sent successfully! SID: {message.sid}")
                    else:
                        print("Skipped Twilio: User has no phone number configured.")
                except Exception as e:
                    print(f"Failed to send Message: {e}")
            else:
                print("Skipped Twilio: Credentials not configured.")
    
    conn.commit()
    conn.close()

def reset_daily_status():
    # Runs at midnight
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE medicines SET status = "not taken" WHERE frequency = "daily"')
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_reminders, trigger="cron", minute="*")
scheduler.add_job(func=reset_daily_status, trigger="cron", hour=0, minute=0)
scheduler.start()

# Auth endpoints
@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password')
    phone = data.get('phone', '')

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 200

    conn = get_db_connection()
    existing_user = conn.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (username,)).fetchone()
    if existing_user:
        conn.close()
        return jsonify({"error": "Username already exists"}), 200

    hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
    conn.execute('INSERT INTO users (username, password, phone) VALUES (?, ?, ?)', 
                 (username, hashed_pw, phone))
    conn.commit()
    conn.close()
    return jsonify({"message": "User created successfully"}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 200

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (username,)).fetchone()
    conn.close()

    if user and check_password_hash(user['password'], password):
        return jsonify({"message": "Login successful", "user_id": user['id'], "username": user['username']}), 200
    
    return jsonify({"error": "Invalid username or password"}), 200


# API endpoints
@app.route('/add_medicine', methods=['POST'])
@require_auth
def add_medicine(user_id):
    data = request.json
    name = data.get('name')
    dosage = data.get('dosage')
    time = data.get('time')
    frequency = data.get('frequency')
    
    if not all([name, dosage, time, frequency]):
        return jsonify({"error": "Missing fields"}), 400
        
    conn = get_db_connection()
    existing = conn.execute('SELECT * FROM medicines WHERE name = ? AND time = ? AND user_id = ?', (name, time, user_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Medicine with this name and time already exists"}), 400
        
    conn.execute('INSERT INTO medicines (user_id, name, dosage, time, frequency) VALUES (?, ?, ?, ?, ?)',
                 (user_id, name, dosage, time, frequency))
    conn.commit()
    conn.close()
    return jsonify({"message": "Medicine added successfully"}), 201

@app.route('/medicines', methods=['GET'])
@require_auth
def get_medicines(user_id):
    conn = get_db_connection()
    medicines = conn.execute('SELECT * FROM medicines WHERE user_id = ?', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(ix) for ix in medicines])

@app.route('/delete_medicine/<int:id>', methods=['DELETE'])
@require_auth
def delete_medicine(user_id, id):
    conn = get_db_connection()
    conn.execute('DELETE FROM medicines WHERE id = ? AND user_id = ?', (id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "Medicine deleted"}), 200

@app.route('/mark_taken', methods=['POST'])
@require_auth
def mark_taken(user_id):
    data = request.json
    med_id = data.get('medicine_id')
    if not med_id:
        return jsonify({"error": "Missing medicine_id"}), 400
        
    conn = get_db_connection()
    conn.execute('UPDATE medicines SET status = "taken" WHERE id = ? AND user_id = ?', (med_id, user_id))
    
    med = conn.execute('SELECT * FROM medicines WHERE id = ? AND user_id = ?', (med_id, user_id)).fetchone()
    if med:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"Logged: Took {med['dosage']} of {med['name']}"
        conn.execute('INSERT INTO logs (user_id, message, timestamp) VALUES (?, ?, ?)', (user_id, msg, now))
        
    conn.commit()
    conn.close()
    return jsonify({"message": "Marked as taken"}), 200

@app.route('/logs', methods=['GET'])
@require_auth
def get_logs(user_id):
    conn = get_db_connection()
    logs = conn.execute('SELECT * FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 50', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(ix) for ix in logs])

if __name__ == '__main__':
    init_db()
    port = int (os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False) 

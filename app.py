import sys
import subprocess
import os

try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
    from apscheduler.schedulers.background import BackgroundScheduler
    from twilio.rest import Client
    from dotenv import load_dotenv
    import psycopg2
    import psycopg2.extras
    load_dotenv()
except ImportError:
    print("Dependencies not found. Auto-installing from requirements.txt...")
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
    print("Dependencies installed successfully. Restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
CORS(app)

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', 'your_account_sid_here')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', 'your_auth_token_here')
TWILIO_FROM_PHONE = os.environ.get('TWILIO_FROM_PHONE', '+1234567890')

# PostgreSQL Connection
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        phone TEXT
                    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS medicines (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        name TEXT NOT NULL,
                        dosage TEXT NOT NULL,
                        time TEXT NOT NULL,
                        frequency TEXT NOT NULL,
                        status TEXT DEFAULT 'not taken',
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        message TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )''')
    conn.commit()
    cur.close()
    conn.close()
    print("Database initialized successfully!")

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
    now = datetime.datetime.now()
    current_time = now.strftime("%H:%M")
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = '''
            SELECT m.*, u.phone 
            FROM medicines m 
            JOIN users u ON m.user_id = u.id 
            WHERE m.time = %s AND m.status = 'not taken'
        '''
        cur.execute(query, (current_time,))
        medicines = cur.fetchall()

        if medicines:
            twilio_client = None
            if TWILIO_ACCOUNT_SID != 'your_account_sid_here':
                try:
                    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                except Exception as e:
                    print(f"Twilio Client Error: {e}")

            for med in medicines:
                msg = f"MediReminder: It's time to take {med['dosage']} of {med['name']}!"
                cur.execute('INSERT INTO logs (user_id, message, timestamp) VALUES (%s, %s, %s)',
                            (med['user_id'], msg, now.strftime("%Y-%m-%d %H:%M:%S")))
                print(f"Reminder Triggered: {msg}")

                if twilio_client and med['phone']:
                    try:
                        from_number = TWILIO_FROM_PHONE
                        if med['phone'].startswith('whatsapp:') and not from_number.startswith('whatsapp:'):
                            from_number = 'whatsapp:' + from_number
                        message = twilio_client.messages.create(
                            body=msg,
                            from_=from_number,
                            to=med['phone']
                        )
                        print(f"Message Sent! SID: {message.sid}")
                    except Exception as e:
                        print(f"Failed to send Message: {e}")

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Reminder check error: {e}")

def reset_daily_status():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE medicines SET status = 'not taken' WHERE frequency = 'daily'")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Reset status error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_reminders, trigger="cron", minute="*")
scheduler.add_job(func=reset_daily_status, trigger="cron", hour=0, minute=0)
scheduler.start()

# Auth endpoints
@app.route('/signup', methods=['POST'])
def signup():
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password')
        phone = data.get('phone', '')

        if not username or not password:
            return jsonify({"error": "Username and password required"}), 200

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(%s)', (username,))
        existing_user = cur.fetchone()

        if existing_user:
            cur.close()
            conn.close()
            return jsonify({"error": "Username already exists"}), 200

        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        cur.execute('INSERT INTO users (username, password, phone) VALUES (%s, %s, %s)',
                    (username, hashed_pw, phone))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "User created successfully"}), 201
    except Exception as e:
        print(f"Signup error: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password')

        if not username or not password:
            return jsonify({"error": "Username and password required"}), 200

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(%s)', (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password'], password):
            return jsonify({"message": "Login successful", "user_id": user['id'], "username": user['username']}), 200

        return jsonify({"error": "Invalid username or password"}), 200
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"error": "Server error"}), 500

# API endpoints
@app.route('/add_medicine', methods=['POST'])
@require_auth
def add_medicine(user_id):
    try:
        data = request.json
        name = data.get('name')
        dosage = data.get('dosage')
        time = data.get('time')
        frequency = data.get('frequency')

        if not all([name, dosage, time, frequency]):
            return jsonify({"error": "Missing fields"}), 400

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM medicines WHERE name = %s AND time = %s AND user_id = %s', (name, time, user_id))
        existing = cur.fetchone()

        if existing:
            cur.close()
            conn.close()
            return jsonify({"error": "Medicine with this name and time already exists"}), 400

        cur.execute('INSERT INTO medicines (user_id, name, dosage, time, frequency) VALUES (%s, %s, %s, %s, %s)',
                    (user_id, name, dosage, time, frequency))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Medicine added successfully"}), 201
    except Exception as e:
        print(f"Add medicine error: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/medicines', methods=['GET'])
@require_auth
def get_medicines(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM medicines WHERE user_id = %s', (user_id,))
        medicines = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(m) for m in medicines])
    except Exception as e:
        print(f"Get medicines error: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/delete_medicine/<int:id>', methods=['DELETE'])
@require_auth
def delete_medicine(user_id, id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM medicines WHERE id = %s AND user_id = %s', (id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Medicine deleted"}), 200
    except Exception as e:
        print(f"Delete medicine error: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/mark_taken', methods=['POST'])
@require_auth
def mark_taken(user_id):
    try:
        data = request.json
        med_id = data.get('medicine_id')
        if not med_id:
            return jsonify({"error": "Missing medicine_id"}), 400

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("UPDATE medicines SET status = 'taken' WHERE id = %s AND user_id = %s", (med_id, user_id))
        cur.execute('SELECT * FROM medicines WHERE id = %s AND user_id = %s', (med_id, user_id))
        med = cur.fetchone()

        if med:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = f"Logged: Took {med['dosage']} of {med['name']}"
            cur.execute('INSERT INTO logs (user_id, message, timestamp) VALUES (%s, %s, %s)', (user_id, msg, now))

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Marked as taken"}), 200
    except Exception as e:
        print(f"Mark taken error: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route('/logs', methods=['GET'])
@require_auth
def get_logs(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM logs WHERE user_id = %s ORDER BY id DESC LIMIT 50', (user_id,))
        logs = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(l) for l in logs])
    except Exception as e:
        print(f"Get logs error: {e}")
        return jsonify({"error": "Server error"}), 500

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
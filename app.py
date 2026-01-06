import sys
import logging
import json
import threading
import re
import hashlib
import time
import uuid
import requests
import subprocess
import webbrowser
import os
import base64
import io
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import Binary
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from neonize.client import NewClient
from neonize.events import ConnectedEv, MessageEv, PairStatusEv, LoggedOutEv, QREv
from neonize.types import MessageServerID
from neonize.utils import log
from datetime import timedelta
import socket
import smtplib
from email.mime.text import MIMEText
from groq import Groq
import qrcode
import datetime
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
# Database Connection
# Check Environment Variable (Render)
DB_URL = os.environ.get("DATABASE_URL")

if not DB_URL:
    print("CRITICAL ERROR: DATABASE_URL environment variable is not set. Please set it in Render or your local environment.")

def get_ist_time():
    try:
        # Calculate IST: UTC + 5:30
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        ist_now = utc_now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%H:%M")
    except Exception as e:
        print(f"Error calculating IST time: {e}")
    return None

# --- HELPER FUNCTIONS ---
def send_email(user_id, subject, body, reply_to=None):
    try:
        user = get_user_by_id(user_id)
        config = load_user_config(user_id)
        dest_email = config.get("dest_email")

        if not user or not user.get('google_token') or not dest_email:
            print(f"[User {user_id}] Email configuration missing (No Token or Dest Email).")
            return False

        # Load Credentials
        client_config = get_google_client_config()
        creds = Credentials(
            token=user['google_token'],
            refresh_token=user.get('google_refresh_token'),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_config["web"]["client_id"],
            client_secret=client_config["web"]["client_secret"],
            scopes=['https://www.googleapis.com/auth/gmail.send']
        )

        # Refresh if expired - this automatically updates the object but we must save back to DB?
        # The library handles refresh in-memory during requests, but good practice to check validity
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            try:
                creds.refresh(Request())
                # Update DB with new access token
                update_user_tokens(user_id, creds.token, creds.refresh_token)
            except Exception as e:
                 print(f"Token Refresh Error: {e}")
                 return False

        service = build('gmail', 'v1', credentials=creds)
        
        message = MIMEText(body)
        message['to'] = dest_email
        message['from'] = user['email']
        message['subject'] = subject
        if reply_to:
            message.add_header('Reply-To', reply_to)
            
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw}

        service.users().messages().send(userId="me", body=create_message).execute()
        print(f"Email sent to {dest_email} via Gmail API", flush=True)
        return True
    except Exception as e:
        print(f"Gmail API Error: {e}", flush=True)
        return False

def analyze_message(api_key, text):
    try:
        client = Groq(api_key=api_key)
        prompt = f"""
        Analyze the following LinkedIn post text to determine if it is a valid USA Job Opening.

        STRICT REJECTION RULES (If any of these are true, "is_usa_hiring" MUST be false):
        1. Phone Numbers: If a phone/WhatsApp number is present, it MUST start with +1 (USA). If it starts with +91 or any other country code, REJECT IT immediately.
        2. Salary: If a salary is mentioned, it MUST be in Dollars ($). If it is in Rupees (₹), Lakhs, or implies a non-US monthly rate (e.g., "20k-50k monthly" without $ context), REJECT IT.
        3. Location: If the location is explicitly outside the USA, REJECT IT.

        CRITERIA FOR "is_usa_hiring" = true:
        - It MUST be a job opening/hiring.
        - It MUST be located in the USA (or imply USA context with $ salary / +1 phone).
        - IGNORE: Hotlists, Services, Achievements, Promotions, General News.
        - IGNORE: Job seekers asking for jobs.

        If it passes all rules, extract:
        - Job Role/Title (e.g., "Python Developer"). Use "Unknown Role" if not found.
        - Contact Email Address (if present). Use null if not found.

        Post Text:
        "{text[:3000]}" 
        
        Reply ONLY with a JSON object in this format:
        {{
            "is_usa_hiring": true/false,
            "role": "extracted role",
            "email": "extracted_email@example.com" or null
        }}
        """
        
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that outputs only valid JSON."
                },
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="llama-3.1-8b-instant",
            temperature=0
        )
        
        response_text = chat_completion.choices[0].message.content.strip()
        # Extract JSON if wrapped in code blocks
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if match:
            json_str = match.group(0)
            return json.loads(json_str)

    except Exception as e:
        print(f"AI Error: {e}", flush=True)
        
    return {"is_usa_hiring": False, "role": "Unknown", "email": None}

# --- FLASK APP SETUP ---
app = Flask(__name__)
# Fix for Render (HTTPS/Proxy headers)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.secret_key = "super_secret_key_change_this_in_production"
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# --- DATA STORAGE PATHS ---
# For Render (Ephemeral), we store cognitive.db in the current working directory
APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
USER_DATA_FOLDER = os.path.join(APP_FOLDER, "whatsapp_sessions")
os.makedirs(USER_DATA_FOLDER, exist_ok=True)

# --- GLOBAL MANAGERS ---
active_clients = {}
qr_data_store = {}

# --- DATABASE FUNCTIONS (NEON / POSTGRES) ---
def get_db_connection():
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # 1. Users Table
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id SERIAL PRIMARY KEY, username TEXT UNIQUE, email TEXT, password TEXT, 
                      google_token TEXT, google_refresh_token TEXT)''')
        
        # 2. User Configs Table (Stores JSON config per user)
        c.execute('''CREATE TABLE IF NOT EXISTS user_configs
                     (user_id INTEGER PRIMARY KEY REFERENCES users(id), config JSONB)''')
        
        # 3. Messages Table (Stores chat history)
        # Added content_hash for duplicate detection and sent_email for stats
        c.execute('''CREATE TABLE IF NOT EXISTS messages
                     (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), 
                      group_jid TEXT, sender TEXT, text TEXT, timestamp TEXT, 
                      content_hash TEXT, sent_email BOOLEAN DEFAULT FALSE,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Migration: Add columns if not exists
        try:
            c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_hash TEXT")
            c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS sent_email BOOLEAN DEFAULT FALSE")
        except:
            pass
            
        # 4. Session Files Table (For WhatsApp Persistence)
        c.execute('''CREATE TABLE IF NOT EXISTS session_files (
                        user_id INTEGER PRIMARY KEY REFERENCES users(id),
                        session_data BYTEA
                    )''')

        conn.commit()
        print("Neon Database Initialized Successfully.")
    except Exception as e:
        print(f"DB Init Error: {e}")
    finally:
        if conn:
            conn.close()

def get_user(username):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = c.fetchone()
        return user
    except Exception as e:
        print(f"DB Error (get_user): {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_by_email(email):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = c.fetchone()
        return user
    except Exception as e:
        print(f"DB Error (get_user_by_email): {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_by_id(user_id):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        user = c.fetchone()
        return user
    except Exception as e:
        print(f"DB Error (get_user_by_id): {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_user_tokens(user_id, token, refresh_token):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Only update refresh_token if it is provided (sometimes google doesn't send it on re-auth)
        if refresh_token:
            c.execute("UPDATE users SET google_token=%s, google_refresh_token=%s WHERE id=%s", (token, refresh_token, user_id))
        else:
            c.execute("UPDATE users SET google_token=%s WHERE id=%s", (token, user_id))
        conn.commit()
    except Exception as e:
        print(f"DB Error (update_tokens): {e}")
    finally:
        if conn:
            conn.close()

def create_google_user(email, token, refresh_token):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        username = email.split('@')[0]
        # Check if username exists, append random if needed
        c.execute("SELECT id FROM users WHERE username=%s", (username,))
        if c.fetchone():
            username = f"{username}_{uuid.uuid4().hex[:4]}"
            
        c.execute("INSERT INTO users (username, email, password, google_token, google_refresh_token) VALUES (%s, %s, %s, %s, %s) RETURNING id", 
                  (username, email, "GOOGLE_LOGIN_NO_PASS", token, refresh_token))
        user_id = c.fetchone()['id']
        
        # Initialize default config
        default_config = {
            "dest_email": "",
            "allowed_jids": [], "groq_api_key": "",
            "auto_allow": False, "start_time": "09:00", "end_time": "18:00"
        }
        c.execute("INSERT INTO user_configs (user_id, config) VALUES (%s, %s)", (user_id, json.dumps(default_config)))
        
        conn.commit()
        return get_user_by_id(user_id)
    except Exception as e:
        print(f"DB Error (create_google_user): {e}")
        return None
    finally:
        if conn:
            conn.close()

def create_user(username, email, password):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        hashed_pw = generate_password_hash(password)
        c.execute("INSERT INTO users (username, email, password) VALUES (%s, %s, %s) RETURNING id", (username, email, hashed_pw))
        user_id = c.fetchone()['id']
        
        # Initialize default config
        default_config = {
            "email_user": "", "email_pass": "", "dest_email": "",
            "allowed_jids": [], "groq_api_key": "",
            "auto_allow": False, "start_time": "09:00", "end_time": "18:00"
        }
        c.execute("INSERT INTO user_configs (user_id, config) VALUES (%s, %s)", (user_id, json.dumps(default_config)))
        
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        return False
    except Exception as e:
        print(f"DB Error (create_user): {e}")
        return False
    finally:
        if conn:
            conn.close()

def load_user_config(user_id):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT config FROM user_configs WHERE user_id=%s", (user_id,))
        res = c.fetchone()
        if res:
            return res['config']
    except Exception as e:
        print(f"DB Error (load_config): {e}")
    finally:
        if conn:
            conn.close()
    return {}

def save_user_config(user_id, config):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE user_configs SET config=%s WHERE user_id=%s", (json.dumps(config), user_id))
        conn.commit()
    except Exception as e:
        print(f"DB Error (save_config): {e}")
    finally:
        if conn:
            conn.close()

def save_session_to_db(user_id):
    conn = None
    try:
        db_path = get_user_db_path(user_id)
        if not os.path.exists(db_path):
            return

        # Read binary file
        with open(db_path, 'rb') as f:
            file_data = f.read()

        conn = get_db_connection()
        c = conn.cursor()
        
        # Upsert (Insert or Update)
        c.execute('''INSERT INTO session_files (user_id, session_data) 
                     VALUES (%s, %s)
                     ON CONFLICT (user_id) 
                     DO UPDATE SET session_data = EXCLUDED.session_data''', 
                  (user_id, Binary(file_data)))
        conn.commit()
        print(f"[User {user_id}] Session saved to Database.", flush=True)
    except Exception as e:
        print(f"DB Error (save_session): {e}")
    finally:
        if conn:
            conn.close()

def load_session_from_db(user_id):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT session_data FROM session_files WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        
        if row and row['session_data']:
            db_path = get_user_db_path(user_id)
            # Write bytes to local file
            with open(db_path, 'wb') as f:
                f.write(row['session_data'])
            print(f"[User {user_id}] Session loaded from Database.", flush=True)
            return True
    except Exception as e:
        print(f"DB Error (load_session): {e}")
    finally:
        if conn:
            conn.close()
    return False

def delete_session_from_db(user_id):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM session_files WHERE user_id=%s", (user_id,))
        conn.commit()
        print(f"[User {user_id}] Session deleted from Database.", flush=True)
    except Exception as e:
        print(f"DB Error (delete_session): {e}")
    finally:
        if conn:
            conn.close()

def save_message(user_id, jid, sender, text, timestamp):
    conn = None
    try:
        content_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''INSERT INTO messages (user_id, group_jid, sender, text, timestamp, content_hash) 
                     VALUES (%s, %s, %s, %s, %s, %s)''', 
                  (user_id, str(jid), sender, text, timestamp, content_hash))
        conn.commit()
    except Exception as e:
        print(f"DB Error (save_message): {e}")
    finally:
        if conn:
            conn.close()

def is_duplicate_message(user_id, text):
    conn = None
    is_dup = False
    try:
        content_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
        conn = get_db_connection()
        c = conn.cursor()
        # Check if message with same hash exists for this user
        c.execute("SELECT id FROM messages WHERE user_id=%s AND content_hash=%s", (user_id, content_hash))
        if c.fetchone():
            is_dup = True
    except Exception as e:
        print(f"DB Error (check_dup): {e}")
    finally:
        if conn:
            conn.close()
    return is_dup

def get_email_stats(user_id):
    conn = None
    stats = {"today": 0, "total": 0}
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Total Sent
        c.execute("SELECT COUNT(*) FROM messages WHERE user_id=%s AND sent_email=TRUE", (user_id,))
        stats["total"] = c.fetchone()['count']
        
        # Today Sent (IST Calculation)
        # We'll calculate "Start of Today IST" in UTC to query the DB efficiently
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        ist_now = utc_now + timedelta(hours=5, minutes=30)
        ist_start_of_day = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Convert back to UTC for the query (approximate, since created_at is DB server time, usually UTC)
        utc_start_of_day = ist_start_of_day - timedelta(hours=5, minutes=30)
        
        c.execute("SELECT COUNT(*) FROM messages WHERE user_id=%s AND sent_email=TRUE AND created_at >= %s", (user_id, utc_start_of_day))
        stats["today"] = c.fetchone()['count']
        
    except Exception as e:
        print(f"Stats Error: {e}")
    finally:
        if conn:
            conn.close()
    return stats

def mark_email_sent(user_id, content_hash):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE messages SET sent_email = TRUE WHERE user_id=%s AND content_hash=%s", (user_id, content_hash))
        conn.commit()
    except Exception as e:
        print(f"Mark Sent Error: {e}")
    finally:
        if conn:
            conn.close()

def get_recent_messages(user_id):
    conn = None
    groups = {}
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Fetch last 50 messages for this user
        c.execute("SELECT group_jid, sender, text, timestamp FROM messages WHERE user_id=%s ORDER BY id DESC LIMIT 50", (user_id,))
        rows = c.fetchall()
        
        config = load_user_config(user_id)
        whitelisted = {item["jid"] for item in config.get("allowed_jids", [])}
        
        for row in rows:
            jid = row['group_jid']
            if jid not in groups:
                groups[jid] = {
                    "whitelisted": jid in whitelisted,
                    "messages": []
                }
            # Add message if we don't have too many for this group yet
            if len(groups[jid]["messages"]) < 5:
                groups[jid]["messages"].insert(0, { # Insert at beginning to keep chronological order
                    "sender": row['sender'],
                    "text": row['text'],
                    "timestamp": row['timestamp']
                })
                
    except Exception as e:
        print(f"DB Error (get_recent): {e}")
    finally:
        if conn:
            conn.close()
    return groups

# --- WHATSAPP CLIENT MANAGEMENT ---
def get_user_db_path(user_id):
    return os.path.join(USER_DATA_FOLDER, f"whatsapp_session_{user_id}.db")
def start_bot_for_user(user_id):
    if user_id in active_clients:
        return # Already running

    db_path = get_user_db_path(user_id)
    
    # 1. Try to load existing session from DB before starting
    load_session_from_db(user_id)

    # Client Setup
    client = NewClient(db_path)

    # Callback for QR Code
    @client.qr
    def on_qr(client, code_bytes: bytes):
        try:
            # Generate QR Image from bytes
            qr = qrcode.QRCode()
            qr.add_data(code_bytes)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            qr_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

            qr_data_store[user_id] = {
                "code": qr_base64,
                "connected": False
            }
            print(f"[User {user_id}] New QR Code Generated (captured via callback)")
        except Exception as e:
             print(f"[User {user_id}] Error processing QR callback: {e}")

    @client.event(ConnectedEv)
    def on_connect(client, event):
        print(f"[User {user_id}] Connected to WhatsApp!")
        qr_data_store[user_id] = {"code": None, "connected": True}
        # Save session to DB on connect to ensure persistence
        save_session_to_db(user_id)

    @client.event(PairStatusEv)
    def on_pair_status(client, event):
        print(f"[User {user_id}] Pair Status: {event}")
        # Save session to DB after pairing
        save_session_to_db(user_id)

    @client.event(LoggedOutEv)
    def on_logout(client, event):
        print(f"[User {user_id}] Logged Out! Cleaning up session...")
        qr_data_store[user_id] = {"code": None, "connected": False}
        
        # Delete from DB
        delete_session_from_db(user_id)
        
        # 1. Remove from active clients
        if user_id in active_clients:
            del active_clients[user_id]
            
        # 2. Try to disconnect and delete file
        try:
            # Attempt to disconnect to release file lock
            if hasattr(client, 'disconnect'):
                client.disconnect()
            
            # Wait a moment for file release
            time.sleep(1)
            
            if os.path.exists(db_path):
                os.remove(db_path)
                print(f"[User {user_id}] Session file deleted successfully.")
        except Exception as e:
            print(f"[User {user_id}] Error during logout cleanup: {e}")

    @client.event(MessageEv)
    def on_message(client, message):
        # 1. GET INFO
        chat_id = message.Info.MessageSource.Chat
        jid = chat_id.User
        is_group = "g.us" in str(chat_id)
        text = message.Message.conversation or message.Message.extendedTextMessage.text
        
        # 2. Store Group Messages (IN NEON)
        if is_group and text:
            timestamp = time.strftime("%I:%M %p")
            sender_name = getattr(message.Info, "PushName", getattr(message.Info, "push_name", "Unknown"))
            
            # Check for duplicates BEFORE saving
            is_dup = is_duplicate_message(user_id, text)
            
            # Save to DB (History)
            save_message(user_id, jid, sender_name, text, timestamp)

            # 3. Process Logic (Whitelist + AI)
            config = load_user_config(user_id)
            allowed_jids = {item["jid"] for item in config.get("allowed_jids", [])}
            
            # --- AUTO-ALLOW & TIME CHECK ---
            should_process = False
            if config.get("auto_allow", False):
                try:
                    # Fetch IST Time
                    ist_time_str = get_ist_time()
                    if ist_time_str:
                        current_time = time.strptime(ist_time_str, "%H:%M")
                        start_time = time.strptime(config.get("start_time", "09:00"), "%H:%M")
                        end_time = time.strptime(config.get("end_time", "18:00"), "%H:%M")
                        
                        if start_time <= current_time <= end_time:
                            should_process = True
                        else:
                            print(f"[User {user_id}] Message skipped: Outside allowed time window ({ist_time_str})")
                    else:
                        print(f"[User {user_id}] Message skipped: Could not fetch IST time")
                except Exception as e:
                    print(f"[User {user_id}] Time Check Error: {e}")
            else:
                print(f"[User {user_id}] Message skipped: Auto-Allow is OFF")

            if should_process and str(jid) in allowed_jids and text:
                if is_dup:
                    print(f"[User {user_id}] Skipping AI Alert: Duplicate Message.")
                    return

                print(f"[User {user_id}] Processing message from {jid}", flush=True)
                
                api_key = config.get("groq_api_key")
                if api_key:
                    result = analyze_message(api_key, text)
                    if result.get("is_usa_hiring"):
                        print(f"[User {user_id}] AI Match: YES. Sending email...", flush=True)
                        
                        role = result.get("role", "Unknown Role")
                        extracted_email = result.get("email")
                        
                        subject = f"Whatsapp Alert New job Found : {role}"
                        body = f"Sender: {sender_name}\nGroup: {jid}\n\nRole: {role}\nEmail: {extracted_email}\n\nMessage:\n{text}"
                        
                        if send_email(user_id, subject, body, reply_to=extracted_email):
                            # Mark as sent in DB ONLY if email success
                            content_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
                            mark_email_sent(user_id, content_hash)
                        else:
                            print(f"[User {user_id}] Email failed to send. Not marking as sent.", flush=True)
                        
                    else:
                        print(f"[User {user_id}] AI Match: NO", flush=True)
                else:
                    print(f"[User {user_id}] No API Key configured.")

    # Start Client in Thread
    def run_client():
        try:
            client.connect()
        except Exception as e:
            print(f"[User {user_id}] Client Error: {e}")

    t = threading.Thread(target=run_client, daemon=True)
    t.start()
    
    active_clients[user_id] = client

# --- ROUTES ---
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    
    user_id = session["user_id"]
    user = get_user(session["username"]) # Refresh user data
    config = load_user_config(user_id)
    
    # Ensure bot is running
    if user_id not in active_clients:
        start_bot_for_user(user_id)
        
    return render_template("index.html", username=session["username"], config=config)

from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest
import pathlib

# Allow HTTP for local dev
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Removed GOOGLE_CLIENT_SECRETS_FILE constant to avoid confusion
SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile", 
    "https://www.googleapis.com/auth/userinfo.email", 
    "openid",
    "https://www.googleapis.com/auth/gmail.send"
]

def get_google_client_config():
    # Check for Env Var first (Render)
    env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_creds:
        return json.loads(env_creds)
    
    # Fallback to local file (Local Dev)
    secrets_file = os.path.join(APP_FOLDER, "google.json")
    if os.path.exists(secrets_file):
        with open(secrets_file, 'r') as f:
            return json.load(f)
            
    raise Exception("Google Credentials not found (Env Var or File)")

def get_google_provider_cfg():
    return requests.get("https://accounts.google.com/.well-known/openid-configuration").json()

@app.route("/google_login")
def google_login():
    try:
        client_config = get_google_client_config()
        flow = Flow.from_client_config(
            client_config=client_config,
            scopes=SCOPES
        )
        flow.redirect_uri = url_for("oauth2callback", _external=True)
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true'
        )
        session["state"] = state
        return redirect(authorization_url)
    except Exception as e:
        return f"Error initiating Google Login: {e}"

@app.route("/oauth2callback")
def oauth2callback():
    try:
        state = session["state"]
        client_config = get_google_client_config()
        flow = Flow.from_client_config(
            client_config=client_config,
            scopes=SCOPES,
            state=state
        )
        flow.redirect_uri = url_for("oauth2callback", _external=True)

        # distinct_id is generic...
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)

        credentials = flow.credentials
        request_session = requests.session()
        cached_session = GoogleRequest(session=request_session)
        
        # Verify Token
        id_info = id_token.verify_oauth2_token(
            id_token=credentials.id_token,
            request=cached_session,
            audience=client_config["web"]["client_id"]
        )

        email = id_info.get("email")
        
        # Check if user exists
        user = get_user_by_email(email)
        if user:
            # Update tokens
            update_user_tokens(user['id'], credentials.token, credentials.refresh_token)
            session["user_id"] = user['id']
            session["username"] = user['username']
        else:
            # Create User
            user = create_google_user(email, credentials.token, credentials.refresh_token)
            if user:
                session["user_id"] = user['id']
                session["username"] = user['username']
            else:
                 return "Error creating user account."
        
        return redirect(url_for("index"))
        
    except Exception as e:
        return f"Authentication Failed: {e}"

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/save", methods=["POST"])
def save_config():
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    user_id = session["user_id"]
    current_config = load_user_config(user_id)
    
    # Update fields
    current_config["email_user"] = request.form.get("email_user")
    current_config["email_pass"] = request.form.get("email_pass")
    current_config["dest_email"] = request.form.get("dest_email")
    current_config["groq_api_key"] = request.form.get("groq_api_key")
    
    current_config["auto_allow"] = "auto_allow" in request.form
    current_config["start_time"] = request.form.get("start_time")
    current_config["end_time"] = request.form.get("end_time")
    
    save_user_config(user_id, current_config)
    return render_template("index.html", 
                           username=session["username"], 
                           config=current_config,
                           message="Settings saved successfully!")

@app.route("/qr_status")
def qr_status():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session["user_id"]
    if user_id not in active_clients:
        start_bot_for_user(user_id)
        
    data = qr_data_store.get(user_id, {"code": None, "connected": False})
    return jsonify(data)

@app.route("/group_messages")
def group_messages():
    if "user_id" not in session:
        return jsonify({})
    return jsonify(get_recent_messages(session["user_id"]))

@app.route("/add_to_whitelist", methods=["POST"])
def add_to_whitelist():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session["user_id"]
    data = request.json
    group_id = data.get("group_id")
    
    config = load_user_config(user_id)
    current_list = config.get("allowed_jids", [])
    
    # Check if already exists
    if not any(item['jid'] == group_id for item in current_list):
        current_list.append({"jid": group_id, "name": f"Group {group_id[:10]}"})
        config["allowed_jids"] = current_list
        save_user_config(user_id, config)
        
    return jsonify({"success": True})

@app.route("/remove_from_whitelist", methods=["POST"])
def remove_from_whitelist():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session["user_id"]
    data = request.json
    group_id = data.get("group_id")
    
    config = load_user_config(user_id)
    current_list = config.get("allowed_jids", [])
    
    # Filter out the item
    new_list = [item for item in current_list if item['jid'] != group_id]
    
    if len(new_list) != len(current_list):
        config["allowed_jids"] = new_list
        save_user_config(user_id, config)
        
    return jsonify({"success": True})

@app.route("/stats")
def stats():
    if "user_id" not in session:
        return jsonify({"today": 0, "total": 0})
    return jsonify(get_email_stats(session["user_id"]))

@app.route("/disconnect_whatsapp", methods=["POST"])
def disconnect_whatsapp():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    user_id = session["user_id"]
    
    # 1. Remove from active clients
    if user_id in active_clients:
        # We can't easily 'stop' the neonize client thread cleanly without a stop signal,
        # but we can remove it from our tracking so a new one starts.
        # Ideally, neonize client has a .disconnect() method.
        try:
            # active_clients[user_id].disconnect() # If available
            del active_clients[user_id]
        except:
            pass
            
    # 2. Clear QR Data
    if user_id in qr_data_store:
        del qr_data_store[user_id]
        
    # 3. Delete Session File (Force new QR)
    # AND Delete from DB
    delete_session_from_db(user_id)
    
    db_path = get_user_db_path(user_id)
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            print(f"[User {user_id}] Session file deleted for fresh login.")
        except Exception as e:
            print(f"[User {user_id}] Error deleting session file: {e}")
            
    return jsonify({"success": True})

# --- MAIN ---
if __name__ == "__main__":
    # Initialize DB on start
    init_db()
    print("Multi-User WhatsApp Manager Running...")
    app.run(host="0.0.0.0", port=5000, debug=True)
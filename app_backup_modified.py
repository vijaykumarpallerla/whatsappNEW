"""
Flask WhatsApp Web Client with Neonize
Runs Neonize client in background thread, Flask in main thread
"""
import json
import threading
import time
import traceback
import segno
import io
import base64
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for
from neonize.client import NewClient

# Global state
app = Flask(__name__)
is_connected = False
client_logged_out = False
qr_data_url = None
reconnect_requested = False
whatsapp_messages = {}
group_names = {}
state_lock = threading.Lock()

# Configuration
CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"


def load_config():
    """Load configuration from file"""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"whitelist": [], "email": ""}


def save_config(config):
    """Save configuration to file"""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)


def load_history():
    """Load message history"""
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_history():
    """Save message history"""
    global whatsapp_messages
    with open(HISTORY_FILE, "w") as f:
        json.dump(whatsapp_messages, f, indent=2)


def handle_qr_update(client_obj, qr_payload):
    """
    Called when Neonize generates a new QR code during login.
    Converts QR payload to PNG image and stores as base64 data URL.
    """
    global qr_data_url
    
    print("\n[QR] ========== CALLBACK FIRED ==========")
    print(f"[QR] Payload type: {type(qr_payload)}")
    print(f"[QR] Payload length: {len(str(qr_payload))}")
    
    try:
        # Generate QR code from payload
        qr = segno.make(qr_payload)
        print("[QR] QR code generated successfully")
        
        # Encode to PNG bytes
        buf = io.BytesIO()
        qr.save(buf, kind='png', scale=8)
        png_data = buf.getvalue()
        print(f"[QR] PNG data length: {len(png_data)} bytes")
        
        # Convert to base64 data URL
        b64_str = base64.b64encode(png_data).decode('utf-8')
        qr_data_url = f"data:image/png;base64,{b64_str}"
        
        with state_lock:
            qr_data_url_safe = qr_data_url
        
        print("[QR] UPDATED successfully. QR code is now ready in memory.")
        print("[QR] ========== CALLBACK COMPLETE ==========\n")
        
    except Exception as e:
        print(f"[QR] ERROR in callback: {str(e)}")
        print(traceback.format_exc())


def run_whatsapp_client():
    """
    Background thread: Manage Neonize WhatsApp client connection
    """
    global is_connected, client_logged_out, client, whatsapp_messages, reconnect_requested
    
    attempt_count = 0
    
    while True:
        attempt_count += 1
        print(f"\n[Client] ========== Connection Attempt #{attempt_count} ==========")
        print(f"[Client] Current state: is_connected={is_connected}, client_logged_out={client_logged_out}")
        
        try:
            # Register QR callback BEFORE connecting
            print("[Client] Registering QR callback...")
            client.qr(handle_qr_update)
            print("[Client] OK - QR callback registered")
            
            # Connect (this blocks until connection is lost)
            print("[Client] Now calling client.connect() - this will block...")
            client.connect()
            print("[Client] OK - client.connect() completed (was blocking)")
            
        except Exception as e:
            print(f"[Client] ERROR Exception during connect: {str(e)}")
            print(f"[Client] ERROR Exception type: {type(e).__name__}")
            
        # Connection lost, wait before retry (unless manual reconnect requested)
        print("[Client] Connection broken, checking if manual reconnect...")
        with state_lock:
            is_connected = False
            if reconnect_requested:
                print("[Client] [FAST] Manual reconnect requested, skipping sleep")
                reconnect_requested = False  # Reset flag
            else:
                print("[Client] Auto-reconnect, waiting 2 seconds...")
                time.sleep(2)


# Initialize client
client = NewClient(name="whatsapp_bot")

# Register QR callback (will be called during connect())
client.qr(handle_qr_update)


# Flask routes

@app.route('/')
def index():
    """Main page"""
    config = load_config()
    return render_template('index.html', email=config.get('email', ''))


@app.route('/whatsapp')
def whatsapp():
    """WhatsApp status page"""
    global is_connected, qr_data_url, whatsapp_messages, group_names
    
    with state_lock:
        status_data = {
            'connected': is_connected,
            'qr_data': qr_data_url,
            'messages': whatsapp_messages,
            'group_names': group_names,
        }
    
    return render_template('whatsapp.html', **status_data)


@app.route('/reconnect', methods=['POST'])
def reconnect():
    """
    Handle reconnect button click.
    Resets all state and forces client to reconnect.
    """
    global is_connected, client_logged_out, qr_data_url, whatsapp_messages, group_names, reconnect_requested
    
    print("\n[Reconnect] ========== USER CLICKED RECONNECT ========== ")
    
    try:
        with state_lock:
            print(f"[Reconnect] State before: qr_data_url={qr_data_url is not None}, is_connected={is_connected}")
            
            # Clear QR code
            qr_data_url = None
            print("[Reconnect] OK - Cleared qr_data_url")
            
            # Reset connection flags
            is_connected = False
            client_logged_out = False
            print("[Reconnect] OK - Reset is_connected=False, client_logged_out=False")
            
            # Clear caches
            whatsapp_messages.clear()
            group_names.clear()
            print("[Reconnect] OK - Cleared message and group caches")
        
        # Re-register QR callback
        print("[Reconnect] Re-registering QR callback...")
        client.qr(handle_qr_update)
        print("[Reconnect] OK - QR callback re-registered")
        
        # Signal fast reconnection
        with state_lock:
            reconnect_requested = True
            print("[Reconnect] [OK] Set reconnect_requested flag")
        
        # Force disconnect
        print("[Reconnect] Calling client.disconnect()...")
        try:
            client.disconnect()
            print("[Reconnect] [OK] client.disconnect() called")
        except Exception as e:
            print(f"[Reconnect] [WARNING] disconnect() threw: {str(e)}")
        
        # Wait for background thread to exit client.connect() and loop back
        print("[Reconnect] Waiting for background thread to reconnect...")
        time.sleep(3)
        print("[Reconnect] [OK] Background thread should be reconnecting now")
        
        print("[Reconnect] ========== RECONNECT SEQUENCE COMPLETE ========== \n")
        
        # Redirect back to WhatsApp page
        return redirect(url_for('whatsapp'))
    
    except Exception as e:
        print(f"[Reconnect] ERROR - Exception: {str(e)}")
        print(traceback.format_exc())
        return redirect(url_for('whatsapp'))


@app.route('/add_whitelist', methods=['POST'])
def add_whitelist():
    """Add number to whitelist"""
    config = load_config()
    number = request.json.get('number', '').strip()
    
    if number and number not in config['whitelist']:
        config['whitelist'].append(number)
        save_config(config)
        return jsonify({'status': 'success'})
    
    return jsonify({'status': 'error'}), 400


@app.route('/remove_whitelist', methods=['POST'])
def remove_whitelist():
    """Remove number from whitelist"""
    config = load_config()
    number = request.json.get('number', '').strip()
    
    if number in config['whitelist']:
        config['whitelist'].remove(number)
        save_config(config)
        return jsonify({'status': 'success'})
    
    return jsonify({'status': 'error'}), 400


@app.route('/save', methods=['POST'])
def save_settings():
    """Save settings"""
    config = load_config()
    config['email'] = request.json.get('email', '').strip()
    save_config(config)
    return jsonify({'status': 'success'})


if __name__ == '__main__':
    # Load history
    whatsapp_messages = load_history()
    
    # Start background thread
    client_thread = threading.Thread(target=run_whatsapp_client, daemon=False)
    client_thread.start()
    print("\n[MAIN] Background client thread started\n")
    
    # Run Flask on main thread
    print("[MAIN] Starting Flask server on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)

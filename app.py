import os
import psycopg2
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime

# Flask app
app = Flask(__name__)

# --- Config ---
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_PORT = os.getenv("DB_PORT", "5432")

# Cloudinary setup
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# --- Database helper ---
def get_conn():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT
    )

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        network_id VARCHAR(100) UNIQUE,
        text TEXT,
        image_url TEXT,
        updated_at TIMESTAMP DEFAULT NOW()
    );
    """)
    conn.commit()
    cur.close()
    conn.close()

init_db()

# --- Helpers ---
def get_network_id():
    ip = request.remote_addr or "unknown"
    return ".".join(ip.split(".")[:3])  # group by subnet

# --- Routes ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get', methods=['GET'])
def get_data():
    network_id = get_network_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT text, image_url FROM messages WHERE network_id = %s", (network_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify({
        "text": row[0] if row else "",
        "image": row[1] if row else None
    })

@app.route('/set', methods=['POST'])
def set_text():
    network_id = get_network_id()
    data = request.get_json()
    new_text = data.get("text", "")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (network_id, text, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (network_id) DO UPDATE
        SET text = EXCLUDED.text, updated_at = NOW()
    """, (network_id, new_text))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/upload_image', methods=['POST'])
def upload_image():
    network_id = get_network_id()
    if 'image' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(f"{network_id}_{datetime.utcnow().timestamp()}_{file.filename}")
    upload_result = cloudinary.uploader.upload(file, public_id=filename, overwrite=True)
    public_url = upload_result["secure_url"]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (network_id, image_url, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (network_id) DO UPDATE
        SET image_url = EXCLUDED.image_url, updated_at = NOW()
    """, (network_id, public_url))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "uploaded", "image": public_url})

@app.route('/delete_image', methods=['POST'])
def delete_image():
    network_id = get_network_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE messages SET image_url = NULL, updated_at = NOW()
        WHERE network_id = %s
    """, (network_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "deleted"})

# --- Run app ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

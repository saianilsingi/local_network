import os
import hashlib
import psycopg
from datetime import datetime
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader
import logging
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- Config (from env) ---
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_PORT = os.getenv("DB_PORT", "5432")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# --- DB helpers ---
def get_conn():
    return psycopg.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT
    )

def init_db():
    q = """
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        network_id VARCHAR(128) UNIQUE NOT NULL,
        text TEXT,
        image_url TEXT,
        public_id TEXT,
        owner_device_id VARCHAR(128),
        updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_network_id ON messages (network_id);
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(q)
    except Exception as e:
        logging.error("DB init error: %s", e)

init_db()

# --- Helpers: network id & headers ---
def get_public_ip():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip:
        ip = ip.split(",")[0].strip()
    return ip or "unknown"

def get_local_subnet():
    # client may send X-Local-Subnet header (e.g., "192.168.1")
    return request.headers.get("X-Local-Subnet")

def make_network_id(public_ip, local_subnet):
    # privacy: hash the hybrid key (public_ip + '|' + local_subnet)
    raw = f"{public_ip}|{local_subnet}" if local_subnet else f"{public_ip}|"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def get_network_id():
    return make_network_id(get_public_ip(), get_local_subnet())

def get_device_id():
    return request.headers.get("X-Device-ID")

def iso_or_none(dt):
    return dt.isoformat() if dt else None

# --- Routes ---

@app.route("/get", methods=["GET"])
def get_message():
    network_id = get_network_id()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT text, image_url, public_id, owner_device_id, updated_at FROM messages WHERE network_id = %s",
                (network_id,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "error": "No message found"}), 404

            text, image_url, public_id, owner_device_id, updated_at = row
            return jsonify({
                "success": True,
                "text": text,
                "image_url": image_url,
                "public_id": public_id,
                "owner_device_id": owner_device_id,
                "updated_at": iso_or_none(updated_at)
            })
    except Exception as e:
        logging.exception("DB error in /get")
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/set", methods=["POST"])
def set_text():
    network_id = get_network_id()
    device_id = get_device_id()
    try:
        payload = request.get_json(force=True)
        new_text = payload.get("text", "") if isinstance(payload, dict) else ""
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (network_id, text, owner_device_id, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (network_id) DO UPDATE
                SET text = EXCLUDED.text,
                    owner_device_id = EXCLUDED.owner_device_id,
                    updated_at = NOW()
            """, (network_id, new_text, device_id))
        return jsonify({"success": True})
    except Exception:
        logging.exception("DB error in /set")
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/upload_image", methods=["POST"])
def upload_image():
    network_id = get_network_id()
    device_id = get_device_id()

    if 'image' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"success": False, "error": "Empty filename"}), 400

    # create a safe public_id; include network hash and timestamp for uniqueness
    safe_name = secure_filename(file.filename)
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    # don't leak raw network info in public_id: use network_id hash
    pub_id = f"{network_id[:16]}_{timestamp}_{safe_name}"
    try:
        upload_result = cloudinary.uploader.upload(file, public_id=pub_id, overwrite=True)
        public_url = upload_result.get("secure_url")
        public_id = upload_result.get("public_id")
    except Exception as e:
        logging.exception("Cloudinary upload failed")
        return jsonify({"success": False, "error": "Image upload failed"}), 500

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (network_id, image_url, public_id, owner_device_id, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (network_id) DO UPDATE
                SET image_url = EXCLUDED.image_url,
                    public_id = EXCLUDED.public_id,
                    owner_device_id = EXCLUDED.owner_device_id,
                    updated_at = NOW()
            """, (network_id, public_url, public_id, device_id))
        return jsonify({"success": True, "image_url": public_url, "public_id": public_id})
    except Exception:
        logging.exception("DB error in /upload_image")
        # attempt best-effort cleanup of uploaded image to avoid orphan
        try:
            cloudinary.uploader.destroy(public_id)
        except Exception:
            pass
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/delete_image", methods=["POST"])
def delete_image():
    network_id = get_network_id()
    device_id = get_device_id()

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT public_id, owner_device_id FROM messages WHERE network_id = %s", (network_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                # nothing to delete
                cur.execute("UPDATE messages SET image_url = NULL, public_id = NULL, updated_at = NOW() WHERE network_id = %s", (network_id,))
                return jsonify({"success": True, "deleted": False})

            public_id, owner_device = row

            # If owner_device exists, enforce delete by owner only (optional)
            if owner_device and device_id and owner_device != device_id:
                return jsonify({"success": False, "error": "Only owner may delete image"}), 403

            # delete from Cloudinary (best-effort)
            try:
                cloudinary.uploader.destroy(public_id)
            except Exception as e:
                logging.warning("Cloudinary delete failed for %s: %s", public_id, e)

            # clear DB fields
            cur.execute("""
                UPDATE messages SET image_url = NULL, public_id = NULL, updated_at = NOW()
                WHERE network_id = %s
            """, (network_id,))
        return jsonify({"success": True, "deleted": True})
    except Exception:
        logging.exception("DB error in /delete_image")
        return jsonify({"success": False, "error": "Database error"}), 500

# keep a simple index if you want to serve index.html from Flask templates
@app.route("/")
def index():
    return app.send_static_file("index.html")  # or render_template if using templates/

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

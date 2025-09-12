import os
import hashlib
import logging

from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg  # psycopg3
import cloudinary
import cloudinary.uploader

# ---- Config constants ----
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIMETYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# ---- Flask app ----
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ---- Database config ----
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL not set")

# ---- Cloudinary config ----
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# ---- Database helpers ----
def get_conn():
    """Return a psycopg3 connection."""
    return psycopg.connect(DB_URL)

def init_db():
    """Create messages table and index if not exist."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                network_id TEXT UNIQUE NOT NULL,
                text TEXT,
                image_url TEXT,
                public_id TEXT,
                owner_device_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_network_updated
            ON messages (network_id, updated_at DESC);
            """)
            conn.commit()
    except Exception as e:
        logging.error(f"DB init error: {e}")

init_db()

# ---- Helpers ----
def get_network_id():
    """Generate a SHA256 hash ID based on client IP and local subnet."""
    public_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if public_ip:
        public_ip = public_ip.split(",")[0].strip()
    local_subnet = request.headers.get("X-Local-Subnet") or request.args.get("local_subnet")
    raw_id = f"{public_ip}|{local_subnet}" if local_subnet else public_ip
    return hashlib.sha256(raw_id.encode()).hexdigest()

def validate_image_file(f):
    """Check file size and type."""
    if not f:
        return False, "No file"
    if getattr(f, "content_length", 0) > MAX_IMAGE_BYTES:
        return False, "File too large"
    if f.mimetype not in ALLOWED_MIMETYPES:
        return False, f"Unsupported image type: {f.mimetype}"
    return True, None

# ---- Routes ----
@app.route("/send", methods=["POST"])
def send_message():
    text = (request.form.get("text") or "").strip()
    image_file = request.files.get("image")
    network_id = get_network_id()
    device_id = request.headers.get("X-Device-ID") or None

    if not text and not image_file:
        return jsonify({"success": False, "error": "Text or image required"}), 400

    image_url = None
    public_id = None

    try:
        with get_conn() as conn, conn.cursor() as cur:
            # Fetch old public_id
            cur.execute("SELECT public_id FROM messages WHERE network_id = %s", (network_id,))
            row = cur.fetchone()
            old_public_id = row[0] if (row and row[0]) else None

            # Handle new image
            if image_file:
                ok, err = validate_image_file(image_file)
                if not ok:
                    return jsonify({"success": False, "error": err}), 400
                try:
                    result = cloudinary.uploader.upload(image_file)
                    image_url = result.get("secure_url")
                    public_id = result.get("public_id")
                except Exception as e:
                    logging.error(f"Cloudinary upload failed: {e}")
                    return jsonify({"success": False, "error": "Image upload failed"}), 500

                if old_public_id:
                    try:
                        cloudinary.uploader.destroy(old_public_id)
                    except Exception as e:
                        logging.warning(f"Failed to destroy old Cloudinary id {old_public_id}: {e}")

            # Upsert message
            cur.execute("""
                INSERT INTO messages (network_id, text, image_url, public_id, owner_device_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (network_id)
                DO UPDATE SET text = EXCLUDED.text,
                              image_url = EXCLUDED.image_url,
                              public_id = EXCLUDED.public_id,
                              owner_device_id = EXCLUDED.owner_device_id,
                              updated_at = CURRENT_TIMESTAMP
                RETURNING text, image_url, public_id, owner_device_id, updated_at
            """, (network_id, text or None, image_url, public_id, device_id))
            saved = cur.fetchone()
            conn.commit()

        return jsonify({
            "success": True,
            "message": "Saved",
            "network_id": network_id,
            "text": saved[0],
            "image_url": saved[1],
            "public_id": saved[2],
            "owner_device_id": saved[3],
            "updated_at": saved[4]
        })
    except Exception as e:
        logging.error(f"DB error in /send: {e}")
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/get", methods=["GET"])
def get_message():
    network_id = get_network_id()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT text, image_url, public_id, owner_device_id, updated_at
                FROM messages
                WHERE network_id = %s
            """, (network_id,))
            row = cur.fetchone()
            if row:
                return jsonify({
                    "success": True,
                    "text": row[0],
                    "image_url": row[1],
                    "public_id": row[2],
                    "owner_device_id": row[3],
                    "updated_at": row[4],
                    "network_id": network_id
                })
        return jsonify({"success": False, "error": "No message found", "network_id": network_id}), 404
    except Exception as e:
        logging.error(f"DB error in /get: {e}")
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/delete", methods=["DELETE"])
def delete_message():
    network_id = get_network_id()
    device_id = request.headers.get("X-Device-ID") or None
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT public_id, owner_device_id FROM messages WHERE network_id = %s", (network_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "error": "No message to delete"}), 404

            public_id, owner_device_id = row[0], row[1]
            if owner_device_id and device_id and owner_device_id != device_id:
                return jsonify({"success": False, "error": "Not authorized"}), 403

            cur.execute("DELETE FROM messages WHERE network_id = %s", (network_id,))
            conn.commit()

            if public_id:
                try:
                    cloudinary.uploader.destroy(public_id)
                except Exception as e:
                    logging.warning(f"Cloudinary delete failed for {public_id}: {e}")

        return jsonify({"success": True, "message": "Deleted"})
    except Exception as e:
        logging.error(f"DB error in /delete: {e}")
        return jsonify({"success": False, "error": "Database error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

# ---- Run ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

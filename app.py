import os
import psycopg2
import hashlib
import logging
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import cloudinary
import cloudinary.uploader

# Config constants
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIMETYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Flask app
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)

# Database config
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL not set")

# Cloudinary config (set env vars)
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# ---- DB helpers ----
def get_conn():
    return psycopg2.connect(DB_URL)

def init_db():
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
            # index for fast lookups
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_network_updated
            ON messages (network_id, updated_at DESC);
            """)
            conn.commit()
    except Exception as e:
        logging.error(f"DB init error: {e}")

init_db()

# ---- Helper: generate network ID ----
def get_network_id():
    # Trust X-Forwarded-For header if present (common behind proxies)
    public_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if public_ip:
        public_ip = public_ip.split(",")[0].strip()
    local_subnet = request.headers.get("X-Local-Subnet") or request.args.get("local_subnet")
    raw_id = f"{public_ip}|{local_subnet}" if local_subnet else public_ip
    return hashlib.sha256(raw_id.encode()).hexdigest()

# ---- Helpers for image validation & Cloudinary ----
def validate_image_file(f):
    if not f:
        return (False, "No file")
    if hasattr(f, "content_length") and f.content_length is not None and f.content_length > MAX_IMAGE_BYTES:
        return (False, "File too large")
    # If content_length not present, read a bit (werkzeug/FileStorage may not expose)
    # We will rely on the client & Cloudinary limits but still check mimetype:
    if f.mimetype not in ALLOWED_MIMETYPES:
        return (False, f"Unsupported image type: {f.mimetype}")
    return (True, None)

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
            # fetch old public_id (but do NOT delete yet)
            cur.execute("SELECT public_id FROM messages WHERE network_id = %s", (network_id,))
            row = cur.fetchone()
            old_public_id = row[0] if (row and row[0]) else None

            # If there is a new image, validate and upload first
            if image_file:
                ok, err = validate_image_file(image_file)
                if not ok:
                    return jsonify({"success": False, "error": err}), 400
                try:
                    upload_result = cloudinary.uploader.upload(image_file)
                    image_url = upload_result.get("secure_url")
                    public_id = upload_result.get("public_id")
                except Exception as e:
                    logging.error(f"Cloudinary upload failed: {e}")
                    return jsonify({"success": False, "error": "Image upload failed"}), 500

                # At this point new image uploaded successfully; delete old image if present
                if old_public_id:
                    try:
                        cloudinary.uploader.destroy(old_public_id)
                    except Exception as e:
                        logging.warning(f"Failed to destroy old Cloudinary id {old_public_id}: {e}")

            # Upsert message: set owner_device_id to current device_id (so last-sender becomes owner)
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
            # Get current record
            cur.execute("SELECT public_id, owner_device_id FROM messages WHERE network_id = %s", (network_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "error": "No message to delete"}), 404

            public_id, owner_device_id = row[0], row[1]
            # Optional: Only allow delete if requester is the owner device (last-sender)
            if owner_device_id and device_id and owner_device_id != device_id:
                return jsonify({"success": False, "error": "Not authorized to delete (not owner)"}), 403

            # delete DB record
            cur.execute("DELETE FROM messages WHERE network_id = %s", (network_id,))
            conn.commit()

            # Attempt to delete image in Cloudinary if exists
            if public_id:
                try:
                    cloudinary.uploader.destroy(public_id)
                except Exception as e:
                    logging.warning(f"Cloudinary delete failed for {public_id}: {e}")

        return jsonify({"success": True, "message": "Deleted"})
    except Exception as e:
        logging.error(f"DB error in /delete: {e}")
        return jsonify({"success": False, "error": "Database error"}), 500

# Simple health endpoint
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

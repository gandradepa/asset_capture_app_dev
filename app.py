import os
import re
import json
import hashlib
import sqlite3
from datetime import datetime
from typing import List, Any, Dict

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_from_directory, jsonify
)
from werkzeug.utils import secure_filename

# -----------------------------------------------------------------------------
# Flask app setup
# -----------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB uploads

APP_ROOT = app.root_path
UPLOAD_DIR = os.path.join(APP_ROOT, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# DB path (override with env QR_CODES_DB_PATH if needed)
DB_PATH = os.environ.get(
    "QR_CODES_DB_PATH",
    os.path.join(APP_ROOT, "data", "QR_codes.db")
)

# -----------------------------------------------------------------------------
# Behavior after submit: success (default) or capture
# -----------------------------------------------------------------------------
def get_after_submit_mode(request_after: str | None = None) -> str:
    """
    Decide what to do after submit:
    - 'success' -> render success.html
    - 'capture' -> redirect back to capture page
    Priority: request override > env var > default('success')
    """
    if request_after:
        v = request_after.strip().lower()
        if v in ("success", "capture"):
            return v
    v = os.environ.get("AFTER_SUBMIT", "success").strip().lower()
    return v if v in ("success", "capture") else "success"

# -----------------------------------------------------------------------------
# Helpers (general)
# -----------------------------------------------------------------------------
def _safe_str(x: Any) -> str:
    try:
        return str(x).strip()
    except Exception:
        return ""

def sanitize_component(s: str, *, prefer_digits: bool = True, maxlen: int = 80) -> str:
    """
    Windows-safe filename component. If prefer_digits=True, prefer 6+ digit run
    (useful when QR contains a URL).
    """
    s = (s or "").strip()
    if prefer_digits:
        m = re.search(r'(\d{6,})', s)
        if m:
            s = m.group(1)
    s = re.sub(r'^[a-zA-Z]+:', '', s).strip('/')       # drop scheme like https:
    s = re.sub(r'[<>:"/\\|?*]+', '_', s)               # illegal chars
    s = re.sub(r'\s+', '_', s)                         # spaces -> _
    s = re.sub(r'[^A-Za-z0-9._-]+', '', s).strip('._-')
    if not s:
        s = hashlib.sha1(b'fallback').hexdigest()[:8]
    return s[:maxlen]

def map_asset_type_to_abbrev(t: str) -> str:
    t = (t or "").strip().lower()
    if t.startswith("mech"): return "ME"
    if t.startswith("elec"): return "EL"
    if t.startswith("back"): return "BF"
    return t[:2].upper() or "AS"

def seq_to_label(asset_type: str, seq: str) -> str:
    """Map the photo sequence to a friendly label."""
    t = (asset_type or "").strip().lower()
    try:
        i = int(seq)
    except Exception:
        return "Photo"
    mech_map = {0: "Asset Plate", 1: "UBC Tag", 2: "Main Asset Photo", 3: "Technical Safety BC"}
    other_map = {0: "Asset Plate", 1: "UBC Tag", 2: "Main Asset Photo"}
    return (mech_map if t == "mechanical" else other_map).get(i, f"Photo {i}")

# -----------------------------------------------------------------------------
# Image saver  (Solution A: optional Pillow with fallback)
# -----------------------------------------------------------------------------
def save_image_file(storage, dest_path: str):
    """
    Save an uploaded image to disk.
    - If Pillow is available, convert RGBA/P to RGB for JPEGs and optimize.
    - Otherwise, fall back to Werkzeug's storage.save().
    """
    try:
        # Optional dependency
        from PIL import Image
        import io as _io

        storage.stream.seek(0)
        data = storage.read()
        storage.stream.seek(0)
        ext = os.path.splitext(dest_path)[1].lower()

        try:
            img = Image.open(_io.BytesIO(data))
            if ext in (".jpg", ".jpeg"):
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(dest_path, format="JPEG", quality=90, optimize=True)
            else:
                img.save(dest_path)
        except Exception:
            # If Pillow can't decode, just write bytes.
            with open(dest_path, "wb") as f:
                f.write(data)
    except Exception:
        # Pillow not installed or other error: simple save
        storage.save(dest_path)

# -----------------------------------------------------------------------------
# SQLite helpers
# -----------------------------------------------------------------------------
def _open_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)

def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    return [row[1] for row in cur.fetchall()]

def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)",
        (table,),
    )
    return cur.fetchone() is not None

def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'

def _list_tables(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in cur.fetchall()]

def _find_assets_table(conn: sqlite3.Connection) -> str | None:
    """
    Return the exact assets table name, working with either
    'QR_code_assets' or 'QR_codes_assets' (case-insensitive).
    """
    names = _list_tables(conn)
    for candidate in ["QR_code_assets", "QR_codes_assets"]:
        for n in names:
            if n.lower() == candidate.lower():
                return n
    # heuristic fallback
    for n in names:
        l = n.lower()
        if "qr" in l and "code" in l and "asset" in l:
            return n
    return None

# -----------------------------------------------------------------------------
# Buildings (value=code, label=name)
# -----------------------------------------------------------------------------
def _load_buildings_from_sqlite() -> List[Dict[str, str]]:
    """
    Returns list like [{'code': '482', 'name': 'Abdul Ladha ...'}, ...] sorted by name.
    """
    if not os.path.exists(DB_PATH):
        return []
    conn = None
    try:
        conn = _open_db()
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        candidates = [t for t in tables if t.lower() == "buildings"] or \
                     [t for t in tables if "building" in t.lower()]
        if not candidates:
            return []
        table = candidates[0]
        cols = _table_columns(conn, table)

        code_keys = ["Code", "Building Code", "Bldg Code", "Bldg_Num", "Bldg Number", "Property", "Bldg"]
        name_keys = ["Name", "Building Name", "Building_Name", "Description", "Building"]

        code_col = next((c for c in cols for ck in code_keys
                         if c.lower().replace("_"," ") == ck.lower().replace("_"," ")), None)
        name_col = next((c for c in cols for nk in name_keys
                         if c.lower().replace("_"," ") == nk.lower().replace("_"," ")), None)

        items: List[Dict[str,str]] = []
        if code_col:
            if not name_col: name_col = code_col
            cur = conn.execute(f'SELECT {_quote_ident(code_col)}, {_quote_ident(name_col)} FROM "{table}"')
            for code, name in cur.fetchall():
                c = _safe_str(code); n = _safe_str(name)
                if c: items.append({"code": c, "name": n or c})
        else:
            cur = conn.execute(f'SELECT * FROM "{table}"')
            rows = cur.fetchall(); cols = _table_columns(conn, table)
            for r in rows:
                row = dict(zip(cols, r))
                code_guess = ""; name_guess = ""
                for v in row.values():
                    vs = _safe_str(v)
                    if vs.isdigit(): code_guess = vs; break
                for v in row.values():
                    vs = _safe_str(v)
                    if vs and not vs.isdigit(): name_guess = vs; break
                if code_guess:
                    items.append({"code": code_guess, "name": name_guess or code_guess})

        # sort by name & dedupe
        seen = set(); out = []
        for it in sorted(items, key=lambda d: d["name"].upper()):
            if it["code"] not in seen:
                seen.add(it["code"]); out.append(it)
        return out
    except Exception:
        return []
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass

def get_building_options() -> List[Dict[str, str]]:
    opts = _load_buildings_from_sqlite()
    if opts:
        return opts
    # Fallback options
    return [
        {"code": "314-1", "name": "Building 314-1"},
        {"code": "482",   "name": "Abdul Ladha Science Student Centre 482"},
        {"code": "775",   "name": "Recreation Centre North"},
    ]

# -----------------------------------------------------------------------------
# DB: QR_codes upsert, assets insert/delete
# -----------------------------------------------------------------------------
def upsert_qr_codes(conn: sqlite3.Connection, qr_code: str, building_code: str):
    """Upsert into QR_codes: set/keep Building Code for this QR."""
    table = "QR_codes"
    if not _has_table(conn, table):
        return
    cols = set(_table_columns(conn, table))
    # Exact column "QR_code_ID" as specified
    if "QR_code_ID" not in cols:
        return
    bcode_candidates = ["Building Code", "Building_Code", "Code", "Property", "Bldg Code", "Bldg"]
    bcode_col = next((c for c in bcode_candidates if c in cols), None)

    cur = conn.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "QR_code_ID"=?',
        (qr_code,)
    )
    exists = cur.fetchone()[0] > 0
    if exists:
        if bcode_col:
            conn.execute(
                f'UPDATE "{table}" SET {_quote_ident(bcode_col)}=? WHERE "QR_code_ID"=?',
                (building_code, qr_code)
            )
    else:
        if bcode_col:
            conn.execute(
                f'INSERT INTO "{table}" ("QR_code_ID", {_quote_ident(bcode_col)}) VALUES (?, ?)',
                (qr_code, building_code)
            )
        else:
            conn.execute(
                f'INSERT INTO "{table}" ("QR_code_ID") VALUES (?)',
                (qr_code,)
            )

def insert_into_assets(conn: sqlite3.Connection, file_bases: List[str]):
    """
    Insert one row per base string into assets table's (code_assets, api_int).
    - Auto-detect table name (QR_code_assets or QR_codes_assets).
    - Create UNIQUE index on code_assets if missing.
    - Use INSERT OR IGNORE to avoid duplicates.
    """
    table = _find_assets_table(conn)
    if not table:
        print("[assets] No QR_*code*_assets table found; skipping inserts.")
        return

    cols = set(_table_columns(conn, table))
    if "code_assets" not in cols:
        print(f"[assets] Table '{table}' lacks 'code_assets'; skipping inserts.")
        return

    idx_name = f"ux_{table}_code_assets"
    conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ("code_assets")')

    if "api_int" in cols:
        conn.executemany(
            f'INSERT OR IGNORE INTO "{table}" ("code_assets","api_int") VALUES (?, 0)',
            [(b,) for b in file_bases]
        )
    else:
        conn.executemany(
            f'INSERT OR IGNORE INTO "{table}" ("code_assets") VALUES (?)',
            [(b,) for b in file_bases]
        )

def delete_from_assets_by_qr(conn: sqlite3.Connection, qr_code: str):
    """
    Delete all rows for a given QR from the assets table.
    Rows are stored as 'code_assets' with pattern: "{QR} {Bldg} {Type} - {seq}"
    So we delete WHERE code_assets LIKE "<QR>%"
    """
    table = _find_assets_table(conn)
    if not table:
        return
    cols = set(_table_columns(conn, table))
    if "code_assets" not in cols:
        return
    conn.execute(f'DELETE FROM "{table}" WHERE "code_assets" LIKE ?', (qr_code + " %",))

def delete_files_by_qr(qr_code: str):
    """Remove all files on disk whose filename starts with '<QR> '."""
    prefix = f"{qr_code} "
    if not os.path.isdir(UPLOAD_DIR):
        return
    for fname in os.listdir(UPLOAD_DIR):
        if fname.startswith(prefix):
            try:
                os.remove(os.path.join(UPLOAD_DIR, fname))
            except Exception:
                pass

def qr_exists(conn: sqlite3.Connection, qr_code: str) -> bool:
    """Return True if the QR exists in QR_codes.QR_code_ID."""
    if not _has_table(conn, "QR_codes"):
        return False
    cols = set(_table_columns(conn, "QR_codes"))
    if "QR_code_ID" not in cols:
        return False
    cur = conn.execute('SELECT 1 FROM "QR_codes" WHERE "QR_code_ID"=? LIMIT 1', (qr_code,))
    return cur.fetchone() is not None

# -----------------------------------------------------------------------------
# Upload listing helper (for capture page)
# -----------------------------------------------------------------------------
ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")

def list_existing_uploads(qr_code: str, building_code: str, asset_type: str) -> List[Dict[str, str]]:
    """Return [{filename, base, url, seq, label}] for current context."""
    safe_qr   = sanitize_component(qr_code, prefer_digits=True)
    safe_bldg = sanitize_component(building_code, prefer_digits=False)
    safe_type = sanitize_component(map_asset_type_to_abbrev(asset_type), prefer_digits=False)
    prefix = f"{safe_qr} {safe_bldg} {safe_type} - "

    out = []
    if not os.path.isdir(UPLOAD_DIR):
        return out
    for fname in os.listdir(UPLOAD_DIR):
        if not fname.lower().endswith(ALLOWED_EXTS):
            continue
        if not fname.startswith(prefix):
            continue
        base, _ = os.path.splitext(fname)
        m = re.search(r'\s-\s(\d+)$', base)
        seq = m.group(1) if m else ""
        out.append({
            "filename": fname,
            "base": base,
            "url": url_for("uploaded_file", filename=fname),
            "seq": seq,
            "label": seq_to_label(asset_type, seq),
        })
    # Sort by sequence number if present
    out.sort(key=lambda x: int(x["seq"]) if x["seq"].isdigit() else 9999)
    return out

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def start():
    building_code = request.args.get("building_code", "")
    asset_type = request.args.get("asset_type", "Mechanical")
    building_options = get_building_options()  # [{code,name}], sorted by name
    return render_template(
        "start.html",
        building_options=building_options,
        building_code=building_code,
        asset_type=asset_type
    )

@app.route("/api/check-qr", methods=["GET"])
def api_check_qr():
    """
    Query param: ?qr=<value>
    Returns: { exists: bool, qr: <sanitized> }
    We sanitize to the numeric ID to match how we store in DB.
    """
    raw = _safe_str(request.args.get("qr"))
    qr = sanitize_component(raw, prefer_digits=True)
    try:
        conn = _open_db()
        exists = qr_exists(conn, qr)
        return jsonify({"exists": bool(exists), "qr": qr})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route("/capture", methods=["GET", "POST"])
def capture():
    """
    Show capture page. Supports GET (querystring) and POST (form).
    Renders any existing uploads as thumbnails with quick delete.
    """
    if request.method == "POST":
        qr_code = _safe_str(request.form.get("qr_code"))
        building_code = _safe_str(request.form.get("building_code"))
        asset_type = _safe_str(request.form.get("asset_type") or "Mechanical")
        overwrite = _safe_str(request.form.get("overwrite") or "0")
    else:
        qr_code = _safe_str(request.args.get("qr_code"))
        building_code = _safe_str(request.args.get("building_code"))
        asset_type = _safe_str(request.args.get("asset_type") or "Mechanical")
        overwrite = _safe_str(request.args.get("overwrite") or "0")

    errors = []
    if not qr_code: errors.append("QR code is required.")
    if not building_code: errors.append("Building is required.")
    if not asset_type: errors.append("Asset type is required.")

    if errors:
        for e in errors: flash(e, "warning")
        building_options = get_building_options()
        return render_template(
            "start.html",
            building_options=building_options,
            building_code=building_code,
            asset_type=asset_type
        ), 400

    existing_files = list_existing_uploads(qr_code, building_code, asset_type)

    return render_template(
        "capture.html",
        qr_code=qr_code,
        building_code=building_code,  # code only
        asset_type=asset_type,
        existing_files=existing_files,
        overwrite=overwrite
    )

@app.route("/submit", methods=["POST"])
def submit():
    """
    Save uploaded files as:
        "{QR} {BuildingCode} {TypeAbbrev} - {seq}.ext"
    and record per-image base values (no extension) into assets table.
    If overwrite=1, delete ALL previous rows & files for this QR first.
    After that, decide whether to render success or go back to capture based on AFTER_SUBMIT.
    """
    qr_code = _safe_str(request.form.get("qr_code"))
    building_code = _safe_str(request.form.get("building_code"))  # CODE ONLY
    asset_type = _safe_str(request.form.get("asset_type") or "Mechanical")
    overwrite = _safe_str(request.form.get("overwrite") or "0")
    type_abbrev = map_asset_type_to_abbrev(asset_type)

    if not qr_code or not building_code:
        flash("Missing QR code or Building code.", "warning")
        return redirect(url_for("start"))

    # Safe components for filenames/DB
    safe_qr   = sanitize_component(qr_code, prefer_digits=True)
    safe_bldg = sanitize_component(building_code, prefer_digits=False)
    safe_type = sanitize_component(type_abbrev, prefer_digits=False)

    # If overwrite requested, nuke prior data for this QR
    if overwrite == "1":
        try:
            conn = _open_db()
            delete_from_assets_by_qr(conn, safe_qr)
            conn.commit()
        except Exception as e:
            flash(f"Warning: could not clear prior DB rows for this QR ({e}).", "warning")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        delete_files_by_qr(safe_qr)

    files_saved: List[str] = []
    bases_saved: List[str] = []  # e.g., "0000085011 482 ME - 0" (no extension)

    for seq in ("0", "1", "2", "3"):
        file_key = f"image_{seq}"
        file = request.files.get(file_key)
        if not file or file.filename == "":
            continue

        orig = secure_filename(file.filename)
        _, ext = os.path.splitext(orig)
        ext = (ext or ".jpg").lower()
        if ext not in ALLOWED_EXTS:
            ext = ".jpg"

        base = f"{safe_qr} {safe_bldg} {safe_type} - {seq}"  # NO extension in DB
        fname = base + ext

        dest = os.path.join(UPLOAD_DIR, fname)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        save_image_file(file, dest)

        bases_saved.append(base)
        files_saved.append(fname)

    # --- DB writes ---
    try:
        conn = _open_db()
        # Keep/update QR → Building mapping
        upsert_qr_codes(conn, qr_code=safe_qr, building_code=building_code)
        # Insert one row per image into assets table
        if bases_saved:
            insert_into_assets(conn, bases_saved)
        conn.commit()
    except Exception as e:
        flash(f"Warning: could not write to database ({e}).", "warning")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # After submit: configurable
    mode = get_after_submit_mode(request.form.get("after_submit"))
    if mode == "capture":
        return redirect(url_for("capture",
                                qr_code=qr_code,
                                building_code=building_code,
                                asset_type=asset_type,
                                overwrite=overwrite))
    # default: success page
    return render_template(
        "success.html",
        qr_code=safe_qr,
        building_code=building_code,
        asset_type=asset_type,
        files_saved=files_saved
    )

@app.route("/delete-upload", methods=["POST"])
def delete_upload():
    """
    Delete a single uploaded file (and its DB row in assets table).
    Expects JSON: { qr_code, building_code, asset_type, filename }
    Validates the filename belongs to the given context before deleting.
    """
    data = request.get_json(silent=True) or {}
    qr_code = _safe_str(data.get("qr_code"))
    building_code = _safe_str(data.get("building_code"))
    asset_type = _safe_str(data.get("asset_type"))
    filename = os.path.basename(_safe_str(data.get("filename")))

    if not (qr_code and building_code and asset_type and filename):
        return jsonify(ok=False, error="missing parameters"), 400

    safe_qr   = sanitize_component(qr_code, prefer_digits=True)
    safe_bldg = sanitize_component(building_code, prefer_digits=False)
    safe_type = sanitize_component(map_asset_type_to_abbrev(asset_type), prefer_digits=False)
    expected_prefix = f"{safe_qr} {safe_bldg} {safe_type} - "

    # Ensure the file belongs to this context
    if not filename.startswith(expected_prefix):
        return jsonify(ok=False, error="filename not in this context"), 400

    dest = os.path.join(UPLOAD_DIR, filename)
    base, _ = os.path.splitext(filename)

    # Delete file from disk
    if os.path.exists(dest):
        try:
            os.remove(dest)
        except Exception as e:
            return jsonify(ok=False, error=f"cannot delete file: {e}"), 500

    # Delete DB row
    try:
        conn = _open_db()
        table = _find_assets_table(conn)
        if table and "code_assets" in _table_columns(conn, table):
            conn.execute(f'DELETE FROM "{table}" WHERE "code_assets"=?', (base,))
            conn.commit()
    except Exception as e:
        return jsonify(ok=False, error=f"db error: {e}"), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return jsonify(ok=True)

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("🚀 Flask app running...")
    print(f"🔗 Open your browser and go to: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)

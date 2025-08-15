import os
import io
import re
import json
import hashlib
import sqlite3
from datetime import datetime
from typing import Iterable, List, Any, Dict

from flask import (
    Flask, render_template, request, redirect, url_for, flash, send_from_directory
)
from werkzeug.utils import secure_filename
from PIL import Image

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
        if not tables:
            return []
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
# DB writes
# -----------------------------------------------------------------------------
def upsert_qr_codes(conn: sqlite3.Connection, qr_code: str, building_code: str):
    """Upsert into QR_codes: set/keep Building Code for this QR."""
    table = "QR_codes"
    if not _has_table(conn, table):
        return
    cols = set(_table_columns(conn, table))
    pk_candidates = ["QR_code_ID", "QR Code", "QR_code", "QR_ID", "QR"]
    pk_col = next((c for c in pk_candidates if c in cols), None)
    if not pk_col:
        return
    bcode_candidates = ["Building Code", "Building_Code", "Code", "Property", "Bldg Code", "Bldg"]
    bcode_col = next((c for c in bcode_candidates if c in cols), None)

    cur = conn.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE {_quote_ident(pk_col)}=?',
        (qr_code,)
    )
    exists = cur.fetchone()[0] > 0
    if exists:
        if bcode_col:
            conn.execute(
                f'UPDATE "{table}" SET {_quote_ident(bcode_col)}=? WHERE {_quote_ident(pk_col)}=?',
                (building_code, qr_code)
            )
    else:
        cols_list = [pk_col]; vals = [qr_code]
        if bcode_col:
            cols_list.append(bcode_col); vals.append(building_code)
        conn.execute(
            f'INSERT INTO "{table}" ({", ".join(_quote_ident(c) for c in cols_list)}) '
            f'VALUES ({", ".join(["?"]*len(cols_list))})',
            vals
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

# -----------------------------------------------------------------------------
# Image saver
# -----------------------------------------------------------------------------
def save_image_file(storage, dest_path: str):
    """Save upload to dest, converting RGBA→RGB for JPEGs."""
    storage.stream.seek(0)
    data = storage.read()
    storage.stream.seek(0)
    ext = os.path.splitext(dest_path)[1].lower()
    try:
        img = Image.open(io.BytesIO(data))
        if ext in (".jpg", ".jpeg"):
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(dest_path, format="JPEG", quality=90, optimize=True)
        else:
            img.save(dest_path)
    except Exception:
        with open(dest_path, "wb") as f:
            f.write(data)

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

@app.route("/capture", methods=["POST"])
def capture():
    qr_code = _safe_str(request.form.get("qr_code"))
    building_code = _safe_str(request.form.get("building_code"))  # CODE (e.g., "482")
    asset_type = _safe_str(request.form.get("asset_type") or "Mechanical")

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

    return render_template(
        "capture.html",
        qr_code=qr_code,
        building_code=building_code,  # CODE
        asset_type=asset_type
    )

@app.route("/submit", methods=["POST"])
def submit():
    """
    Save uploaded files as:
        "{QR} {BuildingCode} {TypeAbbrev} - {seq}.ext"
    and record per-image base values (no extension) into assets table.
    """
    qr_code = _safe_str(request.form.get("qr_code"))
    building_code = _safe_str(request.form.get("building_code"))  # CODE ONLY
    asset_type = _safe_str(request.form.get("asset_type") or "Mechanical")
    type_abbrev = map_asset_type_to_abbrev(asset_type)

    if not qr_code or not building_code:
        flash("Missing QR code or Building code.", "warning")
        return redirect(url_for("start"))

    # Safe components for filenames/DB
    safe_qr   = sanitize_component(qr_code, prefer_digits=True)
    safe_bldg = sanitize_component(building_code, prefer_digits=False)
    safe_type = sanitize_component(type_abbrev, prefer_digits=False)

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
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"):
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
        # Keep QR → Building mapping up to date
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

    if not files_saved:
        flash("No files were uploaded.", "warning")

    return render_template("success.html", qr_code=safe_qr, files_saved=files_saved)

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

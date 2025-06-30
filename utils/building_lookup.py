import sqlite3

SQLITE_DB_PATH = r"S:\\MaintOpsPlan\\AssetMgt\\Asset Management Process\\Database\\8. New Assets\\QR_code_project\\asset_capture_app\\data\\QR_codes.db"

def get_db_connection():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_buildings():
    buildings = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code, name FROM Buildings")
        rows = cursor.fetchall()
        buildings = [{"code": row["code"], "name": row["name"]} for row in rows]
        conn.close()
    except Exception as e:
        print("⚠️ Failed to load buildings from SQLite DB:", e)
    return buildings

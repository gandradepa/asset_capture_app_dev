import pyodbc

def get_buildings():
    ACCESS_DB_PATH = r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\QR_code_project\asset_capture_app\data\QR_codes.accdb"
    conn_str = (
        r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};"
    )
    buildings = []
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        cursor.execute("SELECT code, name FROM Buildings")
        buildings = [{"code": row[0], "name": row[1]} for row in cursor.fetchall()]
        cursor.close()
        conn.close()
    except Exception as e:
        print("⚠️ Failed to load buildings from Access DB:", e)
    return buildings

import pyodbc

access_path = ACCESS_DB_PATH = r"S:\MaintOpsPlan\AssetMgt\Asset Management Process\Database\8. New Assets\QR_code_project\asset_capture_app\data\QR_codes.accdb"

try:
    conn = pyodbc.connect(
        f"Driver={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={access_path};"
    )
    print("✅ Connection successful.")
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 Code, Name FROM Buildings")
    row = cursor.fetchone()
    print("Sample row:", row)
    cursor.close()
    conn.close()
except Exception as e:
    print("❌ Connection failed:", e)

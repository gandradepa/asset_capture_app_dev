from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from utils.file_handler import handle_upload
from utils.building_lookup import get_buildings
import pyodbc
import os

#package ready to run

app = Flask(__name__)
app.secret_key = 'ubc-qr-secret'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['SESSION_TYPE'] = 'filesystem'

ACCESS_DB_PATH = r"S:\\MaintOpsPlan\\AssetMgt\\Asset Management Process\\Database\\8. New Assets\\QR_code_project\\asset_capture_app\\data\\QR_codes.accdb"

@app.route('/')
def start():
    buildings = get_buildings()
    building_code = session.get('building_code', '')
    asset_type = session.get('asset_type', '')
    return render_template('start.html', buildings=buildings, building_code=building_code, asset_type=asset_type)

@app.route('/capture', methods=['POST'])
def capture():
    qr_code = request.form.get('qr_code')
    building_code = request.form.get('building_code')
    asset_type = request.form.get('asset_type')

    if not qr_code or qr_code.strip() == "":
        return "⚠️ QR code must be scanned and all fields completed.", 400

    # Check if QR code already exists
    exists = False
    try:
        conn_str = (
            r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
            rf"DBQ={ACCESS_DB_PATH};"
        )
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM QR_codes WHERE QR_code_ID = ?", (qr_code,))
        exists = cursor.fetchone()[0] > 0
        cursor.close()
        conn.close()
    except Exception as e:
        print("⚠️ Failed to check QR code on capture:", e)

    session['building_code'] = building_code
    session['asset_type'] = asset_type

    return render_template(
        'capture.html',
        qr_code=qr_code,
        building_code=building_code,
        asset_type=asset_type,
        qr_exists=exists
    )

@app.route('/submit', methods=['POST'])
def submit():
    data = request.form
    files = request.files
    result = handle_upload(data, files, app.config['UPLOAD_FOLDER'])

    try:
        conn_str = (
            r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
            rf"DBQ={ACCESS_DB_PATH};"
        )
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM QR_codes WHERE QR_code_ID = ?", (result['qr_code'],))
        exists = cursor.fetchone()[0] > 0
        if not exists:
            cursor.execute("INSERT INTO QR_codes (QR_code_ID) VALUES (?)", (result['qr_code'],))
            conn.commit()
        else:
            print(f"⚠️ QR code {result['qr_code']} already exists. Skipping insert.")
        cursor.close()
        conn.close()
    except Exception as e:
        print("⚠️ Failed to insert QR code:", e)

    return render_template('success.html', qr_code=result['qr_code'], files_saved=result['files_saved'])

@app.route('/check_qr_code', methods=['POST'])
def check_qr_code():
    qr_code = request.json.get('qr_code')
    exists = False
    try:
        conn_str = (
            r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
            rf"DBQ={ACCESS_DB_PATH};"
        )
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM QR_codes WHERE QR_code_ID = ?", (qr_code,))
        exists = cursor.fetchone()[0] > 0
        cursor.close()
        conn.close()
    except Exception as e:
        print("⚠️ Failed to check QR code:", e)
    return jsonify({'exists': exists})

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    print("🚀 Flask app running...")
    print("🔗 Open your browser and go to: http://127.0.0.1:5000")
    app.run(debug=True, use_reloader=False)

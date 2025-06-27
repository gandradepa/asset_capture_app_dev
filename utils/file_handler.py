import os
from werkzeug.utils import secure_filename
import re
import pyodbc

ACCESS_DB_PATH = r"S:\\MaintOpsPlan\\AssetMgt\\Asset Management Process\\Database\\8. New Assets\\QR_code_project\\asset_capture_app\\data\\QR_codes.accdb"


def handle_upload(data, files, upload_folder):
    qr_code = data.get('qr_code')
    asset_type = data.get('asset_type')
    building_code = data.get('building_code')
    files_saved = []
    filenames_no_ext = []

    if not qr_code or not asset_type or not building_code:
        return {"qr_code": qr_code or "unknown", "files_saved": []}

    for key in files:
        file = files[key]
        if file and file.filename:
            index = key.split('_')[-1]  # e.g., image_0, image_1
            filename_raw = f"{qr_code} {building_code} {asset_type[:2].upper()} - {index}.jpg"
            filename_raw = re.sub(r'\s+', ' ', filename_raw).strip()
            filename = filename_raw.replace(' ', '_') if '\\' in upload_folder else filename_raw
            save_path = os.path.join(upload_folder, filename)
            file.save(save_path)
            files_saved.append(filename)
            filenames_no_ext.append(os.path.splitext(filename)[0])

    # Save all code_assets to Access DB in QR_code_assets table, replacing old ones if they exist
    try:
        if filenames_no_ext:
            conn_str = (
                r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
                rf"DBQ={ACCESS_DB_PATH};"
            )
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()

            # Delete existing entries for this QR code prefix
            qr_prefix = filenames_no_ext[0].split(' ')[0]  # assumes QR is the first part of filename
            cursor.execute("DELETE FROM QR_code_assets WHERE code_assets LIKE ?", (qr_prefix + '%',))

            # Insert new filenames
            for asset_code in filenames_no_ext:
                cursor.execute("INSERT INTO QR_code_assets ([code_assets]) VALUES (?)", (asset_code,))
            conn.commit()
            cursor.close()
            conn.close()
    except Exception as e:
        print("⚠️ Failed to update QR_code_assets in Access DB:", e)

    return {"qr_code": qr_code, "files_saved": files_saved}

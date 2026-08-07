import os
import uuid
import sqlite3
import shutil
import json
import base64
import xml.etree.ElementTree as ET
import cairosvg
import threading
import time
from fastapi import FastAPI, HTTPException, Form, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageChops
from reportlab.pdfgen import canvas as pdf_canvas
from pypdf import PdfWriter, PdfReader

app = FastAPI(title="CardApp Image Maker Central Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "image_maker.db"

# =================================================================
# 🎯 MOBILE-FRIENDLY ABSOLUTE PATH ENGINE
# =================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REQUIRED_FOLDERS = ["fonts", "image_masks", "image_templates", "user_outputs"]
for folder in REQUIRED_FOLDERS:
    full_folder_path = os.path.join(BASE_DIR, folder)
    if not os.path.exists(full_folder_path):
        os.makedirs(full_folder_path)

app.mount("/fonts", StaticFiles(directory=os.path.join(BASE_DIR, "fonts")), name="fonts")
app.mount("/image_templates", StaticFiles(directory=os.path.join(BASE_DIR, "image_templates")), name="image_templates")
app.mount("/user_outputs", StaticFiles(directory=os.path.join(BASE_DIR, "user_outputs")), name="user_outputs")
app.mount("/outputs", StaticFiles(directory=os.path.join(BASE_DIR, "image_templates")), name="outputs")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Fonts Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS fonts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, font_name TEXT UNIQUE, file_path TEXT
    )''')
    
    # 2. Cards Table (Updated Schema)
    cursor.execute('''CREATE TABLE IF NOT EXISTS cards (
        card_id TEXT PRIMARY KEY, card_web_id TEXT, card_name TEXT, folder_name TEXT, 
        card_image TEXT, keywords TEXT, image_slots TEXT, text_slots TEXT, api_link TEXT, 
        design_canvas_id TEXT, cut_crease_canvas_id TEXT, preview_canvas_id TEXT, is_enabled INTEGER DEFAULT 1
    )''')
    
    # 3. Card Assets / Images Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS card_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT, asset_name TEXT, file_name TEXT, use_image_maker INTEGER DEFAULT 0, canvas_id TEXT,
        FOREIGN KEY(card_id) REFERENCES cards(card_id) ON DELETE CASCADE
    )''')
    
    # 4. Canvases Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS card_canvases (
        canvas_id TEXT PRIMARY KEY, card_id TEXT, canvas_name TEXT, width INTEGER, height INTEGER, background_image TEXT, category_folder TEXT DEFAULT 'root',
        output_format TEXT DEFAULT 'png'
    )''')
    
    # 5. Canvas Layers Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS canvas_layers (
        layer_id TEXT PRIMARY KEY, canvas_id TEXT, layer_type TEXT, placeholder_id TEXT,
        x_axis INTEGER, y_axis INTEGER, width INTEGER, height INTEGER, blend_mode TEXT, opacity INTEGER, mask_image TEXT,
        font_id INTEGER, font_size INTEGER, font_color TEXT, rotation INTEGER, text_align TEXT DEFAULT 'left', preview_text TEXT DEFAULT 'Sample Text'
    )''')

    # 6. Text Slot Hints Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS text_slot_hints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, hint TEXT NOT NULL
    )''')

    # Safe Schema Alters
    try: cursor.execute("ALTER TABLE cards ADD COLUMN card_web_id TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE cards ADD COLUMN card_image TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE cards ADD COLUMN keywords TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE cards ADD COLUMN image_slots TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE cards ADD COLUMN text_slots TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE cards ADD COLUMN preview_canvas_id TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE card_canvases ADD COLUMN output_format TEXT DEFAULT 'png'")
    except sqlite3.OperationalError: pass

    conn.commit()
    conn.close()

init_db()

def clean_and_save_file(file_obj, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    for old_f in os.listdir(target_dir):
        old_path = os.path.join(target_dir, old_f)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass
    with open(os.path.join(target_dir, file_obj.filename), "wb") as buffer:
        shutil.copyfileobj(file_obj.file, buffer)

# --- CARD MAKER ENDPOINTS ---

# 🎯 Instant Layer Mask Direct Upload API
@app.post("/api/upload-mask-direct")
async def upload_mask_direct(
    folder_name: str = Form(...),
    mask_file: UploadFile = File(...)
):
    try:
        card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)
        masks_dir = os.path.join(card_base_dir, "masks")
        os.makedirs(masks_dir, exist_ok=True)

        file_path = os.path.join(masks_dir, mask_file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(mask_file.file, buffer)

        relative_path = f"{folder_name}/masks/{mask_file.filename}"
        return {"status": "success", "mask_path": relative_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cards/{card_id}/folder-files")
def get_card_folder_files(card_id: str, category: str = "root"):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT folder_name FROM cards WHERE card_id = ?", (card_id,))
    card = cursor.fetchone()
    conn.close()

    if not card:
        return {"files": [], "folder_name": ""}

    folder_name = card['folder_name']
    card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)

    if category == "design":
        target_dir = os.path.join(card_base_dir, "design")
    elif category == "cut_and_crease":
        target_dir = os.path.join(card_base_dir, "cut_and_crease")
    elif category == "preview":
        target_dir = os.path.join(card_base_dir, "preview")
    else:
        target_dir = card_base_dir

    if not os.path.exists(target_dir):
        return {"files": [], "folder_name": folder_name}

    files = [f for f in os.listdir(target_dir) if os.path.isfile(os.path.join(target_dir, f)) and not f.startswith('.')]
    return {"files": files, "folder_name": folder_name}

@app.get("/api/cards")
def get_cards():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards")
    cards = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return cards

@app.get("/api/cards/{card_id}")
def get_card_detail(card_id: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    card_row = cursor.fetchone()
    if not card_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")
        
    card_dict = dict(card_row)
    folder_name = card_dict.get('folder_name', '')
    card_dir = os.path.join(BASE_DIR, "image_templates", folder_name)

    def get_first_file(sub_folder):
        target_path = os.path.join(card_dir, sub_folder)
        if os.path.exists(target_path):
            files = [f for f in os.listdir(target_path) if not f.startswith('.')]
            return files[0] if len(files) > 0 else "none"
        return "none"

    card_dict['saved_design_file'] = get_first_file("design")
    card_dict['saved_preview_file'] = get_first_file("preview")
    card_dict['saved_cut_file'] = get_first_file("cut_and_crease")

    cursor.execute("SELECT * FROM card_assets WHERE card_id = ?", (card_id,))
    assets = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"card": card_dict, "assets": assets}

@app.post("/api/cards")
async def create_card(
    card_id: str = Form(...),
    card_name: str = Form(...), 
    folder_name: str = Form(...), 
    api_link: str = Form(...), 
    assets_json: str = Form(...),
    card_web_id: str = Form(""), 
    keywords: str = Form(""), 
    image_slots: str = Form(""), 
    text_slots: str = Form(""),
    design_use_im: int = Form(...), 
    design_canvas_id: str = Form(None), 
    cut_use_im: int = Form(...), 
    cut_canvas_id: str = Form(None),
    preview_use_im: int = Form(...), 
    preview_canvas_id: str = Form(None),
    card_image_file: UploadFile = File(None),
    design_file: UploadFile = File(None), 
    cut_file: UploadFile = File(None), 
    preview_file: UploadFile = File(None),
    req_image_files: list[UploadFile] = File([])
):
    card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)
    os.makedirs(card_base_dir, exist_ok=True)
    os.makedirs(os.path.join(card_base_dir, "design"), exist_ok=True)
    os.makedirs(os.path.join(card_base_dir, "cut_and_crease"), exist_ok=True)
    os.makedirs(os.path.join(card_base_dir, "preview"), exist_ok=True)

    card_img_base64 = ""
    if card_image_file:
        contents = await card_image_file.read()
        card_img_base64 = f"data:image/jpeg;base64,{base64.b64encode(contents).decode('utf-8')}"

    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''INSERT INTO cards 
            (card_id, card_web_id, card_name, folder_name, card_image, keywords, image_slots, text_slots, api_link, 
             design_canvas_id, cut_crease_canvas_id, preview_canvas_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
            (card_id, card_web_id, card_name, folder_name, card_img_base64, keywords.lower(), image_slots, text_slots, api_link, 
             design_canvas_id if design_use_im else None, cut_canvas_id if cut_use_im else None, preview_canvas_id if preview_use_im else None))

        if design_file and not design_use_im:
            clean_and_save_file(design_file, os.path.join(card_base_dir, "design"))
        if cut_file and not cut_use_im:
            clean_and_save_file(cut_file, os.path.join(card_base_dir, "cut_and_crease"))
        if preview_file and not preview_use_im:
            clean_and_save_file(preview_file, os.path.join(card_base_dir, "preview"))

        assets_list = json.loads(assets_json)
        file_dict = {f.filename: f for f in req_image_files}
        
        for asset in assets_list:
            final_filename = "none"
            req_name = asset.get('requirement_name') or asset.get('asset_name')
            if not asset['use_image_maker'] and asset['client_filename'] in file_dict:
                f_obj = file_dict[asset['client_filename']]
                final_filename = f_obj.filename
                with open(os.path.join(card_base_dir, final_filename), "wb") as buffer:
                    shutil.copyfileobj(f_obj.file, buffer)
                    
            cursor.execute('''INSERT INTO card_assets (card_id, asset_name, file_name, use_image_maker, canvas_id) VALUES (?, ?, ?, ?, ?)''',
                (card_id, req_name, final_filename, asset['use_image_maker'], asset['canvas_id'] if asset['use_image_maker'] else None))
        conn.commit()
        return {"status": "success", "card_id": card_id}
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Card ID already exists in database registry node!")
    finally:
        conn.close()

@app.post("/api/cards/update/{card_id}")
async def update_card(
    card_id: str, 
    api_link: str = Form(...), 
    assets_json: str = Form(...),
    card_web_id: str = Form(""), 
    keywords: str = Form(""), 
    image_slots: str = Form(""), 
    text_slots: str = Form(""),
    design_use_im: int = Form(...), 
    design_canvas_id: str = Form(None), 
    cut_use_im: int = Form(...), 
    cut_canvas_id: str = Form(None),
    preview_use_im: int = Form(...), 
    preview_canvas_id: str = Form(None),
    card_image_file: UploadFile = File(None),
    design_file: UploadFile = File(None), 
    cut_file: UploadFile = File(None),
    preview_file: UploadFile = File(None),
    req_image_files: list[UploadFile] = File([])
):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT folder_name FROM cards WHERE card_id = ?", (card_id,))
    card_row = cursor.fetchone()
    if not card_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")
        
    folder_name = card_row['folder_name']
    card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)

    if card_image_file:
        contents = await card_image_file.read()
        card_img_base64 = f"data:image/jpeg;base64,{base64.b64encode(contents).decode('utf-8')}"
        cursor.execute('''UPDATE cards SET api_link = ?, card_web_id = ?, keywords = ?, image_slots = ?, text_slots = ?, card_image = ?,
                          design_canvas_id = ?, cut_crease_canvas_id = ?, preview_canvas_id = ? WHERE card_id = ?''',
                       (api_link, card_web_id, keywords.lower(), image_slots, text_slots, card_img_base64,
                        design_canvas_id if design_use_im else None, cut_canvas_id if cut_use_im else None, preview_canvas_id if preview_use_im else None, card_id))
    else:
        cursor.execute('''UPDATE cards SET api_link = ?, card_web_id = ?, keywords = ?, image_slots = ?, text_slots = ?,
                          design_canvas_id = ?, cut_crease_canvas_id = ?, preview_canvas_id = ? WHERE card_id = ?''',
                       (api_link, card_web_id, keywords.lower(), image_slots, text_slots,
                        design_canvas_id if design_use_im else None, cut_canvas_id if cut_use_im else None, preview_canvas_id if preview_use_im else None, card_id))

    if design_file and not design_use_im:
        clean_and_save_file(design_file, os.path.join(card_base_dir, "design"))
    if cut_file and not cut_use_im:
        clean_and_save_file(cut_file, os.path.join(card_base_dir, "cut_and_crease"))
    if preview_file and not preview_use_im:
        clean_and_save_file(preview_file, os.path.join(card_base_dir, "preview"))

    assets_list = json.loads(assets_json)
    file_dict = {f.filename: f for f in req_image_files}
    cursor.execute("SELECT asset_name, file_name FROM card_assets WHERE card_id = ?", (card_id,))
    old_assets = {r['asset_name']: r['file_name'] for r in cursor.fetchall()}
    cursor.execute("DELETE FROM card_assets WHERE card_id = ?", (card_id,))
    
    for asset in assets_list:
        final_filename = old_assets.get(asset['requirement_name'], "none")
        req_name = asset.get('requirement_name') or asset.get('asset_name')
        
        if not asset['use_image_maker'] and asset['client_filename'] in file_dict:
            f_obj = file_dict[asset['client_filename']]
            if final_filename and final_filename != "none":
                old_asset_path = os.path.join(card_base_dir, final_filename)
                if os.path.exists(old_asset_path):
                    try: os.remove(old_asset_path)
                    except: pass
                    
            final_filename = f_obj.filename
            with open(os.path.join(card_base_dir, final_filename), "wb") as buffer:
                shutil.copyfileobj(f_obj.file, buffer)
                
        cursor.execute('''INSERT INTO card_assets (card_id, asset_name, file_name, use_image_maker, canvas_id) VALUES (?, ?, ?, ?, ?)''',
            (card_id, req_name, final_filename, asset['use_image_maker'], asset['canvas_id'] if asset['use_image_maker'] else None))
            
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/cards/{card_id}")
def delete_card(card_id: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT folder_name FROM cards WHERE card_id = ?", (card_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found")
    folder_name = row['folder_name']
    cursor.execute("DELETE FROM card_assets WHERE card_id = ?", (card_id,))
    cursor.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
    conn.commit()
    conn.close()
    card_dir_path = os.path.join(BASE_DIR, "image_templates", folder_name)
    if os.path.exists(card_dir_path):
        shutil.rmtree(card_dir_path)
    return {"status": "success"}

# --- TEXT SLOT HINTS ENDPOINTS ---
@app.get("/api/hints")
def get_hints():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT hint FROM text_slot_hints ORDER BY id ASC")
    hints = [row[0] for row in cursor.fetchall()]
    conn.close()
    return {"hintsString": ", ".join(hints)}

@app.post("/api/hints")
async def save_hints(request: Request):
    try:
        form_data = await request.form()
        hintsString = form_data.get("hintsString", "")
    except:
        hintsString = ""
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM text_slot_hints")
    
    hints_list = [h.strip() for h in hintsString.split(',') if h.strip() != ""]
    for h in hints_list:
        cursor.execute("INSERT INTO text_slot_hints (hint) VALUES (?)", (h,))
        
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- IMAGE MAKER WORKSPACE ENDPOINTS ---

@app.get("/api/canvases")
def get_canvases():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM card_canvases")
    canvases = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return canvases

@app.get("/api/canvases/{canvas_id}")
def get_canvas_detail(canvas_id: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM card_canvases WHERE trim(canvas_id) = trim(?)", (canvas_id,))
    c_row = cursor.fetchone()
    
    layers = []
    if c_row:
        actual_db_id = c_row['canvas_id']
        cursor.execute("SELECT * FROM canvas_layers WHERE canvas_id = ?", (actual_db_id,))
        for row in cursor.fetchall():
            d = dict(row)
            if d['layer_id'].startswith(f"{actual_db_id}_"):
                d['layer_id'] = d['layer_id'][len(actual_db_id)+1:]
            layers.append(d)
        
    conn.close()
    return {"canvas": dict(c_row) if c_row else None, "layers": layers}

@app.delete("/api/canvases/{canvas_id}")
def delete_canvas(canvas_id: str):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM canvas_layers WHERE trim(canvas_id) = trim(?)", (canvas_id,))
    cursor.execute("DELETE FROM card_canvases WHERE trim(canvas_id) = trim(?)", (canvas_id,))
    
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/save-template")
async def save_template(
    request: Request,
    canvas_id: str = Form(...),
    card_id: str = Form(...),
    canvas_name: str = Form(...),
    width: int = Form(...),
    height: int = Form(...),
    layers_json: str = Form(...),
    category_folder: str = Form(...), 
    output_format: str = Form("png"),
    bg_file: UploadFile = File(None),
    existing_bg_path: str = Form("none")
):
    form_data = await request.form()
    conn = get_db()
    cursor = conn.cursor()
    try:
        final_bg_path = existing_bg_path
        folder_name = "christmas_card"
        
        if card_id and card_id != "none":
            cursor.execute("SELECT folder_name FROM cards WHERE card_id = ?", (card_id,))
            row = cursor.fetchone()
            if row:
                folder_name = row[0]

        card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)
        masks_dir = os.path.join(card_base_dir, "masks")
        os.makedirs(masks_dir, exist_ok=True)
        os.makedirs(os.path.join(card_base_dir, "outputs"), exist_ok=True)

        if card_id and card_id != "none":
            if category_folder == "design":
                target_dir = os.path.join(card_base_dir, "design")
                relative_db_path = f"{folder_name}/design"
            elif category_folder == "cut_and_crease":
                target_dir = os.path.join(card_base_dir, "cut_and_crease")
                relative_db_path = f"{folder_name}/cut_and_crease"
            else:
                target_dir = card_base_dir
                relative_db_path = folder_name
            os.makedirs(target_dir, exist_ok=True)

            if bg_file:
                with open(os.path.join(target_dir, bg_file.filename), "wb") as buffer:
                    shutil.copyfileobj(bg_file.file, buffer)
                final_bg_path = f"{relative_db_path}/{bg_file.filename}"
        else:
            if bg_file:
                target_root_dir = os.path.join(BASE_DIR, "image_templates", "christmas_card")
                os.makedirs(target_root_dir, exist_ok=True)
                with open(os.path.join(target_root_dir, bg_file.filename), "wb") as buffer:
                    shutil.copyfileobj(bg_file.file, buffer)
                final_bg_path = f"christmas_card/{bg_file.filename}"

        layers = json.loads(layers_json)
        
        cursor.execute("SELECT layer_id, mask_image FROM canvas_layers WHERE canvas_id = ?", (canvas_id,))
        old_masks = {r[0]: r[1] for r in cursor.fetchall()}
        
        cursor.execute("DELETE FROM canvas_layers WHERE canvas_id = ?", (canvas_id,))
        
        for l in layers:
            form_file_key = f"mask_file_{l['layer_id']}"
            db_layer_id = f"{canvas_id}_{l['layer_id']}"
            
            l['mask_image'] = old_masks.get(db_layer_id, "none") if l.get('mask_image') == "none" or not l.get('mask_image') else l['mask_image']
            
            if form_file_key in form_data and form_data[form_file_key] != "":
                uploaded_mask = form_data[form_file_key]
                if hasattr(uploaded_mask, 'filename') and uploaded_mask.filename != "":
                    with open(os.path.join(masks_dir, uploaded_mask.filename), "wb") as buffer:
                        shutil.copyfileobj(uploaded_mask.file, buffer)
                    l['mask_image'] = f"{folder_name}/masks/{uploaded_mask.filename}"

            cursor.execute('''INSERT OR REPLACE INTO canvas_layers (layer_id, canvas_id, layer_type, placeholder_id, x_axis, y_axis, width, height, blend_mode, opacity, mask_image, font_id, font_size, font_color, rotation, text_align, preview_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                (db_layer_id, canvas_id, l['layer_type'], l['placeholder_id'], l['x_axis'], l['y_axis'], l['width'], l['height'], l['blend_mode'], l['opacity'], l['mask_image'], l.get('font_id'), l.get('font_size'), l.get('font_color'), l.get('rotation', 0), l.get('text_align', 'left'), l.get('preview_text', 'Sample Text')))
        
        cursor.execute('''INSERT OR REPLACE INTO card_canvases (canvas_id, card_id, canvas_name, width, height, background_image, category_folder, output_format) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', 
            (canvas_id, card_id if card_id != "none" else None, canvas_name, width, height, final_bg_path, category_folder, output_format))
        
        conn.commit()
        return {"status": "success", "background_image": final_bg_path}
    except Exception as e:
        conn.rollback()
        print(f"❌ Save Template Engine Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/fonts")
def get_fonts():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM fonts")
    fonts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return fonts

@app.post("/api/fonts")
def add_font(font_name: str = Form(...), file: UploadFile = File(...)):
    file_path = f"fonts/{file.filename}"
    full_font_path = os.path.join(BASE_DIR, file_path)
    with open(full_font_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO fonts (font_name, file_path) VALUES (?, ?)", (font_name, file_path))
        conn.commit()
        return {"status": "success"}
    except:
        raise HTTPException(status_code=400, detail="Font exists")
    finally:
        conn.close()

@app.delete("/api/fonts/{font_id}")
def delete_font(font_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM fonts WHERE id = ?", (font_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- 1. DYNAMIC COMPOSITING ENGINE ---
def internal_render_canvas(canvas_id: str, form_data, output_dir: str, unique_id: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM card_canvases WHERE canvas_id = ?", (canvas_id,))
    canvas_config = cursor.fetchone()
    cursor.execute("SELECT * FROM canvas_layers WHERE canvas_id = ?", (canvas_id,))
    layers_config = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not canvas_config:
        return None

    bg_path = os.path.join(BASE_DIR, "image_templates", canvas_config['background_image'])
    main_canvas = Image.open(bg_path).convert("RGBA") if os.path.exists(bg_path) and canvas_config['background_image'] != "none" else Image.new("RGBA", (canvas_config['width'], canvas_config['height']), (255, 255, 255, 255))
    
    for layer in layers_config:
        p_id = layer['placeholder_id']
        
        if layer['layer_type'] == 'Image':
            # Specific Placeholder ID එකෙන් පමණක් File එක ගන්න (Generic 'user_image' fallback එක ඉවත් කර ඇත)
            file_obj = form_data.get(p_id) if hasattr(form_data, 'get') else None
            
            temp_img_path = os.path.join(BASE_DIR, output_dir, f"temp_{unique_id}_{p_id}.png")
            
            # User විසින් එම Slot එකට පින්තූරයක් තෝරාගෙන ඇත්නම් පමණක් Render කරන්න
            if file_obj and hasattr(file_obj, 'file') and getattr(file_obj, 'filename', ''):
                file_obj.file.seek(0)
                with open(temp_img_path, "wb") as buffer:
                    shutil.copyfileobj(file_obj.file, buffer)
                
                try:
                    u_img = Image.open(temp_img_path).convert("RGBA").resize((layer['width'], layer['height']), Image.Resampling.LANCZOS)
                except Exception:
                    # File එක open කිරීමට අපොහොසත් වුවහොත් Empty (Transparent) Image එකක් යොදන්න
                    u_img = Image.new("RGBA", (layer['width'], layer['height']), (0, 0, 0, 0))
            else:
                # පින්තූරයක් තෝරා නොමැති Slots සඳහා Empty / Transparent (R=0, G=0, B=0, Alpha=0) Canvas එකක් සාදන්න
                u_img = Image.new("RGBA", (layer['width'], layer['height']), (0, 0, 0, 0))
            
            opacity_val = layer.get('opacity', 100)
            if opacity_val < 100:
                alpha = u_img.split()[3].point(lambda p: p * (opacity_val / 100.0))
                u_img.putalpha(alpha)
                
            orig_w, orig_h = layer['width'], layer['height']
            
            mask_path = os.path.join(BASE_DIR, "image_templates", layer['mask_image'])
            if os.path.exists(mask_path) and layer['mask_image'] != "none":
                mask_img = Image.open(mask_path).convert("L").resize((orig_w, orig_h), Image.Resampling.LANCZOS)
                masked_layer = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
                masked_layer.paste(u_img, (0, 0), mask=mask_img)
                u_img = masked_layer
                
            paste_x = layer['x_axis']
            paste_y = layer['y_axis']
            
            if layer.get('rotation', 0) != 0:
                u_img = u_img.rotate(-layer['rotation'], resample=Image.Resampling.BICUBIC, expand=True)
                new_w, new_h = u_img.size
                offset_x = (new_w - orig_w) // 2
                offset_y = (new_h - orig_h) // 2
                paste_x = paste_x - offset_x
                paste_y = paste_y - offset_y
            
            layer_canvas = Image.new("RGBA", main_canvas.size, (0, 0, 0, 0))
            layer_canvas.paste(u_img, (paste_x, paste_y))
            
            b_mode = layer.get('blend_mode', 'source-over').lower()
            
            if b_mode in ('multiply', 'screen', 'overlay'):
                if b_mode == 'multiply': 
                    blended = ImageChops.multiply(main_canvas, layer_canvas)
                elif b_mode == 'screen': 
                    blended = ImageChops.screen(main_canvas, layer_canvas)
                elif b_mode == 'overlay': 
                    blended = ImageChops.hard_light(layer_canvas, main_canvas)
                
                main_canvas.paste(blended, (0, 0), mask=layer_canvas)
            else:
                main_canvas = Image.alpha_composite(main_canvas, layer_canvas)
            
            if os.path.exists(temp_img_path):
                os.remove(temp_img_path)
            
        elif layer['layer_type'] == 'Text':
            font_file = os.path.join(BASE_DIR, "fonts", "arial.ttf")
            if layer['font_id']:
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT file_path FROM fonts WHERE id = ?", (layer['font_id'],))
                res = c.fetchone()
                conn.close()
                if res:
                    font_file = os.path.join(BASE_DIR, res[0])
            
            try: font = ImageFont.truetype(font_file, layer['font_size'])
            except: font = ImageFont.load_default()
            
            if hasattr(form_data, 'get') and p_id in form_data:
                display_text = str(form_data[p_id])
            elif hasattr(form_data, 'get') and 'user_text_val' in form_data:
                display_text = str(form_data['user_text_val'])
            else:
                display_text = layer.get('preview_text', 'Sample Text')
            
            align = layer.get('text_align', 'left')
            anchor_val = "ma" if align == "center" else "ra" if align == "right" else "la"
            font_color = layer['font_color']
            
            if layer['rotation'] != 0:
                text_img = Image.new("RGBA", (canvas_config['width']*2, canvas_config['height']*2), (0,0,0,0))
                ImageDraw.Draw(text_img).text((canvas_config['width'], canvas_config['height']), display_text, fill=font_color, font=font, anchor=anchor_val)
                main_canvas.alpha_composite(text_img.rotate(-layer['rotation'], center=(canvas_config['width'], canvas_config['height']), resample=Image.Resampling.BICUBIC), dest=(layer['x_axis'] - canvas_config['width'], layer['y_axis'] - canvas_config['height']))
            else:
                ImageDraw.Draw(main_canvas).text((layer['x_axis'], layer['y_axis']), display_text, fill=font_color, font=font, anchor=anchor_val)
                
    clean_canvas_name = canvas_config['canvas_name'].lower().replace(" ", "_")
    
    raw_fmt = canvas_config['output_format'] if 'output_format' in canvas_config.keys() else 'png'
    if raw_fmt is None:
        raw_fmt = 'png'
        
    fmt = str(raw_fmt).lower().strip()
    
    filename = f"final_{clean_canvas_name}_{unique_id}.{fmt}"
    full_path = os.path.join(BASE_DIR, output_dir, filename)
    
    if fmt in ('jpg', 'jpeg'):
        import io
        ram_buffer = io.BytesIO()
        main_canvas.save(ram_buffer, format="PNG")
        ram_buffer.seek(0)
        
        flattened_png = Image.open(ram_buffer)
        jpg_canvas = Image.new("RGB", flattened_png.size, (255, 255, 255))
        jpg_canvas.paste(flattened_png, (0, 0), flattened_png)
        jpg_canvas.save(full_path, "JPEG", quality=90, optimize=True)
        ram_buffer.close()
    else:
        main_canvas.save(full_path, "PNG")
        
    return full_path

# --- 2. PRINT DESIGN PIPELINE (DYNAMIC PDF GENERATOR) ---
@app.post("/api/v1/generate-card-pdf")
async def generate_card_pdf(request: Request):
    form_data = await request.form()
    card_id = form_data.get("card_id")
    
    unique_pdf_session = str(uuid.uuid4())
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
    card = cursor.fetchone()
    
    if not card:
        conn.close()
        raise HTTPException(status_code=404, detail="Card infrastructure not found")
        
    folder_name = card['folder_name']
    design_canvas_id = card['design_canvas_id']
    cut_canvas_id = card['cut_crease_canvas_id']

    card_base_dir = os.path.join(BASE_DIR, "image_templates", folder_name)
    outputs_path = os.path.join(card_base_dir, "outputs")
    os.makedirs(outputs_path, exist_ok=True)

    def find_target_file(sub_folder):
        target_path = os.path.join(card_base_dir, sub_folder)
        if os.path.exists(target_path):
            files = [f for f in os.listdir(target_path) if not f.startswith('.')]
            if len(files) > 0:
                return os.path.join(target_path, files[0])
        return None

    design_template_path = find_target_file("design")
    cut_template_path = find_target_file("cut_and_crease")

    design_final_path = None
    is_svg_design = False

    # PAGE 1: DESIGN LAYOUT PRIORITY SELECTION
    if design_template_path and design_template_path.endswith('.svg'):
        is_svg_design = True
        svg_output_path = os.path.join(outputs_path, f"dynamic_design_{unique_pdf_session[:8]}.svg")
        
        try:
            tree = ET.parse(design_template_path)
            root = tree.getroot()
            
            ET.register_namespace('', "http://www.w3.org/2000/svg")
            ET.register_namespace('xlink', "http://www.w3.org/1999/xlink")
            
            for img_element in root.findall('.//{http://www.w3.org/2000/svg}image'):
                slot_id = img_element.get('id')
                if slot_id:
                    matched_key = None
                    for form_key in form_data.keys():
                        if form_key in slot_id:
                            matched_key = form_key
                            break
                            
                    user_image_file = form_data.get(matched_key) if matched_key else None
                    if not user_image_file: 
                        user_image_file = form_data.get(slot_id)
                    
                    # 🛑 මෙතැන තිබූ `form_data.get("user_image")` fallback කොටස ඉවත් කර ඇත.
                    
                    if user_image_file and hasattr(user_image_file, 'file') and getattr(user_image_file, 'filename', ''):
                        temp_img_path = os.path.join(outputs_path, f"temp_{slot_id}_{unique_pdf_session[:8]}.png")
                        compressed_img_path = os.path.join(outputs_path, f"comp_{slot_id}_{unique_pdf_session[:8]}.jpg")
                        
                        user_image_file.file.seek(0)
                        with open(temp_img_path, "wb") as buffer:
                            shutil.copyfileobj(user_image_file.file, buffer)
                        
                        try:
                            with Image.open(temp_img_path) as img:
                                if img.width > 600:
                                    aspect_ratio = img.height / img.width
                                    new_width = 600
                                    new_height = int(new_width * aspect_ratio)
                                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                
                                img.convert("RGB").save(compressed_img_path, "JPEG", quality=70, optimize=True)
                            
                            with open(compressed_img_path, "rb") as img_file:
                                encoded_string = base64.b64encode(img_file.read()).decode('utf-8')
                            base64_href = f"data:image/jpeg;base64,{encoded_string}"
                            
                            img_element.set('{http://www.w3.org/1999/xlink}href', base64_href)
                            img_element.set('href', base64_href)
                            
                        except Exception as img_err:
                            print(f"[Compression Error]: {str(img_err)}")
                        finally:
                            if os.path.exists(temp_img_path): os.remove(temp_img_path)
                            if os.path.exists(compressed_img_path): os.remove(compressed_img_path)
                    else:
                        # 💡 Image එකක් තෝරාගෙන නැති නම්, SVG tag එකේ href/xlink:href හිස් කර දමන්න
                        img_element.set('{http://www.w3.org/1999/xlink}href', '')
                        img_element.set('href', '')
            
            cursor_font = get_db().cursor()
            for text_element in root.findall('.//{http://www.w3.org/2000/svg}text'):
                text_slot_id = text_element.get('id')
                svg_font = text_element.get("font-family", "").replace("'", "").replace('"', "")
                
                cursor_font.execute("SELECT file_path FROM fonts WHERE font_name = ?", (svg_font,))
                font_res = cursor_font.fetchone()
                
                if font_res:
                    full_font_path = os.path.join(BASE_DIR, font_res[0]).replace("\\", "/")
                    font_url = f"file:///{full_font_path}"
                    text_element.set("style", f"font-family: '{svg_font}'; src: url('{font_url}');")
                
                if text_slot_id:
                    matched_text_key = None
                    for form_key in form_data.keys():
                        if form_key in text_slot_id:
                            matched_text_key = form_key
                            break
                    if matched_text_key:
                        raw_txt = str(form_data[matched_text_key])
                        text_element.text = raw_txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    elif text_slot_id == "text_01" and "user_text_val" in form_data:
                        raw_txt = str(form_data["user_text_val"])
                        text_element.text = raw_txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            cursor_font.close()
            tree.write(svg_output_path, encoding='utf-8', xml_declaration=True)
            design_final_path = svg_output_path
        except Exception as e:
            print(f"[SVG Automation Error]: {str(e)}")
            design_final_path = design_template_path

    elif design_canvas_id and str(design_canvas_id).strip() != "":
        try:
            print(f"[ImageMaker] Rendering Design Layout via Canvas ({design_canvas_id})...")
            output_dir_rel = f"image_templates/{folder_name}/outputs"
            design_final_path = internal_render_canvas(design_canvas_id, form_data, output_dir_rel, unique_pdf_session)
        except Exception as render_err:
            print(f"[ERROR] ImageMaker Render Failed for Design Layout: {str(render_err)}")
            design_final_path = design_template_path

    if not design_final_path:
        design_final_path = design_template_path

    if not design_final_path or not os.path.exists(design_final_path):
        conn.close()
        raise HTTPException(status_code=400, detail="Design file or ImageMaker Canvas not found for Design Layout")

    # PAGE 2: CUT AND CREASE PRIORITY SELECTION
    cut_final_path = None
    is_svg_cut = False

    if cut_template_path and cut_template_path.endswith('.svg'):
        is_svg_cut = True
        cut_final_path = cut_template_path
    elif cut_canvas_id and str(cut_canvas_id).strip() != "":
        try:
            print(f"[ImageMaker] Rendering Cut & Crease via Canvas ({cut_canvas_id})...")
            output_dir_rel = f"image_templates/{folder_name}/outputs"
            cut_final_path = internal_render_canvas(cut_canvas_id, form_data, output_dir_rel, unique_pdf_session)
        except Exception as render_err:
            print(f"[ERROR] ImageMaker Render Failed for Cut/Crease: {str(render_err)}")
            cut_final_path = cut_template_path
    elif cut_template_path:
        cut_final_path = cut_template_path

    conn.close()

    # PDF PAGE RENDERING AND MERGING
    pdf_filename = f"print_ready_{card_id}_{unique_pdf_session[:8]}.pdf"
    pdf_output_path = os.path.join(outputs_path, pdf_filename)

    try:
        page1_pdf_path = os.path.join(outputs_path, f"p1_{unique_pdf_session[:8]}.pdf")
        if is_svg_design:
            cairosvg.svg2pdf(url=design_final_path, write_to=page1_pdf_path, background_color='white')
            if os.path.exists(svg_output_path): os.remove(svg_output_path)
        else:
            img = Image.open(design_final_path)
            p_pdf = pdf_canvas.Canvas(page1_pdf_path, pagesize=(img.width, img.height))
            p_pdf.drawImage(design_final_path, 0, 0, width=img.width, height=img.height)
            p_pdf.save()

        page2_pdf_path = os.path.join(outputs_path, f"p2_{unique_pdf_session[:8]}.pdf")
        if cut_final_path and os.path.exists(cut_final_path):
            if is_svg_cut:
                cairosvg.svg2pdf(url=cut_final_path, write_to=page2_pdf_path, background_color='white')
            else:
                img = Image.open(cut_final_path)
                p_pdf = pdf_canvas.Canvas(page2_pdf_path, pagesize=(img.width, img.height))
                p_pdf.drawImage(cut_final_path, 0, 0, width=img.width, height=img.height)
                p_pdf.save()

        writer = PdfWriter()
        reader1 = PdfReader(page1_pdf_path)
        writer.add_page(reader1.pages[0])
        
        if cut_final_path and os.path.exists(page2_pdf_path):
            reader2 = PdfReader(page2_pdf_path)
            writer.add_page(reader2.pages[0])
        
        with open(pdf_output_path, "wb") as f:
            writer.write(f)
            
        if os.path.exists(page1_pdf_path): os.remove(page1_pdf_path)
        if os.path.exists(page2_pdf_path): os.remove(page2_pdf_path)
        
        return {"status": "success", "download_url": f"/image_templates/{folder_name}/outputs/{pdf_filename}"}
    except Exception as e:
        print(f"[ERROR] PDF Engine Final Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"PDF Generation Failed: {str(e)}")

# --- 3. LIVE WEB PREVIEW RENDER ENGINE ---
@app.post("/api/v1/render-user-card")
async def render_user_card(request: Request):
    form_data = await request.form()
    canvas_id = form_data.get("canvas_id")
    unique_session_id = str(uuid.uuid4())
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM card_canvases WHERE trim(canvas_id) = trim(?)", (canvas_id,))
    canvas_config = cursor.fetchone()
    
    if not canvas_config:
        conn.close()
        raise HTTPException(status_code=404, detail="Canvas Template not found")

    output_dir = "user_outputs"
    download_prefix = "/user_outputs"
    
    if canvas_config['card_id']:
        cursor.execute("SELECT folder_name FROM cards WHERE trim(card_id) = trim(?)", (canvas_config['card_id'],))
        card_row = cursor.fetchone()
        if card_row:
            folder_name = card_row['folder_name']
            output_dir = f"image_templates/{folder_name}/outputs"
            download_prefix = f"/image_templates/{folder_name}/outputs"
            
            os.makedirs(os.path.join(BASE_DIR, output_dir), exist_ok=True)

    try:
        final_path = internal_render_canvas(canvas_config['canvas_id'], form_data, output_dir, unique_session_id)
        conn.close()
        if not final_path:
            raise HTTPException(status_code=500, detail="Render engine failed to produce composite")
            
        filename = os.path.basename(final_path)
        return {"status": "success", "download_url": f"{download_prefix}/{filename}"}
    except Exception as e:
        if conn: conn.close()
        raise HTTPException(status_code=500, detail=str(e))

# =================================================================
# ⏰ AUTOMATIC FILE CLEANUP ENGINE FOR IMAGE_TEMPLATES OUTPUTS
# =================================================================
def auto_cleanup_outputs_engine():
    """විනාඩි 10කට සැරයක් image_templates sub-folders වල outputs ඇතුළේ ඇති විනාඩි 30ට වඩා පරණ files මකා දමයි."""
    while True:
        try:
            templates_dir = os.path.join(BASE_DIR, "image_templates")
            max_age_seconds = 30 * 60  # විනාඩි 30 (තත්පර වලින්)
            current_time = time.time()

            if os.path.exists(templates_dir):
                # image_templates ඇතුළේ ඇති සියලුම Card Folders පරීක්ෂා කිරීම
                for card_folder in os.listdir(templates_dir):
                    card_folder_path = os.path.join(templates_dir, card_folder)
                    
                    if os.path.isdir(card_folder_path):
                        outputs_path = os.path.join(card_folder_path, "outputs")
                        
                        # Outputs ෆෝල්ඩරයක් ඇත්නම් එහි ඇති පරණ ෆයිල්ස් මකා දැමීම
                        if os.path.exists(outputs_path) and os.path.isdir(outputs_path):
                            for file_name in os.listdir(outputs_path):
                                file_path = os.path.join(outputs_path, file_name)
                                
                                if os.path.isfile(file_path):
                                    file_age = current_time - os.path.getmtime(file_path)
                                    if file_age > max_age_seconds:
                                        try:
                                            os.remove(file_path)
                                            print(f"🗑️ [Python Cleanup] Auto-Cleared File: {file_path}")
                                        except Exception as del_err:
                                            print(f"❌ [Python Cleanup] Failed to delete {file_path}: {del_err}")
        except Exception as e:
            print(f"❌ [Python Cleanup Engine Error]: {str(e)}")

        # විනාඩි 10ක් (තත්පර 600ක්) රැඳී සිට නැවත රන් වීම
        time.sleep(600)

# FastAPI Server එක Start වන විටම Background Thread එකක් ලෙස Cleanup Engine එක Run කිරීම
cleanup_thread = threading.Thread(target=auto_cleanup_outputs_engine, daemon=True)
cleanup_thread.start()
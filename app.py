import io
import os
import re
import sqlite3
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from PIL import Image
import pytesseract
from pytesseract import Output
import cv2
from pdf2image import convert_from_bytes
from gtts import gTTS
from googletrans import Translator  # ✅ for translation

# ========== CONFIG ==========
DB_PATH = "data.db"
UPLOADS_DIR = "uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024  # 40MB limit

# Uncomment and set your Tesseract path if needed (for Windows):
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ========== Normal Ranges ==========
NORMAL_RANGES = {
    'hemoglobin': (13.5, 17.5, 'g/dL', 'Hemoglobin'),
    'glucose_fasting': (70, 99, 'mg/dL', 'Fasting sugar'),
    'glucose_pp': (70, 140, 'mg/dL', 'Post-prandial sugar'),
    'hba1c': (4.0, 5.6, '%', 'HbA1c'),
    'cholesterol_total': (0, 200, 'mg/dL', 'Total cholesterol'),
    'hdl': (40, 60, 'mg/dL', 'HDL'),
    'ldl': (0, 100, 'mg/dL', 'LDL'),
    'triglycerides': (0, 150, 'mg/dL', 'Triglycerides'),
    'creatinine': (0.6, 1.3, 'mg/dL', 'Creatinine'),
    'urea': (10, 50, 'mg/dL', 'Urea'),
}

SYNONYMS = {
    'hemoglobin': ['hemoglobin', 'hb '],
    'glucose_fasting': ['fasting blood sugar', 'fbs'],
    'glucose_pp': ['post prandial', 'pp sugar'],
    'hba1c': ['hba1c', 'a1c'],
    'cholesterol_total': ['total cholesterol', 'cholesterol total'],
    'hdl': ['hdl'],
    'ldl': ['ldl'],
    'triglycerides': ['triglycerides', 'tg'],
    'creatinine': ['creatinine'],
    'urea': ['urea', 'bun'],
}

NUM_RE = re.compile(r'([<>]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)')

# ========== Helper Functions ==========
def to_float(tok):
    try:
        n = re.sub(r'[<>%]', '', tok).strip()
        return float(n)
    except:
        return None

def find_numbers_in_text(s):
    return [m.group(1).replace(',', '').strip() for m in NUM_RE.finditer(s)]

def preprocess_image(pil_img):
    import numpy as np
    arr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 3)
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, 10)
    return Image.fromarray(th)

def extract_data(file_bytes, filename):
    if filename.lower().endswith('.pdf'):
        pages = convert_from_bytes(file_bytes, dpi=200)
        imgs = pages
    else:
        imgs = [Image.open(io.BytesIO(file_bytes)).convert('RGB')]

    text_all = []
    found = {}

    for pil in imgs:
        processed = preprocess_image(pil)
        text = pytesseract.image_to_string(processed, lang='eng')
        text_all.append(text.lower())

    combined = "\n".join(text_all)

    for key, synonyms in SYNONYMS.items():
        for s in synonyms:
            pattern = rf"{s}[:\s]*([<>]?\s*\d+(?:\.\d+)?)"
            match = re.search(pattern, combined)
            if match:
                val = to_float(match.group(1))
                if val is not None:
                    found[key] = val
                    break
    return found, combined

# ========== ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    f = request.files.get('report')
    if not f:
        return "No file uploaded", 400

    file_bytes = f.read()
    filename = f.filename or f"report_{datetime.utcnow().timestamp()}.pdf"
    extracted, full_text = extract_data(file_bytes, filename)

    summary = {}
    overall_attention = False

    for k, v in extracted.items():
        low, high, unit, label = NORMAL_RANGES.get(k, (None, None, '', k))
        status = 'unknown'
        if low is not None and high is not None:
            status = 'normal' if (v >= low and v <= high) else 'attention'
            if status == 'attention':
                overall_attention = True
        summary[k] = {'value': v, 'low': low, 'high': high, 'unit': unit, 'status': status, 'label': label}

    overall = 'attention' if overall_attention else 'normal'

    # --- AI natural summary ---
    summary_lines = []
    if overall == 'normal':
        summary_lines.append("✅ All values appear normal. You seem to be in good health.")
    else:
        summary_lines.append("⚠️ Some results need attention. Please review the following points carefully.")

    ADVICE = {
        'hemoglobin': "Low hemoglobin may indicate anemia. Eat iron-rich foods and consult your doctor.",
        'glucose_fasting': "High fasting sugar could indicate diabetes risk. Monitor your diet.",
        'hba1c': "High HbA1c shows elevated average blood sugar. Consult your doctor.",
        'cholesterol_total': "High cholesterol may increase heart disease risk.",
        'hdl': "Low HDL means good cholesterol is low; exercise more.",
        'ldl': "High LDL increases heart risk; reduce saturated fats.",
        'triglycerides': "High triglycerides indicate excess fats; avoid sugary foods.",
        'creatinine': "Abnormal creatinine may mean kidney issues.",
        'urea': "Abnormal urea suggests kidney or dehydration issues.",
    }

    for k, v in summary.items():
        val = v['value']
        unit = v['unit']
        status = v['status']
        name = v['label']

        if status == 'normal':
            summary_lines.append(f"Your {name.lower()} is {val} {unit}, which is within the healthy range.")
        elif status == 'attention':
            advice = ADVICE.get(k, "Please consult your doctor.")
            summary_lines.append(f"{name} is {val} {unit}, which is outside normal range. {advice}")
        else:
            summary_lines.append(f"{name} value is {val} {unit}.")

    summary_text = " ".join(summary_lines)

    return render_template('result.html',
                           summary=summary,
                           summary_text=summary_text,
                           overall_status=overall)

# ========== Text-to-Speech with Hindi Translation ==========
@app.route('/speak', methods=['POST'])
def speak_text():
    text = request.form.get('text', '')
    lang = request.form.get('lang', 'en')
    if not text.strip():
        return jsonify({'ok': False, 'error': 'Empty text'})

    os.makedirs('static/audio', exist_ok=True)
    try:
        # Translate English to Hindi if requested
        if lang == 'hi':
            translator = Translator()
            translated = translator.translate(text, src='en', dest='hi')
            text = translated.text

        # Generate speech
        tts = gTTS(text=text, lang=lang)
        filename = f"static/audio/report_{lang}.mp3"
        tts.save(filename)
        return jsonify({'ok': True, 'url': '/' + filename})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

# ========== RUN APP ==========
if __name__ == '__main__':
    app.run(debug=True)

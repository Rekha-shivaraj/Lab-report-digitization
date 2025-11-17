import io
import os
import re
import sqlite3
import json
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from PIL import Image
import pytesseract
from pdf2image.pdf2image import convert_from_bytes
from gtts import gTTS

# ================== CONFIG ==================
app = Flask(__name__, static_folder="static")
CORS(app)

os.makedirs("static/audio", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)

# Tesseract Path
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Database configuration
DATABASE = 'reports.db'

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the database"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Create reports table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            upload_date TEXT NOT NULL,
            report_type TEXT,
            extracted_text TEXT
        )
    ''')
    
    # Create test_values table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL,
            status TEXT NOT NULL,
            interpretation TEXT,
            FOREIGN KEY (report_id) REFERENCES reports (id)
        )
    ''')
    
    # Create findings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            finding TEXT NOT NULL,
            FOREIGN KEY (report_id) REFERENCES reports (id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()


# ================== STATIC FILE SERVING ==================
@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


# ================== HOME ==================
@app.route("/")
def home():
    return render_template('index.html')

@app.route("/reports")
def reports_list():
    """Display all saved reports"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM reports ORDER BY upload_date DESC')
    reports = cursor.fetchall()
    conn.close()
    return render_template('reports.html', reports=reports)

@app.route("/report/<int:report_id>")
def report_detail(report_id):
    """Display detailed view of a specific report"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get report data
    cursor.execute('SELECT * FROM reports WHERE id = ?', (report_id,))
    report = cursor.fetchone()
    
    if report:
        # Get test values for this report
        cursor.execute('SELECT * FROM test_values WHERE report_id = ?', (report_id,))
        values = cursor.fetchall()
        
        # Get findings for this report
        cursor.execute('SELECT * FROM findings WHERE report_id = ?', (report_id,))
        findings = cursor.fetchall()
        
        conn.close()
        return render_template('report_detail.html', report=report, values=values, findings=findings)
    
    conn.close()
    return render_template('report_detail.html', report=None)


# ================== OCR + ANALYSIS ==================
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        file = request.files.get("report")
        if not file:
            return render_template('result.html', error="No file uploaded")

        filename = file.filename
        file_bytes = file.read()

        # PDF or image OCR
        if filename and filename.lower().endswith(".pdf"):
            pages = convert_from_bytes(file_bytes)
            text = "\n".join([pytesseract.image_to_string(p, lang="eng") for p in pages])
        else:
            img = Image.open(io.BytesIO(file_bytes))
            text = pytesseract.image_to_string(img, lang="eng")

        analysis_results = analyze_text(text)
        report_type = detect_report_type(text)
        
        # Prepare chart data
        chart_data = {
            'labels': [],
            'values': [],
            'colors': []
        }
        
        for name, data in analysis_results['values'].items():
            chart_data['labels'].append(name)
            chart_data['values'].append(data['value'])
            
            status = analysis_results['interpretations'][name]['status']
            if status == 'Normal':
                chart_data['colors'].append('rgba(16, 185, 129, 0.8)')
            elif status == 'High':
                chart_data['colors'].append('rgba(239, 68, 68, 0.8)')
            else:
                chart_data['colors'].append('rgba(245, 158, 11, 0.8)')
        
        # Save to database
        conn = get_db()
        cursor = conn.cursor()
        
        # Insert report
        cursor.execute('''
            INSERT INTO reports (filename, upload_date, report_type, extracted_text)
            VALUES (?, ?, ?, ?)
        ''', (filename, datetime.now().isoformat(), report_type, text))
        
        report_id = cursor.lastrowid
        
        # Insert test values
        for test_name, data in analysis_results['values'].items():
            interpretation = analysis_results['interpretations'][test_name]
            cursor.execute('''
                INSERT INTO test_values (report_id, test_name, value, unit, status, interpretation)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (report_id, test_name, data['value'], data['unit'], 
                  interpretation['status'], interpretation['interpretation']))
        
        # Insert findings
        for finding in analysis_results['findings']:
            cursor.execute('''
                INSERT INTO findings (report_id, finding)
                VALUES (?, ?)
            ''', (report_id, finding))
        
        conn.commit()
        conn.close()

        return render_template('result.html', 
                           values=analysis_results['values'],
                           interpretations=analysis_results['interpretations'],
                           findings=analysis_results['findings'],
                           report_type=report_type,
                           report_id=report_id,
                           chart_data=chart_data)

    except Exception as e:
        print("❌ ERROR:", e)
        return render_template('result.html', error=str(e))


# ================== TEXT-TO-SPEECH ==================
@app.route("/speak", methods=["POST"])
def speak():
    try:
        text = request.form.get("text", "")
        lang = request.form.get("lang", "en")

        if not text:
            return jsonify({"ok": False, "error": "Empty text"})

        filename = f"report_{lang}.mp3"
        output_path = os.path.join("static/audio", filename)

        # Generate speech
        tts = gTTS(text=text, lang=lang)
        tts.save(output_path)

        # Full accessible URL
        file_url = f"http://127.0.0.1:5000/static/audio/{filename}"

        return jsonify({"ok": True, "url": file_url})

    except Exception as e:
        print("❌ SPEAK ERROR:", e)
        return jsonify({"ok": False, "error": str(e)})


# ================== HELPERS ==================
def get_preventive_measures(test_name, status, value):
    """Get preventive measures and medical advice based on test results"""
    measures = []
    
    if test_name == 'Hemoglobin':
        if status == 'Low':
            measures.append("⚠️ CONSULT A DOCTOR: Low hemoglobin requires medical attention.")
            measures.append("• Eat iron-rich foods: red meat, spinach, lentils, fortified cereals")
            measures.append("• Include vitamin C to enhance iron absorption")
            measures.append("• Consider iron supplements after consulting your doctor")
        elif status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: High hemoglobin needs medical evaluation.")
            measures.append("• Stay well hydrated")
            measures.append("• Avoid smoking")
            measures.append("• Get regular medical checkups")
    
    elif test_name == 'Cholesterol':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: High cholesterol significantly increases heart disease risk.")
            measures.append("• Reduce saturated fats and trans fats in your diet")
            measures.append("• Increase fiber intake: oats, beans, fruits, vegetables")
            measures.append("• Exercise regularly (at least 30 minutes daily)")
            measures.append("• Maintain healthy weight")
            measures.append("• Avoid fried and processed foods")
        else:
            measures.append("• Continue maintaining a healthy diet")
            measures.append("• Regular exercise to keep cholesterol in check")
    
    elif test_name == 'Glucose':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR IMMEDIATELY: High glucose may indicate diabetes or pre-diabetes.")
            measures.append("• Reduce sugar and refined carbohydrate intake")
            measures.append("• Eat more whole grains, vegetables, and lean proteins")
            measures.append("• Exercise regularly to improve insulin sensitivity")
            measures.append("• Monitor blood sugar levels as advised by doctor")
            measures.append("• Maintain healthy body weight")
        else:
            measures.append("• Maintain balanced diet with controlled carbohydrate portions")
            measures.append("• Regular physical activity")
    
    elif test_name == 'Creatinine':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: Elevated creatinine indicates potential kidney issues.")
            measures.append("• Stay well hydrated (drink adequate water)")
            measures.append("• Limit protein intake as advised by doctor")
            measures.append("• Avoid nephrotoxic medications without medical advice")
            measures.append("• Control blood pressure and blood sugar")
        else:
            measures.append("• Stay hydrated")
            measures.append("• Maintain healthy kidney function with balanced diet")
    
    elif test_name == 'Urea':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: High urea levels need medical evaluation.")
            measures.append("• Increase water intake")
            measures.append("• Moderate protein consumption")
            measures.append("• Avoid dehydration")
        else:
            measures.append("• Continue balanced protein intake")
            measures.append("• Stay adequately hydrated")
    
    elif test_name == 'HDL':
        if status == 'Low':
            measures.append("⚠️ CONSULT A DOCTOR: Low HDL increases cardiovascular risk.")
            measures.append("• Regular aerobic exercise (raises HDL)")
            measures.append("• Include healthy fats: olive oil, nuts, fatty fish")
            measures.append("• Quit smoking if applicable")
            measures.append("• Maintain healthy weight")
        else:
            measures.append("• Maintain your current healthy lifestyle")
            measures.append("• Continue regular exercise")
    
    elif test_name == 'LDL':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: High LDL significantly increases heart disease risk.")
            measures.append("• Reduce saturated fat intake")
            measures.append("• Increase soluble fiber in diet")
            measures.append("• Regular cardiovascular exercise")
            measures.append("• Consider plant stanols/sterols")
            measures.append("• Medication may be needed - consult doctor")
        else:
            measures.append("• Keep up the good work with heart-healthy diet")
            measures.append("• Continue regular exercise")
    
    elif test_name == 'Triglycerides':
        if status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: Elevated triglycerides increase heart disease risk.")
            measures.append("• Limit sugar and refined carbohydrates")
            measures.append("• Reduce alcohol consumption")
            measures.append("• Eat omega-3 rich fish (salmon, mackerel)")
            measures.append("• Regular physical activity")
            measures.append("• Lose weight if overweight")
        else:
            measures.append("• Maintain low sugar diet")
            measures.append("• Continue healthy eating habits")
    
    elif test_name == 'Sodium':
        if status == 'Low' or status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR IMMEDIATELY: Abnormal sodium levels can be serious.")
            if status == 'Low':
                measures.append("• Medical treatment is needed for low sodium")
            else:
                measures.append("• Reduce salt intake")
                measures.append("• Increase water consumption")
    
    elif test_name == 'Potassium':
        if status == 'Low' or status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR IMMEDIATELY: Abnormal potassium affects heart function.")
            if status == 'Low':
                measures.append("• Eat potassium-rich foods: bananas, oranges, potatoes")
            else:
                measures.append("• Limit high-potassium foods")
                measures.append("• Medical management required")
    
    elif test_name in ['Platelets', 'WBC', 'RBC']:
        if status == 'Low' or status == 'High':
            measures.append(f"⚠️ CONSULT A DOCTOR: Abnormal {test_name} count requires medical evaluation.")
            measures.append("• Blood disorders need professional diagnosis and treatment")
            measures.append("• Follow up with complete blood count (CBC) tests")
    
    # ECG-specific measures
    elif test_name == 'Heart Rate':
        if status == 'Low':
            measures.append("⚠️ CONSULT A DOCTOR: Low heart rate (bradycardia) needs evaluation.")
            measures.append("• May indicate heart conduction issues")
            measures.append("• Avoid sudden strenuous activities without medical clearance")
        elif status == 'High':
            measures.append("⚠️ CONSULT A DOCTOR: High heart rate (tachycardia) needs evaluation.")
            measures.append("• Reduce caffeine and stimulant intake")
            measures.append("• Practice stress management techniques")
            measures.append("• Avoid excessive alcohol")
        else:
            measures.append("• Maintain regular cardiovascular exercise")
            measures.append("• Monitor heart rate during physical activities")
    
    elif test_name in ['PR Interval', 'QRS Duration', 'QT Interval', 'QTc Interval']:
        if status == 'Low' or status == 'High':
            measures.append(f"⚠️ CONSULT A CARDIOLOGIST IMMEDIATELY: Abnormal {test_name} may indicate serious heart rhythm issues.")
            measures.append("• Cardiac conduction abnormalities require specialist evaluation")
            measures.append("• May need further cardiac testing (Holter monitor, stress test)")
            measures.append("• Avoid medications that affect heart rhythm without doctor approval")
            measures.append("• Immediate medical attention if experiencing chest pain, dizziness, or palpitations")
        else:
            measures.append("• Maintain heart health with regular checkups")
            measures.append("• Monitor any new cardiac symptoms")
    
    # EEG-specific measures
    elif test_name in ['Alpha Wave', 'Beta Wave', 'Theta Wave', 'Delta Wave']:
        if status == 'Low' or status == 'High':
            measures.append(f"⚠️ CONSULT A NEUROLOGIST: Abnormal {test_name} patterns require specialist evaluation.")
            measures.append("• Brain wave abnormalities may indicate neurological conditions")
            measures.append("• May need additional neurological testing")
            measures.append("• Ensure adequate sleep (7-8 hours daily)")
            measures.append("• Reduce stress and practice relaxation techniques")
            measures.append("• Avoid seizure triggers if applicable (flashing lights, sleep deprivation)")
            measures.append("• Do not drive or operate heavy machinery if advised by doctor")
        else:
            measures.append("• Maintain healthy sleep patterns")
            measures.append("• Continue stress management practices")
            measures.append("• Regular brain health activities (reading, puzzles)")
    
    return measures

def detect_report_type(text):
    t = text.lower()
    if any(k in t for k in ["eeg", "electroencephalogram", "brain wave", "epilepsy", "seizure"]):
        return "EEG Report"
    if any(k in t for k in ["mri", "hippocampi", "ventricles"]):
        return "MRI Report"
    if any(k in t for k in ["ecg", "ekg", "electrocardiogram", "qrs", "t wave", "p wave", "heart rate", "rhythm"]):
        return "ECG Report"
    return "Lab Report"


def get_detailed_interpretation(test_name, value, status, low_normal, high_normal, unit):
    """Provide detailed medical interpretation explaining the issue and analysis needed"""
    
    interpretations = {
        'Hemoglobin': {
            'Low': f"Your Hemoglobin is {value} {unit}, which is below the normal range ({low_normal}-{high_normal} {unit}). Low hemoglobin (Anemia) means your blood has fewer red blood cells to carry oxygen throughout your body. This can cause fatigue, weakness, pale skin, and shortness of breath. Possible causes include iron deficiency, vitamin B12/folate deficiency, chronic diseases, or blood loss. Analysis needed: Complete Blood Count (CBC), Iron studies, Vitamin B12 and Folate levels, Stool test for occult blood.",
            'High': f"Your Hemoglobin is {value} {unit}, which is above the normal range ({low_normal}-{high_normal} {unit}). High hemoglobin (Polycythemia) means your blood has too many red blood cells, making it thicker. This increases risk of blood clots, stroke, and heart problems. Causes may include dehydration, lung disease, heart disease, or bone marrow disorders. Analysis needed: Complete Blood Count, EPO levels, Blood gas analysis, Bone marrow biopsy if needed."
        },
        'Cholesterol': {
            'High': f"Your Total Cholesterol is {value} {unit}, which is above the healthy limit of {high_normal} {unit}. High cholesterol leads to plaque buildup in arteries (atherosclerosis), significantly increasing risk of heart attack and stroke. The excess cholesterol deposits on artery walls, narrowing blood vessels. Analysis needed: Lipid profile (HDL, LDL, Triglycerides), Cardiovascular risk assessment, Liver function tests, Thyroid function tests."
        },
        'Glucose': {
            'High': f"Your Blood Glucose is {value} {unit}, which exceeds the normal limit of {high_normal} {unit}. Elevated glucose indicates pre-diabetes or diabetes, where your body cannot properly regulate blood sugar. Chronic high glucose damages blood vessels, nerves, kidneys, eyes, and increases infection risk. Analysis needed: HbA1c test (3-month average), Fasting glucose test, Oral Glucose Tolerance Test (OGTT), Insulin levels, Kidney function tests."
        },
        'Creatinine': {
            'High': f"Your Creatinine is {value} {unit}, above the normal range ({low_normal}-{high_normal} {unit}). Creatinine is a waste product filtered by kidneys. High levels indicate reduced kidney function (Chronic Kidney Disease). When kidneys don't work properly, waste builds up in blood, causing serious health issues. Analysis needed: eGFR calculation, Urea/BUN test, Urinalysis, Kidney ultrasound, Electrolyte panel."
        },
        'Urea': {
            'High': f"Your Urea is {value} {unit}, above the normal range ({low_normal}-{high_normal} {unit}). Urea is a waste product from protein breakdown, filtered by kidneys. High urea indicates kidney stress, dehydration, or excessive protein breakdown. This can lead to uremia with symptoms like nausea, confusion, and fatigue. Analysis needed: Creatinine levels, eGFR, Urinalysis, Electrolyte panel, Kidney imaging."
        },
        'HDL': {
            'Low': f"Your HDL (Good Cholesterol) is {value} {unit}, below the protective level of {low_normal} {unit}. HDL removes harmful cholesterol from arteries and transports it to liver for disposal. Low HDL fails to protect against heart disease and increases cardiovascular risk. Analysis needed: Complete lipid profile, Cardiovascular risk assessment, Lifestyle evaluation, Liver function tests."
        },
        'LDL': {
            'High': f"Your LDL (Bad Cholesterol) is {value} {unit}, above the safe limit of {high_normal} {unit}. LDL deposits cholesterol on artery walls, forming plaques that narrow arteries (atherosclerosis). This significantly increases risk of heart attack, stroke, and peripheral artery disease. Analysis needed: Complete lipid profile, Cardiac risk markers (CRP, Lipoprotein-a), Carotid ultrasound, Cardiac stress test if symptomatic."
        },
        'Triglycerides': {
            'High': f"Your Triglycerides are {value} {unit}, above the healthy limit of {high_normal} {unit}. Triglycerides are fat molecules in blood. High levels increase risk of pancreatitis, heart disease, and metabolic syndrome. They often indicate poor diet, obesity, or diabetes. Analysis needed: Lipid profile, Fasting glucose, HbA1c, Liver function tests, Pancreatic enzyme levels."
        },
        'Sodium': {
            'Low': f"Your Sodium is {value} {unit}, below the normal range ({low_normal}-{high_normal} {unit}). Low sodium (Hyponatremia) causes cells to swell, particularly dangerous for brain cells. Symptoms include confusion, seizures, coma. Causes include excessive water intake, kidney problems, heart failure, or hormonal imbalances. Analysis needed: Serum and urine osmolality, Kidney function tests, Thyroid and adrenal hormone tests.",
            'High': f"Your Sodium is {value} {unit}, above the normal range ({low_normal}-{high_normal} {unit}). High sodium (Hypernatremia) causes severe dehydration and cell shrinkage. Can lead to confusion, seizures, coma, and death if severe. Causes include dehydration, diabetes insipidus, or excessive salt intake. Analysis needed: Serum and urine osmolality, Kidney function tests, Diabetes insipidus workup."
        },
        'Potassium': {
            'Low': f"Your Potassium is {value} {unit}, below the normal range ({low_normal}-{high_normal} {unit}). Low potassium (Hypokalemia) affects heart rhythm and muscle function, potentially causing dangerous arrhythmias, muscle weakness, and paralysis. Causes include diuretics, vomiting, diarrhea, or kidney disease. Analysis needed: ECG, Kidney function tests, Magnesium levels, Aldosterone levels, Urine potassium.",
            'High': f"Your Potassium is {value} {unit}, above the normal range ({low_normal}-{high_normal} {unit}). High potassium (Hyperkalemia) is life-threatening, causing dangerous heart arrhythmias and cardiac arrest. Symptoms include muscle weakness, palpitations. Causes include kidney failure, medications, or excessive supplementation. Analysis needed: URGENT ECG, Kidney function tests, Medication review, Arterial blood gas."
        },
        'Heart Rate': {
            'Low': f"Your Heart Rate is {value} {unit}, below the normal range ({low_normal}-{high_normal} {unit}). Bradycardia (slow heart rate) may indicate heart conduction problems, medication effects, or athletic conditioning. If symptomatic, it can cause dizziness, fatigue, fainting. Analysis needed: ECG, Holter monitor (24-hour ECG), Echocardiogram, Thyroid function tests, Electrolyte panel.",
            'High': f"Your Heart Rate is {value} {unit}, above the normal range ({low_normal}-{high_normal} {unit}). Tachycardia (fast heart rate) increases heart workload and may indicate stress, anxiety, fever, anemia, thyroid problems, or heart disease. Persistent tachycardia can lead to heart failure. Analysis needed: ECG, Holter monitor, Echocardiogram, Thyroid function tests, Electrolyte panel, Stress test."
        },
        'PR Interval': {
            'Low': f"Your PR Interval is {value} {unit}, shorter than normal ({low_normal}-{high_normal} {unit}). Short PR interval may indicate pre-excitation syndromes (like WPW syndrome) where electrical signals bypass normal pathways, potentially causing rapid dangerous arrhythmias. Analysis needed: Detailed ECG analysis, Electrophysiology study, Echocardiogram, Holter monitoring.",
            'High': f"Your PR Interval is {value} {unit}, longer than normal ({low_normal}-{high_normal} {unit}). Prolonged PR interval indicates first-degree heart block where electrical signals are delayed. This can progress to complete heart block requiring pacemaker. Analysis needed: Serial ECGs, Holter monitor, Echocardiogram, Electrolyte panel, Consider pacemaker evaluation."
        },
        'QRS Duration': {
            'Low': f"Your QRS Duration is {value} {unit}, shorter than typical ({low_normal}-{high_normal} {unit}). While usually not concerning, unusually short QRS may occur with certain conditions. Analysis needed: Comprehensive ECG review, Echocardiogram, Electrolyte assessment.",
            'High': f"Your QRS Duration is {value} {unit}, longer than normal ({low_normal}-{high_normal} {unit}). Wide QRS indicates delayed ventricular conduction (bundle branch block) or ventricular origin of beats. This reduces heart efficiency and may indicate heart disease or cardiomyopathy. Analysis needed: Echocardiogram, Cardiac MRI, Holter monitor, Electrophysiology study, Heart failure workup."
        },
        'QT Interval': {
            'Low': f"Your QT Interval is {value} {unit}, shorter than normal ({low_normal}-{high_normal} {unit}). Short QT syndrome is rare but increases risk of dangerous arrhythmias and sudden cardiac death. May be genetic. Analysis needed: QTc calculation, Genetic testing, Family screening, Electrophysiology study, ICD consideration.",
            'High': f"Your QT Interval is {value} {unit}, longer than normal ({low_normal}-{high_normal} {unit}). Long QT syndrome increases risk of Torsades de Pointes (life-threatening arrhythmia) and sudden cardiac death. Can be congenital or medication-induced. Analysis needed: QTc calculation, Medication review, Electrolyte panel (especially K+, Mg2+, Ca2+), Genetic testing, Family history evaluation."
        },
        'QTc Interval': {
            'Low': f"Your Corrected QT (QTc) is {value} {unit}, shorter than normal ({low_normal}-{high_normal} {unit}). Short QTc syndrome increases sudden cardiac death risk through ventricular fibrillation. Often genetic. Analysis needed: Genetic testing for cardiac channelopathies, Family screening, Exercise stress test, Electrophysiology study.",
            'High': f"Your Corrected QT (QTc) is {value} {unit}, longer than normal ({low_normal}-{high_normal} {unit}). Prolonged QTc is HIGH RISK for Torsades de Pointes and sudden cardiac arrest. Immediate intervention may be needed. Analysis needed: URGENT medication review, Electrolyte correction (K+, Mg2+), Serial ECGs, Consider ICD placement, Genetic counseling."
        },
        'Alpha Wave': {
            'Low': f"Your Alpha Wave activity is {value} {unit}, below normal ({low_normal}-{high_normal} {unit}). Reduced alpha waves may indicate anxiety, stress, depression, or neurological conditions affecting relaxation and calm states. Analysis needed: Complete neurological examination, Sleep study, Psychological evaluation, MRI brain if indicated.",
            'High': f"Your Alpha Wave activity is {value} {unit}, above normal ({low_normal}-{high_normal} {unit}). Excessive alpha waves may occur with certain medications, drowsiness disorders, or neurological conditions. Analysis needed: Detailed EEG review, Sleep study, Medication review, Neurological examination."
        },
        'Beta Wave': {
            'Low': f"Your Beta Wave activity is {value} {unit}, below normal ({low_normal}-{high_normal} {unit}). Reduced beta waves may indicate depression, attention disorders, or reduced alertness and cognitive function. Analysis needed: Neurological examination, Cognitive assessment, Sleep study, Thyroid function tests.",
            'High': f"Your Beta Wave activity is {value} {unit}, above normal ({low_normal}-{high_normal} {unit}). Excessive beta waves often indicate anxiety, stress, excessive mental activity, or stimulant use. Can affect sleep quality. Analysis needed: Anxiety assessment, Sleep study, Medication/stimulant review, Stress evaluation."
        },
        'Theta Wave': {
            'Low': f"Your Theta Wave activity is {value} {unit}, below normal ({low_normal}-{high_normal} {unit}). Low theta waves may affect memory consolidation and emotional processing. Analysis needed: Cognitive testing, Memory assessment, Sleep quality evaluation, Neurological examination.",
            'High': f"Your Theta Wave activity is {value} {unit}, above normal ({low_normal}-{high_normal} {unit}). Excessive theta waves in awake state may indicate drowsiness disorders, attention deficit, head injury, or degenerative brain conditions. Analysis needed: Sleep study, Cognitive assessment, MRI brain, ADHD evaluation if applicable."
        },
        'Delta Wave': {
            'Low': f"Your Delta Wave activity is {value} {unit}, below normal ({low_normal}-{high_normal} {unit}). Reduced delta waves may indicate poor sleep quality or sleep disorders affecting deep restorative sleep. Analysis needed: Sleep study (polysomnography), Sleep disorder evaluation, Sleep hygiene assessment.",
            'High': f"Your Delta Wave activity is {value} {unit}, above normal ({low_normal}-{high_normal} {unit}). Excessive delta waves in awake state can indicate brain injury, tumors, metabolic disorders, or deep sleep intrusion into wakefulness. Analysis needed: MRI brain, Metabolic panel, Sleep study, Neurological examination."
        },
    }
    
    # Return specific interpretation or generic message
    if test_name in interpretations and status in interpretations[test_name]:
        return interpretations[test_name][status]
    else:
        return f"{test_name} is {status.lower()} at {value} {unit} (normal: {low_normal}-{high_normal} {unit}). Further medical evaluation recommended."


def analyze_text(text):
    # Preprocess text to handle common OCR issues
    t = text.lower()
    # Fix common OCR errors
    t = re.sub(r'[\|lI]l', 'll', t)  # Double 'l' often misread
    t = re.sub(r'[\|lI]t', 'tt', t)  # Double 't' often misread
    t = re.sub(r'[O0]', 'o', t)      # 'O' and '0' confusion
    t = re.sub(r'\s+', ' ', t)      # Normalize whitespace
    t = re.sub(r'\n+', '\n', t)    # Normalize line breaks
    
    results = {
        'values': {},
        'findings': [],
        'interpretations': {}
    }

    def extract_number(pattern):
        match = re.search(pattern, t, re.IGNORECASE)
        if match:
            value = match.group(1)
            try:
                # Handle common OCR artifacts
                value = re.sub(r'[^0-9.]', '', value)  # Remove non-numeric characters
                if value.count('.') > 1:
                    # If multiple dots, keep only the first one
                    parts = value.split('.')
                    value = parts[0] + '.' + ''.join(parts[1:])
                return float(value)
            except:
                return None
        return None

    # Extract all possible values with their reference ranges
    # Enhanced patterns to match real-world variations
    values_data = [
        # Lab Report Tests
        ('Hemoglobin', r"(?:hemoglobin|hb|hgb)[:\s]*([0-9.]+)\s*(?:g/dl|g/dL|gm/dl|gm/dL)?", 12, 17, 'g/dL'),
        ('Cholesterol', r"(?:cholesterol|chol)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 200, 'mg/dL'),
        ('Glucose', r"(?:glucose|sugar|blood sugar)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 140, 'mg/dL'),
        ('Urea', r"(?:urea|blood urea)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 50, 'mg/dL'),
        ('Creatinine', r"(?:creatinine|creat)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 1.3, 'mg/dL'),
        ('HDL', r"(?:hdl|high density lipoprotein)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 40, float('inf'), 'mg/dL'),
        ('LDL', r"(?:ldl|low density lipoprotein)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 130, 'mg/dL'),
        ('Triglycerides', r"(?:triglycerides|trig)[:\s]*([0-9.]+)\s*(?:mg/dl|mg/dL)?", 0, 150, 'mg/dL'),
        ('Sodium', r"(?:sodium|na)[:\s]*([0-9.]+)\s*(?:mmol/l|mmol/L)?", 135, 145, 'mmol/L'),
        ('Potassium', r"(?:potassium|k)[:\s]*([0-9.]+)\s*(?:mmol/l|mmol/L)?", 3.5, 5.1, 'mmol/L'),
        ('Platelets', r"(?:platelets|plt)[:\s]*([0-9.]+)\s*(?:th/?l|th/?L|thousand/?l|thousand/?L)?", 150, 450, 'th/L'),
        ('WBC', r"(?:wbc|white blood cell count)[:\s]*([0-9.]+)\s*(?:th/?l|th/?L|thousand/?l|thousand/?L)?", 4, 11, 'th/L'),
        ('RBC', r"(?:rbc|red blood cell count)[:\s]*([0-9.]+)\s*(?:mil/?l|mil/?L|million/?l|million/?L)?", 4.5, 5.9, 'mil/L'),
        ('Hematocrit', r"(?:hematocrit|hct)[:\s]*([0-9.]+)\s*(?:%|percent)?", 38, 50, '%'),
        
        # ECG Report Tests
        ('Heart Rate', r"(?:heart rate|hr|ventricular rate)[:\s]*([0-9.]+)\s*(?:bpm|beats)?", 60, 100, 'bpm'),
        ('PR Interval', r"(?:pr interval|pr)[:\s]*([0-9.]+)\s*(?:ms|msec)?", 120, 200, 'ms'),
        ('QRS Duration', r"(?:qrs duration|qrs)[:\s]*([0-9.]+)\s*(?:ms|msec)?", 80, 120, 'ms'),
        ('QT Interval', r"(?:qt interval|qt)[:\s]*([0-9.]+)\s*(?:ms|msec)?", 350, 450, 'ms'),
        ('QTc Interval', r"(?:qtc interval|qtc|corrected qt)[:\s]*([0-9.]+)\s*(?:ms|msec)?", 350, 460, 'ms'),
        
        # EEG Report Tests
        ('Alpha Wave', r"(?:alpha wave|alpha rhythm|alpha)[:\s]*([0-9.]+)\s*(?:hz)?", 8, 13, 'Hz'),
        ('Beta Wave', r"(?:beta wave|beta rhythm|beta)[:\s]*([0-9.]+)\s*(?:hz)?", 13, 30, 'Hz'),
        ('Theta Wave', r"(?:theta wave|theta rhythm|theta)[:\s]*([0-9.]+)\s*(?:hz)?", 4, 8, 'Hz'),
        ('Delta Wave', r"(?:delta wave|delta rhythm|delta)[:\s]*([0-9.]+)\s*(?:hz)?", 0.5, 4, 'Hz'),
    ]

    # Process each value
    for name, pattern, low_normal, high_normal, unit in values_data:
        value = extract_number(pattern)
        if value is not None:
            # Validate reasonable ranges to avoid OCR artifacts
            if (name in ['Hemoglobin', 'Cholesterol', 'Glucose', 'Urea', 'Creatinine', 'HDL', 'LDL', 'Triglycerides'] and 
                (value < 0 or value > 1000)):
                # Skip unrealistic values
                continue
            elif (name in ['Sodium', 'Potassium'] and (value < 1 or value > 200)):
                # Skip unrealistic electrolyte values
                continue
            elif (name in ['Platelets', 'WBC', 'RBC'] and (value < 0 or value > 10000)):
                # Skip unrealistic blood count values
                continue
            elif (name in ['Heart Rate'] and (value < 20 or value > 250)):
                # Skip unrealistic heart rate values
                continue
            elif (name in ['PR Interval', 'QRS Duration', 'QT Interval', 'QTc Interval'] and (value < 50 or value > 1000)):
                # Skip unrealistic ECG interval values
                continue
            elif (name in ['Alpha Wave', 'Beta Wave', 'Theta Wave', 'Delta Wave'] and (value < 0 or value > 100)):
                # Skip unrealistic EEG wave values
                continue
            
            results['values'][name] = {
                'value': value,
                'unit': unit,
                'low_normal': low_normal,
                'high_normal': high_normal
            }
            
            # Determine status and create detailed interpretation
            if value < low_normal:
                status = 'Low'
                interpretation = get_detailed_interpretation(name, value, 'Low', low_normal, high_normal, unit)
            elif value > high_normal and high_normal != float('inf'):
                status = 'High'
                interpretation = get_detailed_interpretation(name, value, 'High', low_normal, high_normal, unit)
            elif value > high_normal and high_normal == float('inf'):
                status = 'Normal'
                interpretation = f"{name} ({value} {unit}) is within the healthy range (>{low_normal} {unit}). This is a healthy result."
            else:
                status = 'Normal'
                interpretation = f"{name} is within the normal range ({low_normal}-{high_normal} {unit}). This is a healthy result."
            
            results['interpretations'][name] = {
                'status': status,
                'interpretation': interpretation,
                'preventive_measures': get_preventive_measures(name, status, value)
            }
        else:
            # Check for partial matches with more flexible patterns
            flexible_pattern = r"(?:^|\s|\b)" + name.lower() + r".*?([0-9.]+)"
            flexible_match = re.search(flexible_pattern, t, re.IGNORECASE | re.DOTALL)
            if flexible_match:
                try:
                    value = float(flexible_match.group(1))
                    results['findings'].append(f"{name}: Possible value {value} found (requires verification).")
                except:
                    results['findings'].append(f"{name} was mentioned in the report but no clear numeric value was identified.")
            elif name.lower() in t:
                results['findings'].append(f"{name} was mentioned in the report but no numeric value was found.")

    # If nothing found
    if not results['values'] and not results['findings']:
        results['findings'].append("No numeric values detected. Your report text was read but exact numbers were not found.")

    return results


# ================== RUN SERVER ==================
if __name__ == "__main__":
    app.run(debug=True, port=5000)

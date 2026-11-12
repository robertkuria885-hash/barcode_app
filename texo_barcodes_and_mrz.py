import os
import io
import base64
import re
import requests
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import barcode
from barcode.writer import ImageWriter
from pdf417 import encode, render_image
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
import qrcode

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-super-secret-key-here')

if app.config['SECRET_KEY'] == 'your-super-secret-key-here' and os.getenv('FLASK_ENV') == 'production':
    raise RuntimeError("CRITICAL SECURITY ERROR: Default SECRET_KEY detected in production environment!")

# Use absolute path for SQLite to prevent file loss depending on working directory execution context
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(basedir, "texo_app.db")}?timeout=20'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# PRODUCTION SECURITY: Auto-enable secure cookies if environment is production or using HTTPS
IS_PRODUCTION = os.getenv('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SECURE'] = IS_PRODUCTION
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

Talisman(app, content_security_policy={
    'default-src': ["'self'", "https://cdn.jsdelivr.net"],
    'script-src': ["'self'", "https://cdn.jsdelivr.net", "'unsafe-inline'"],
    'style-src': ["'self'", "https://cdn.jsdelivr.net", "'unsafe-inline'"],
    'img-src': ["'self'", "data:"]
})

# --- EXACT TRUST WALLET TRON ADDRESSES ---
WALLET_1 = "TKLndVfZEAKJxTwKc34cF7ZA5T6jg7Auf"
WALLET_2 = "TGh8z3hJRASL52pcDLw9YPmfmGNQdCkKWv"
WALLET_3 = "TUVEjB3qKhuEmVYJM3ouVjP6qsB6WMwL8P"
FINAL_MASTER_WALLET_4 = "TLxAgxKq4WkktpMTtyRqt65iMFKTIGfqT"

ROTATING_WALLETS = [WALLET_1, WALLET_2, WALLET_3]

# USDT TRC20 Contract Address on Tron Mainnet
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

STATE_IINS = {
    'AL': '636033', 'AK': '636059', 'AZ': '636026', 'AR': '636021',
    'CA': '636014', 'CO': '636020', 'CT': '636006', 'DE': '636011',
    'DC': '636043', 'FL': '636010', 'GA': '636055', 'HI': '636047',
    'ID': '636050', 'IL': '636035', 'IN': '636037', 'IA': '636018',
    'KS': '636022', 'KY': '636046', 'LA': '636007', 'ME': '636041',
    'MD': '636003', 'MA': '636002', 'MI': '636032', 'MN': '636038',
    'MS': '636051', 'MO': '636030', 'MT': '636008', 'NE': '636054',
    'NV': '636049', 'NH': '636039', 'NJ': '636036', 'NM': '636009',
    'NY': '636001', 'NC': '636004', 'ND': '636034', 'OH': '636023',
    'OK': '636058', 'OR': '636029', 'PA': '636025', 'RI': '636052',
    'SC': '636005', 'SD': '636042', 'TN': '636053', 'TX': '636015',
    'UT': '636040', 'VT': '636024', 'VA': '636000', 'WA': '636045',
    'WV': '636061', 'WI': '636031', 'WY': '636060', 'AS': '604427',
    'GU': '636019', 'MP': '604430', 'PR': '604431', 'VI': '636062',
    'US': '636027'
}

CANADA_PROVINCE_IINS = {
    'Ontario': '636012', 'Quebec': '604428', 'British Columbia': '636028',
    'Alberta': '604432', 'Manitoba': '636048', 'Saskatchewan': '636044',
    'Nova Scotia': '636013', 'New Brunswick': '636017', 'Newfoundland and Labrador': '636016',
    'Prince Edward Island': '604426', 'Northwest Territories': '604434',
    'Nunavut': '604433', 'Yukon': '604429'
}

# --- DATABASE MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    credits = db.Column(db.Integer, default=0)
    is_admin = db.Column(db.Boolean, default=False)

class TransactionHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    receipt_number = db.Column(db.String(100), unique=True, nullable=False)
    user_id = db.Column(db.Integer, nullable=True)  # Nullable to capture pre-signup payments
    temp_session_id = db.Column(db.String(100), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class ActiveCheckoutSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, unique=True, nullable=True)
    temp_session_id = db.Column(db.String(100), unique=True, nullable=True)
    assigned_wallet = db.Column(db.String(100), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- SANITIZATION HELPER ---
def sanitize_input(val):
    if not val:
        return ""
    return re.sub(r'[^a-zA-Z0-9\s\-\<\./]', '', str(val))

# --- HELPER: BASE58 TO HEX CONVERSION FOR STRICT WALLET VERIFICATION ---
def base58_to_hex(address):
    """Decodes a TRON Base58 address into hex format for payload comparison."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not address or not address.startswith('T'):
        return ""
    
    # Simple base58 decoding
    n = 0
    for char in address:
        if char not in alphabet:
            return ""
        n = n * 58 + alphabet.index(char)
    
    combined = n.to_bytes(25, 'big')
    # Strip leading version byte and trailing 4-byte checksum
    raw_payload = combined[1:-4]
    return raw_payload.hex().lower()

# --- AUTH ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if request.method == 'POST':
        username = sanitize_input(request.form.get('username'))
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            try:
                # Claim any pre-signup unassigned credits tied to visitor session cookie
                temp_id = session.get('visitor_session_id')
                if temp_id:
                    pending_txs = TransactionHistory.query.filter_by(temp_session_id=temp_id, user_id=None).all()
                    for tx in pending_txs:
                        tx.user_id = user.id
                        user.credits += int(tx.amount)
                    db.session.commit()
            except Exception:
                db.session.rollback()
            return redirect(url_for('barcode_page'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def register():
    if request.method == 'POST':
        username = sanitize_input(request.form.get('username'))
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        
        try:
            hashed_password = generate_password_hash(password, method='scrypt')
            new_user = User(username=username, password=hashed_password, credits=0)
            db.session.add(new_user)
            db.session.commit()
            
            # Claim pre-signup credits if any paid prior to registration
            temp_id = session.get('visitor_session_id')
            if temp_id:
                pending_txs = TransactionHistory.query.filter_by(temp_session_id=temp_id, user_id=None).all()
                for tx in pending_txs:
                    tx.user_id = new_user.id
                    new_user.credits += int(tx.amount)
                db.session.commit()
            
            login_user(new_user)
        except Exception:
            db.session.rollback()
            flash('Registration error occurred. Please try again.', 'danger')
            return redirect(url_for('register'))

        return redirect(url_for('barcode_page'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- MAIN APP ROUTES ---
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('barcode_page'))
    return redirect(url_for('login'))

@app.route('/barcode')
@login_required
def barcode_page():
    return render_template('barcode.html')

@app.route('/mrz')
@login_required
def mrz_page():
    return render_template('mrz.html')

@app.route('/checkout')
def checkout_page():
    now = datetime.utcnow()
    
    # Track guest users via persistent session cookie so they retain credits if they pay before registering
    if not current_user.is_authenticated and not session.get('visitor_session_id'):
        import uuid
        session['visitor_session_id'] = str(uuid.uuid4())
    
    identifier_filter = {'user_id': current_user.id} if current_user.is_authenticated else {'temp_session_id': session.get('visitor_session_id')}
    session_record = ActiveCheckoutSession.query.filter_by(**identifier_filter).first()
    
    if not session_record or session_record.expires_at < now:
        seed_val = current_user.id if current_user.is_authenticated else hash(session.get('visitor_session_id'))
        index = (abs(seed_val) + int(now.timestamp())) % len(ROTATING_WALLETS)
        assigned_wallet = ROTATING_WALLETS[index]
        expiry_time = now + timedelta(minutes=15)
        
        try:
            if session_record:
                session_record.assigned_wallet = assigned_wallet
                session_record.expires_at = expiry_time
            else:
                session_record = ActiveCheckoutSession(
                    user_id=current_user.id if current_user.is_authenticated else None,
                    temp_session_id=None if current_user.is_authenticated else session.get('visitor_session_id'),
                    assigned_wallet=assigned_wallet,
                    expires_at=expiry_time
                )
                db.session.add(session_record)
            db.session.commit()
        except Exception:
            db.session.rollback()
    else:
        assigned_wallet = session_record.assigned_wallet

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(assigned_wallet)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

    remaining_seconds = int((session_record.expires_at - now).total_seconds())

    return render_template(
        'checkout.html', 
        assigned_wallet=assigned_wallet,
        final_master_wallet=FINAL_MASTER_WALLET_4,
        qr_base64=qr_base64,
        remaining_seconds=remaining_seconds
    )

@app.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access unauthorized.', 'danger')
        return redirect(url_for('barcode_page'))
    users = User.query.all()
    return render_template('admin.html', users=users)

# --- SECURE API ENDPOINTS ---
@app.route('/api/generate-barcode', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_generate_barcode():
    db_user = db.session.get(User, current_user.id)
    if not db_user.is_admin and db_user.credits < 1:
        return jsonify({'error': 'Insufficient credits! Please top up via USDT.'}), 403
    try:
        form_data = request.form
        tab_type = sanitize_input(form_data.get('active_tab', 'us'))

        if tab_type == 'us':
            daq = sanitize_input(form_data.get('daq', ''))
            vehicle_class = sanitize_input(form_data.get('us_class', ''))
            
            raw_restrictions = sanitize_input(form_data.get('restrictions', '')).strip()
            restrictions = 'NONE' if not raw_restrictions or raw_restrictions.lower() == 'none' else raw_restrictions.upper()
            
            raw_endorsements = sanitize_input(form_data.get('endorsements', '')).strip()
            endorsements = 'NONE' if not raw_endorsements or raw_endorsements.lower() == 'none' else raw_endorsements.upper()
            
            exp_date = sanitize_input(form_data.get('exp_date', ''))
            
            raw_dcs = sanitize_input(form_data.get('dcs', '')).upper()
            raw_dac = sanitize_input(form_data.get('dac', '')).upper()
            if raw_dcs and raw_dac and raw_dcs in raw_dac:
                raw_dac = raw_dac.replace(raw_dcs, '').strip()
            
            dcs = raw_dcs[:20]
            dac = raw_dac[:15]
            dad = sanitize_input(form_data.get('dad', '')).upper()[:15]
            
            ddf_flag = 'T' if len(raw_dcs) > 20 else 'N'
            ddg_flag = 'T' if len(raw_dac) > 15 else 'N'
            ddh_flag = 'T' if len(sanitize_input(form_data.get('dad', '')).upper()) > 15 else 'N'
            
            dob = sanitize_input(form_data.get('dob', ''))
            issue_date = sanitize_input(form_data.get('issue_date', ''))
            height = "".join([c for c in sanitize_input(form_data.get('height', '')) if c.isdigit()])
            weight = "".join([c for c in sanitize_input(form_data.get('weight', '')) if c.isdigit()])
            eyes = sanitize_input(form_data.get('eyes', '')).upper()
            hair = sanitize_input(form_data.get('hair', '')).upper()
            address = sanitize_input(form_data.get('address', ''))
            city = sanitize_input(form_data.get('city', ''))
            
            state_code = sanitize_input(form_data.get('us_state', 'IL')).upper()
            iin = STATE_IINS.get(state_code, '636035')
            zip_code = sanitize_input(form_data.get('zip', ''))
            sex = sanitize_input(form_data.get('sex', ''))
            dcf = sanitize_input(form_data.get('dcf', 'USA-A89234110')).upper()
            dcg = "USA"

            columns_input = sanitize_input(form_data.get('pdf_columns', 'Auto'))
            # Compensate for library overhead (+2 columns offset) so target input matches rendered count
            columns = (int(columns_input) - 2) if columns_input.isdigit() and 3 <= int(columns_input) <= 30 else (int(columns_input) if columns_input.isdigit() and 1 <= int(columns_input) <= 30 else None)
            scale_factor = 3 if sanitize_input(form_data.get('export_resolution', '6X')) == '6X' else 2

            lines = [f"@\n\rANSI {iin}1002DL00410288DL"]
            if daq: lines.append(f"DAQ{daq}")
            if vehicle_class: lines.append(f"DCA{vehicle_class}")
            if restrictions: lines.append(f"DCB{restrictions}")
            if endorsements: lines.append(f"DCD{endorsements}")
            if exp_date: lines.append(f"DBA{exp_date}")
            if dcs: lines.append(f"DCS{dcs}")
            if dac: lines.append(f"DAC{dac}")
            if dad: lines.append(f"DAD{dad}")
            if ddf_flag: lines.append(f"DDF{ddf_flag}")
            if ddg_flag: lines.append(f"DDG{ddg_flag}")
            if ddh_flag: lines.append(f"DDH{ddh_flag}")
            if dob: lines.append(f"DBB{dob}")
            if issue_date: lines.append(f"DBD{issue_date}")
            if height: lines.append(f"DAU{height}")
            if weight: lines.append(f"DAW{weight}")
            if eyes: lines.append(f"DAY{eyes}")
            if hair: lines.append(f"DAZ{hair}")
            if address: lines.append(f"DAG{address}")
            if city: lines.append(f"DAI{city}")
            if state_code: lines.append(f"DAJ{state_code}")
            if zip_code: lines.append(f"DAK{zip_code}")
            if dcf: lines.append(f"DCF{dcf}")
            if dcg: lines.append(f"DCG{dcg}")
            if sex: lines.append(f"DBC{sex}")
            
            lines.append("DDAF")
            lines.append("DDK1")

            payload_str = "\n".join(lines) + "\n"
            payload = payload_str.encode('latin1')

            codes = encode(payload, columns=columns) if columns else encode(payload)
            image = render_image(codes, scale=scale_factor)
            rv = io.BytesIO()
            image.save(rv, format="PNG")
            encoded_image = base64.b64encode(rv.getvalue()).decode('utf-8')
            message_payload = payload_str

        elif tab_type == 'canada':
            daq = sanitize_input(form_data.get('ca_daq', ''))
            vehicle_class = sanitize_input(form_data.get('ca_class', ''))
            
            raw_ca_restrictions = sanitize_input(form_data.get('ca_restrictions', '')).strip()
            restrictions = 'NONE' if not raw_ca_restrictions or raw_ca_restrictions.lower() == 'none' else raw_ca_restrictions.upper()
            
            raw_ca_endorsements = sanitize_input(form_data.get('ca_endorsements', '')).strip()
            endorsements = 'NONE' if not raw_ca_endorsements or raw_ca_endorsements.lower() == 'none' else raw_ca_endorsements.upper()
            
            exp_date = sanitize_input(form_data.get('ca_exp', ''))
            
            raw_dcs = sanitize_input(form_data.get('ca_dcs', '')).upper()
            raw_dac = sanitize_input(form_data.get('ca_dac', '')).upper()
            if raw_dcs and raw_dac and raw_dcs in raw_dac:
                raw_dac = raw_dac.replace(raw_dcs, '').strip()
            
            dcs = raw_dcs[:20]
            dac = raw_dac[:15]
            dad = sanitize_input(form_data.get('ca_dad', '')).upper()[:15]
            
            ddf_flag = 'T' if len(raw_dcs) > 20 else 'N'
            ddg_flag = 'T' if len(raw_dac) > 15 else 'N'
            ddh_flag = 'T' if len(sanitize_input(form_data.get('ca_dad', '')).upper()) > 15 else 'N'
            
            dob = sanitize_input(form_data.get('ca_dob', ''))
            issue_date = sanitize_input(form_data.get('ca_issue', ''))
            height = "".join([c for c in sanitize_input(form_data.get('ca_height', '')) if c.isdigit()])
            weight = "".join([c for c in sanitize_input(form_data.get('ca_weight', '')) if c.isdigit()])
            eyes = sanitize_input(form_data.get('ca_eyes', '')).upper()
            hair = sanitize_input(form_data.get('ca_hair', '')).upper()
            address = sanitize_input(form_data.get('ca_address', ''))
            city = sanitize_input(form_data.get('ca_city', ''))
            
            province_name = sanitize_input(form_data.get('ca_province', 'Ontario'))
            iin = CANADA_PROVINCE_IINS.get(province_name, '636012')
            state_code = sanitize_input(form_data.get('ca_state_code', 'ON')).upper()
            postal = sanitize_input(form_data.get('ca_postal', ''))
            sex = sanitize_input(form_data.get('ca_sex', ''))
            dcf = sanitize_input(form_data.get('ca_dcf', 'CAN-H12345678')).upper()
            dcg = "CAN"

            columns_input = sanitize_input(form_data.get('ca_pdf_columns', 'Auto'))
            # Compensate for library overhead (+2 columns offset) so target input matches rendered count
            columns = (int(columns_input) - 2) if columns_input.isdigit() and 3 <= int(columns_input) <= 30 else (int(columns_input) if columns_input.isdigit() and 1 <= int(columns_input) <= 30 else None)
            scale_factor = 3 if sanitize_input(form_data.get('ca_export_resolution', '6X')) == '6X' else 2

            lines = [f"@\n\rANSI {iin}1002DL00410288DL"]
            if daq: lines.append(f"DAQ{daq}")
            if vehicle_class: lines.append(f"DCA{vehicle_class}")
            if restrictions: lines.append(f"DCB{restrictions}")
            if endorsements: lines.append(f"DCD{endorsements}")
            if exp_date: lines.append(f"DBA{exp_date}")
            if dcs: lines.append(f"DCS{dcs}")
            if dac: lines.append(f"DAC{dac}")
            if dad: lines.append(f"DAD{dad}")
            if ddf_flag: lines.append(f"DDF{ddf_flag}")
            if ddg_flag: lines.append(f"DDG{ddg_flag}")
            if ddh_flag: lines.append(f"DDH{ddh_flag}")
            if dob: lines.append(f"DBB{dob}")
            if issue_date: lines.append(f"DBD{issue_date}")
            if height: lines.append(f"DAU{height}")
            if weight: lines.append(f"DAW{weight}")
            if eyes: lines.append(f"DAY{eyes}")
            if hair: lines.append(f"DAZ{hair}")
            if address: lines.append(f"DAG{address}")
            if city: lines.append(f"DAI{city}")
            if state_code: lines.append(f"DAJ{state_code}")
            if postal: lines.append(f"DAK{postal}")
            if dcf: lines.append(f"DCF{dcf}")
            if dcg: lines.append(f"DCG{dcg}")
            if sex: lines.append(f"DBC{sex}")

            payload_str = "\n".join(lines) + "\n"
            payload = payload_str.encode('latin1')

            codes = encode(payload, columns=columns) if columns else encode(payload)
            image = render_image(codes, scale=scale_factor)
            rv = io.BytesIO()
            image.save(rv, format="PNG")
            encoded_image = base64.b64encode(rv.getvalue()).decode('utf-8')
            message_payload = payload_str

        else:
            code_data = sanitize_input(form_data.get('uk_licence', 'SMITJ90151AZ9EE'))
            Code39 = barcode.get_barcode_class('code39')
            rv = io.BytesIO()
            Code39(code_data, writer=ImageWriter()).write(rv)
            encoded_image = base64.b64encode(rv.getvalue()).decode('utf-8')
            message_payload = f"UK-DL-1D: {code_data} | Region: {sanitize_input(form_data.get('uk_region', 'DVLA'))}"

        if not db_user.is_admin:
            try:
                rows_updated = User.query.filter_by(id=db_user.id).filter(User.credits >= 1).update(
                    {"credits": User.credits - 1}, synchronize_session=False
                )
                db.session.commit()
                if rows_updated == 0:
                    db.session.rollback()
                    return jsonify({'error': 'Insufficient credits! Please top up via USDT.'}), 403
                db.session.refresh(db_user)
            except Exception as db_err:
                db.session.rollback()
                return jsonify({'error': f'Database transaction error: {str(db_err)}'}), 500

        return jsonify({
            'success': True, 
            'message': message_payload,
            'image_base64': encoded_image,
            'remaining_credits': db_user.credits
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def calculate_check_digit(data_str):
    weights = [7, 3, 1]
    total_sum = 0
    for i, char in enumerate(data_str):
        char_upper = char.upper()
        if char_upper.isdigit():
            val = int(char_upper)
        elif 'A' <= char_upper <= 'Z':
            val = ord(char_upper) - ord('A') + 10
        else:
            val = 0
        weight = weights[i % 3]
        total_sum += val * weight
    return str(total_sum % 10)

@app.route('/api/generate-mrz', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_generate_mrz():
    db_user = db.session.get(User, current_user.id)
    if not db_user.is_admin and db_user.credits < 1:
        return jsonify({'error': 'Insufficient credits! Please top up via USDT.'}), 403
    try:
        form_data = request.form
        doc_type = sanitize_input(form_data.get('document_type', 'P'))
        issuing_country = sanitize_input(form_data.get('issuing_country', 'KEN'))
        surname = sanitize_input(form_data.get('surname', '')).upper()
        given_names = sanitize_input(form_data.get('given_names', '')).upper()
        doc_number = sanitize_input(form_data.get('doc_number', '')).upper()
        nationality = sanitize_input(form_data.get('nationality', 'KEN'))
        dob = sanitize_input(form_data.get('dob', ''))
        expiry = sanitize_input(form_data.get('expiry', ''))
        sex = sanitize_input(form_data.get('sex', 'M')).strip().upper()[:1] or 'M'
        personal_no = sanitize_input(form_data.get('personal_no', '')).upper()

        def format_mrz_date(date_str):
            if not date_str: return "000000"
            parts = date_str.split('-')
            if len(parts) == 3:
                return parts[0][2:] + parts[1] + parts[2]
            return "000000"

        b_date = format_mrz_date(dob)
        e_date = format_mrz_date(expiry)

        clean_surname = surname.replace(' ', '<')
        clean_given = given_names.replace(' ', '<')
        name_field = f"{clean_surname}<<{clean_given}"
        line1 = f"{doc_type}<{issuing_country}{name_field}"
        line1 = line1.ljust(44, '<')[:44]

        doc_num_field = doc_number.ljust(9, '<')[:9]
        doc_num_cd = calculate_check_digit(doc_num_field)

        dob_field = b_date.ljust(6, '<')[:6]
        dob_cd = calculate_check_digit(dob_field)

        exp_field = e_date.ljust(6, '<')[:6]
        exp_cd = calculate_check_digit(exp_field)

        pers_field = personal_no.ljust(14, '<')[:14]
        pers_cd = calculate_check_digit(pers_field)

        composite_data = f"{doc_num_field}{doc_num_cd}{dob_field}{dob_cd}{exp_field}{exp_cd}{pers_field}{pers_cd}"
        composite_cd = calculate_check_digit(composite_data)

        line2_core = f"{doc_num_field}{doc_num_cd}{nationality}{dob_field}{dob_cd}{sex}{exp_field}{exp_cd}{pers_field}{pers_cd}{composite_cd}"
        line2 = line2_core.ljust(44, '<')[:44]

        mrz_result = f"{line1}\n{line2}"

        if not db_user.is_admin:
            try:
                rows_updated = User.query.filter_by(id=db_user.id).filter(User.credits >= 1).update(
                    {"credits": User.credits - 1}, synchronize_session=False
                )
                db.session.commit()
                if rows_updated == 0:
                    db.session.rollback()
                    return jsonify({'error': 'Insufficient credits! Please top up via USDT.'}), 403
                db.session.refresh(db_user)
            except Exception as db_err:
                db.session.rollback()
                return jsonify({'error': f'Database transaction error: {str(db_err)}'}), 500

        return jsonify({
            'success': True,
            'message': mrz_result,
            'remaining_credits': db_user.credits
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# --- SECURE BLOCKCHAIN TXID VERIFICATION ENDPOINT WITH RETRY LOGIC ---
@app.route('/api/verify-usdt-topup', methods=['POST'])
@limiter.limit("10 per minute")
def verify_usdt_topup():
    data = request.get_json() or {}
    txid = sanitize_input(data.get('txid', '')).strip()
    try:
        credits_to_add = int(data.get('credits', 1))
    except (ValueError, TypeError):
        credits_to_add = 1
    
    if not txid:
        return jsonify({'error': 'Transaction hash (TXID) is required'}), 400

    try:
        # 1. Prevent TXID Re-use
        existing_tx = TransactionHistory.query.filter_by(receipt_number=txid).first()
        if existing_tx:
            return jsonify({'error': 'This transaction hash has already been redeemed!'}), 400

        # 2. Query official TronGrid API with retry loop to handle blockchain propagation delays
        tx_url = f"https://api.trongrid.io/v1/transactions/{txid}"
        headers = {}
        tron_api_key = os.getenv('TRON_PRO_API_KEY')
        if tron_api_key:
            headers['TRON-PRO-API-KEY'] = tron_api_key

        tx_res = None
        for attempt in range(3):
            try:
                tx_res = requests.get(tx_url, headers=headers, timeout=10)
                if tx_res.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1.5)  # Brief pause before retrying propagation lookup

        if not tx_res or tx_res.status_code != 200:
            return jsonify({'error': 'Transaction could not be found on the Tron network yet. Please wait a moment and try again.'}), 400

        tx_json = tx_res.json()
        data_list = tx_json.get('data', [])
        if not data_list:
            return jsonify({'error': 'Invalid TXID or transaction has not propagated yet.'}), 400

        tx_info = data_list[0]
        
        # Ensure transaction is confirmed/successful
        if tx_info.get('ret', [{}])[0].get('contractRet') != 'SUCCESS':
            return jsonify({'error': 'Transaction failed or is unconfirmed on the blockchain.'}), 400

        # Determine target assigned wallet for user session to enforce strict match verification
        identifier_filter = {'user_id': current_user.id} if current_user.is_authenticated else {'temp_session_id': session.get('visitor_session_id')}
        active_session = ActiveCheckoutSession.query.filter_by(**identifier_filter).first()
        target_wallet = active_session.assigned_wallet if active_session else ROTATING_WALLETS[0]

        # 3. Strict cryptographic validation of TRC20 transfer matching official USDT contract, wallet, and payment amount
        raw_data = tx_info.get('raw_data', {})
        contract = raw_data.get('contract', [{}])[0]
        contract_type = contract.get('type')

        is_valid_transfer = False
        if contract_type == 'TriggerSmartContract':
            value_dict = contract.get('parameter', {}).get('value', {})
            contract_addr_hex = value_dict.get('contract_address', '')
            
            # Convert official USDT contract address to hex for absolute contract validation
            usdt_contract_hex = base58_to_hex(USDT_TRC20_CONTRACT).lower()
            
            data_payload = value_dict.get('data', '')
            
            # Ensure contract matches USDT and data payload is a valid TRC20 transfer (method ID + 32-byte recipient + 32-byte amount)
            if contract_addr_hex.lower() == usdt_contract_hex and data_payload.startswith('a9059cbb') and len(data_payload) >= 136:
                recipient_hex_padded = data_payload[8:72]
                recipient_hex = recipient_hex_padded[-40:].lower()
                target_wallet_hex = base58_to_hex(target_wallet).lower()
                
                # Extract and decode the on-chain transfer amount (USDT uses 6 decimal places)
                amount_hex = data_payload[72:136]
                transferred_amount_units = int(amount_hex, 16)
                transferred_usdt = transferred_amount_units / (10 ** 6)
                
                # Enforce that the actual USDT transferred covers the requested credits (assuming 1 USDT = 1 credit)
                if target_wallet_hex and recipient_hex == target_wallet_hex and transferred_usdt >= float(credits_to_add):
                    is_valid_transfer = True

        if not is_valid_transfer:
            return jsonify({'error': 'Transaction does not match a valid target USDT transfer amount for this session.'}), 400

        # 4. Map credits to authenticated user or queue for guest visitor session
        try:
            user_id_val = current_user.id if current_user.is_authenticated else None
            temp_id_val = None if current_user.is_authenticated else session.get('visitor_session_id')

            new_tx = TransactionHistory(
                receipt_number=txid,
                user_id=user_id_val,
                temp_session_id=temp_id_val,
                amount=float(credits_to_add)
            )
            db.session.add(new_tx)

            if current_user.is_authenticated:
                db_user = db.session.get(User, current_user.id)
                db_user.credits += credits_to_add

            db.session.commit()
        except Exception as commit_err:
            db.session.rollback()
            return jsonify({'error': f'Failed to record transaction: {str(commit_err)}'}), 500

        current_credits = current_user.credits if current_user.is_authenticated else credits_to_add

        return jsonify({
            'success': True,
            'message': f'Successfully verified! {credits_to_add} credit(s) added.',
            'new_credits': current_credits
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Verification error: {str(e)}'}), 500
        # Automatically patch SQLite table columns if migrating from an older DB version
        try:
            db.session.execute(db.text('ALTER TABLE active_checkout_session ADD COLUMN temp_session_id VARCHAR(100);'))
            db.session.commit()
        except Exception:
            db.session.rollback()
            
        try:
            db.session.execute(db.text('ALTER TABLE transaction_history ADD COLUMN temp_session_id VARCHAR(100);'))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Force update admin password to JuneJune2020@ on every launch
        admin_user = User.query.filter_by(username='admin').first()
        if admin_user:
            admin_user.password = generate_password_hash('JuneJune2020@', method='scrypt')
            admin_user.credits = 999
            admin_user.is_admin = True
            db.session.commit()
        else:
            admin_user = User(
                username='admin', 
                password=generate_password_hash('JuneJune2020@', method='scrypt'), 
                credits=999, 
                is_admin=True
            )
            db.session.add(admin_user)
            db.session.commit()
            
    # Production-safe entrypoint: Use development server only if explicitly not in production
    if os.getenv('FLASK_ENV') == 'production':
        raise RuntimeError("Production environment detected: Do not run via app.run(). Use a WSGI server like Gunicorn instead (e.g., gunicorn -w 4 -b 0.0.0.0:5000 app:app).")
    app.run(debug=True, port=5000)

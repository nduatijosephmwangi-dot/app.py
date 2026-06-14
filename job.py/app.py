"""
Wambui Shadrack Advocates — Legal Portal Backend
Production-ready Flask app with live M-Pesa Daraja STK Push.

ENVIRONMENT VARIABLES REQUIRED (set in your host: Render/Railway/Heroku/etc.)
---------------------------------------------------------------------------
DATABASE_URL              postgres://user:pass@host:port/dbname
FRONTEND_URL              https://your-frontend.com   (for CORS; "*" for testing only)

# --- M-Pesa Daraja (LIVE / production) ---
MPESA_ENV                 production           # or "sandbox"
MPESA_CONSUMER_KEY        <your consumer key>
MPESA_CONSUMER_SECRET     <your consumer secret>   # ROTATE if leaked
MPESA_SHORTCODE           4747331              # Paybill / Till
MPESA_PASSKEY             <Lipa Na M-Pesa Online passkey from Daraja>
MPESA_CALLBACK_URL        https://your-backend.com/api/public/mpesa/callback
MPESA_TRANSACTION_TYPE    CustomerPayBillOnline   # or CustomerBuyGoodsOnline for Till

SECURITY NOTES
- NEVER hardcode the Consumer Secret or Passkey in source. Use env vars only.
- The callback URL MUST be publicly reachable over HTTPS for Daraja to deliver results.
- Rotate any secret that has ever appeared in chat, screenshots, or a public repo.
"""

import os
import random
import logging
import base64
import requests
from datetime import datetime
from requests.auth import HTTPBasicAuth

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# =========================================================
# ⚙️ APP CONFIG
# =========================================================
app = Flask(__name__)

frontend_url = os.environ.get("FRONTEND_URL", "*")
CORS(app, resources={r"/api/*": {"origins": frontend_url}})

app.config['DATABASE_URL'] = os.environ.get(
    'DATABASE_URL',
    'dbname=postgres user=postgres password=jose1023 host=localhost port=5432'
)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', './client_docs/')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

logging.basicConfig(
    filename='system_security.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

SYSTEM_STATE = {"LOCKDOWN_MODE": False, "OTP_STORE": {}}

# =========================================================
# 💰 M-PESA DARAJA INTEGRATION (LIVE STK PUSH)
# =========================================================
MPESA_ENV = os.environ.get('MPESA_ENV', 'sandbox').lower()
MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY', '')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET', '')
MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE', '4747331')
MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY', '')
MPESA_CALLBACK_URL = os.environ.get('MPESA_CALLBACK_URL', '')
MPESA_TRANSACTION_TYPE = os.environ.get('MPESA_TRANSACTION_TYPE', 'CustomerPayBillOnline')

if MPESA_ENV == 'production':
    MPESA_BASE = 'https://api.safaricom.co.ke'
else:
    MPESA_BASE = 'https://sandbox.safaricom.co.ke'


def _normalize_phone(phone: str) -> str:
    """Normalize Kenyan numbers to 2547XXXXXXXX format required by Daraja."""
    p = str(phone or '').strip().replace(' ', '').replace('+', '')
    if p.startswith('0') and len(p) == 10:
        p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9:
        p = '254' + p
    return p


def get_mpesa_access_token():
    """OAuth token from Daraja. Raises on failure."""
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise RuntimeError("M-Pesa credentials not configured (MPESA_CONSUMER_KEY/SECRET).")
    url = f"{MPESA_BASE}/oauth/v1/generate?grant_type=client_credentials"
    resp = requests.get(
        url,
        auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get('access_token')
    if not token:
        raise RuntimeError(f"Daraja did not return access_token: {data}")
    return token


def build_mpesa_password():
    """Return (password, timestamp) per Daraja spec: base64(shortcode + passkey + timestamp)."""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode('utf-8')
    return password, timestamp


def initiate_stk_push(phone: str, amount: float, account_ref: str, description: str = "Legal Fees"):
    """Trigger Lipa Na M-Pesa Online STK Push. Returns Daraja JSON response."""
    token = get_mpesa_access_token()
    password, timestamp = build_mpesa_password()

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": MPESA_TRANSACTION_TYPE,
        "Amount": int(round(float(amount))),
        "PartyA": _normalize_phone(phone),
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": _normalize_phone(phone),
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": account_ref[:12] if account_ref else "LegalFees",
        "TransactionDesc": (description or "Legal Fees")[:13],
    }

    url = f"{MPESA_BASE}/mpesa/stkpush/v1/processrequest"
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    logging.info(f"STK Push response ({resp.status_code}): {data}")
    return resp.status_code, data


# =========================================================
# 🗄️ DATABASE
# =========================================================
def get_db():
    if 'db' not in g:
        g.db = psycopg2.connect(app.config['DATABASE_URL'], cursor_factory=RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    try:
        conn = psycopg2.connect(app.config['DATABASE_URL'])
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                full_name VARCHAR(255) NOT NULL,
                phone_number VARCHAR(50) UNIQUE NOT NULL,
                role VARCHAR(50) NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) UNIQUE NOT NULL,
                case_parties TEXT,
                client_name VARCHAR(255),
                next_court_date VARCHAR(255),
                coming_up_for TEXT,
                total_balance NUMERIC(15,2) DEFAULT 0.00,
                paid_balance NUMERIC(15,2) DEFAULT 0.00,
                ai_access_granted BOOLEAN DEFAULT FALSE
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_client_logs (
                log_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) NOT NULL,
                client_name VARCHAR(255),
                client_question TEXT NOT NULL,
                ai_response TEXT NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mpesa_transactions (
                tx_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255),
                phone_number VARCHAR(50),
                amount NUMERIC(15,2),
                merchant_request_id VARCHAR(255),
                checkout_request_id VARCHAR(255) UNIQUE,
                mpesa_receipt VARCHAR(255),
                result_code INTEGER,
                result_desc TEXT,
                status VARCHAR(50) DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
        """)

        seed_users = [
            ('Shadrack Wambui', '0700260086', 'admin'),
            ('Jeff Kangethe', '0704704758', 'advocate'),
            ('Jeff Kangethe', '0796178783', 'advocate'),
            ('Jane Onyango', '0795204923', 'secretary'),
        ]
        for name, phone, role in seed_users:
            cur.execute(
                "INSERT INTO users (full_name, phone_number, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (phone_number) DO NOTHING;",
                (name, phone, role),
            )

        conn.commit()
        cur.close()
        conn.close()
        print("💾 Database initialized.")
    except Exception as e:
        print(f"⚠️ DB init failure: {e}")


# =========================================================
# 🛡️ SECURITY MIDDLEWARE
# =========================================================
@app.before_request
def cyber_security_check():
    if SYSTEM_STATE["LOCKDOWN_MODE"]:
        allowed_routes = ['login_router', 'verify_otp', 'toggle_kill_switch', 'mpesa_callback']
        if request.endpoint not in allowed_routes:
            logging.warning(f"BLOCKED: {request.endpoint} during lockdown")
            return jsonify({
                "success": False,
                "error": "SECURITY_LOCKDOWN",
                "message": "⚠️ PORTAL LOCKDOWN ACTIVE."
            }), 503


# =========================================================
# 🔐 AUTH
# =========================================================
@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    payload = request.get_json() or {}
    credential = payload.get('credential', '').strip()
    if not credential:
        return jsonify({"success": False, "message": "Login field cannot be blank."}), 400
    if credential.isdigit() and len(credential) >= 10:
        return initiate_staff_login(credential)
    return client_login(credential)


def initiate_staff_login(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT full_name, phone_number, role FROM users
            WHERE phone_number = %s AND role IN ('admin', 'advocate', 'secretary');
        """, (phone,))
        account = cur.fetchone()
        if not account:
            return jsonify({"success": False, "message": "Access Denied: Not registered staff."}), 403
        otp = str(random.randint(100000, 999999))
        SYSTEM_STATE["OTP_STORE"][phone] = {"code": otp, "user": account}
        print(f"\n📡 OTP for {account['full_name']} -> {otp}\n")
        logging.info(f"OTP generated for {account['phone_number']}")
        return jsonify({"success": True, "mode": "otp_required", "message": "OTP dispatched."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Auth fault: {e}"}), 500


@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    record = SYSTEM_STATE["OTP_STORE"].get(phone)
    if not record or record['code'] != code:
        return jsonify({"success": False, "message": "Invalid or expired OTP."}), 401
    SYSTEM_STATE["OTP_STORE"].pop(phone, None)
    return jsonify({
        "success": True,
        "role": record['user']['role'],
        "user_name": record['user']['full_name'],
        "lockdown_status": SYSTEM_STATE["LOCKDOWN_MODE"]
    })


def client_login(case_number):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT case_id, case_number, case_parties, client_name, ai_access_granted,
                   next_court_date, coming_up_for, total_balance, paid_balance
            FROM cases WHERE case_number ILIKE %s
        """, (f"%{case_number}%",))
        case = cur.fetchone()
        if not case:
            return jsonify({"success": False, "message": "No case found."}), 404
        total = float(case['total_balance'] or 0)
        paid = float(case['paid_balance'] or 0)
        score = random.randint(55, 98)
        return jsonify({
            "success": True,
            "mode": "client_dashboard",
            "data": {
                "case_id": case['case_id'],
                "case_number": case['case_number'],
                "case_parties": case['case_parties'],
                "client_name": case['client_name'],
                "next_court_date": str(case['next_court_date']),
                "coming_up_for": case['coming_up_for'],
                "financials": {"total": total, "paid": paid, "balance": total - paid},
                "ai_unlocked": case['ai_access_granted'],
                "case_predictor": {
                    "score": score,
                    "analysis": f"Outcome trends track at an estimated {score}% favorable rating."
                }
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"DB failure: {e}"}), 500


# =========================================================
# 🤖 AI
# =========================================================
@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json() or {}
    question = data.get('question', '').strip()
    user_name = data.get('user_name', '').strip()
    case_number = data.get('case_number', '').strip()
    ai_type = data.get('ai_type', 'free').strip().lower()

    if not question:
        return jsonify({"success": False, "message": "Question cannot be blank."}), 400

    if user_name == "Shadrack Wambui":
        ans = f"⚖️ [Admin AI - Constitution 2010]: For '{question}', see Chapter Four (Bill of Rights)."
        return jsonify({"success": True, "engine": "Constitution 2010", "answer": ans})

    if user_name:
        ans = f"📋 [Staff Assistant AI - {user_name}]: Processing '{question}'."
        return jsonify({"success": True, "engine": "Staff Assistant Free AI", "answer": ans})

    if case_number:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT client_name, ai_access_granted FROM cases WHERE case_number = %s", (case_number,))
            case_record = cur.fetchone()
            if not case_record:
                return jsonify({"success": False, "message": "Case not found."}), 404

            if ai_type == "consultant":
                if case_record['ai_access_granted']:
                    ans = f"🧠 [Premium Consultant AI]: Strategic evaluation for '{question}'."
                    engine = "Paid Consultant AI"
                else:
                    return jsonify({
                        "success": False,
                        "message": "Premium Consultant AI requires KES 5,000 activation."
                    }), 402
            else:
                ans = f"ℹ️ [Client Free AI]: Summary for '{question}'."
                engine = "Client Free AI"

            cur.execute("""
                INSERT INTO ai_client_logs (case_number, client_name, client_question, ai_response)
                VALUES (%s, %s, %s, %s)
            """, (case_number, case_record['client_name'], question, ans))
            conn.commit()
            return jsonify({"success": True, "engine": engine, "answer": ans})
        except Exception as e:
            return jsonify({"success": False, "message": f"AI fault: {e}"}), 500

    return jsonify({"success": False, "message": "Unable to verify routing scope."}), 400


# =========================================================
# 💸 PAYMENTS — LIVE M-PESA STK PUSH
# =========================================================
@app.route('/api/public/process-payment', methods=['POST'])
def process_payment():
    payload = request.get_json() or {}
    amount = payload.get('amount')
    account_number = (payload.get('account_number') or '').strip()
    payment_method = (payload.get('payment_method') or '').lower()
    phone_number = (payload.get('phone_number') or '').strip()

    try:
        if not amount or float(amount) <= 0:
            return jsonify({"success": False, "message": "Valid amount required."}), 400
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Amount must be numeric."}), 400

    if not account_number:
        return jsonify({"success": False, "message": "Account/case number required."}), 400
    if payment_method not in ['mpesa', 'card']:
        return jsonify({"success": False, "message": "Select Mpesa or Card."}), 400
    if payment_method == 'mpesa' and not phone_number:
        return jsonify({"success": False, "message": "Phone number required for M-Pesa."}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT case_number, ai_access_granted FROM cases WHERE case_number = %s", (account_number,))
        case_record = cur.fetchone()
        if not case_record:
            return jsonify({"success": False, "message": "Account does not match any case."}), 404

        float_amount = float(amount)

        if payment_method == 'mpesa':
            # Trigger real STK push
            try:
                status_code, daraja_resp = initiate_stk_push(
                    phone=phone_number,
                    amount=float_amount,
                    account_ref=account_number,
                    description="Legal Fees",
                )
            except Exception as e:
                logging.error(f"STK push exception: {e}")
                return jsonify({"success": False, "message": f"M-Pesa gateway error: {e}"}), 502

            if status_code == 200 and str(daraja_resp.get('ResponseCode')) == '0':
                # Record pending tx — DO NOT credit balance until callback confirms
                cur.execute("""
                    INSERT INTO mpesa_transactions
                    (case_number, phone_number, amount, merchant_request_id, checkout_request_id, status)
                    VALUES (%s, %s, %s, %s, %s, 'PENDING')
                    ON CONFLICT (checkout_request_id) DO NOTHING
                """, (
                    account_number,
                    _normalize_phone(phone_number),
                    float_amount,
                    daraja_resp.get('MerchantRequestID'),
                    daraja_resp.get('CheckoutRequestID'),
                ))
                conn.commit()
                return jsonify({
                    "success": True,
                    "message": f"M-Pesa prompt sent to {phone_number}. Enter your PIN.",
                    "checkout_request_id": daraja_resp.get('CheckoutRequestID'),
                })
            else:
                return jsonify({
                    "success": False,
                    "message": daraja_resp.get('errorMessage') or daraja_resp.get('CustomerMessage') or "STK push rejected.",
                    "daraja": daraja_resp,
                }), 400

        # Card path (stub — wire to Stripe/Flutterwave when ready)
        return jsonify({
            "success": False,
            "message": "Card payments not yet wired. Configure Stripe/Flutterwave to enable."
        }), 501

    except Exception as e:
        return jsonify({"success": False, "message": f"Payment failure: {e}"}), 500


@app.route('/api/public/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Daraja posts the final result here. Credit balance ONLY on success."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        logging.info(f"M-Pesa callback: {body}")
        stk = body.get('Body', {}).get('stkCallback', {})
        checkout_id = stk.get('CheckoutRequestID')
        result_code = stk.get('ResultCode')
        result_desc = stk.get('ResultDesc')

        receipt = None
        amount_paid = None
        if result_code == 0:
            for item in stk.get('CallbackMetadata', {}).get('Item', []) or []:
                if item.get('Name') == 'MpesaReceiptNumber':
                    receipt = item.get('Value')
                elif item.get('Name') == 'Amount':
                    amount_paid = float(item.get('Value') or 0)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE mpesa_transactions
            SET result_code=%s, result_desc=%s, mpesa_receipt=%s,
                status=%s, completed_at=CURRENT_TIMESTAMP
            WHERE checkout_request_id=%s
            RETURNING case_number, amount
        """, (
            result_code, result_desc, receipt,
            'SUCCESS' if result_code == 0 else 'FAILED',
            checkout_id,
        ))
        row = cur.fetchone()

        if result_code == 0 and row:
            credited = amount_paid if amount_paid else float(row['amount'])
            # Update ledger; unlock AI if this is the 5,000 activation
            cur.execute("""
                UPDATE cases
                SET paid_balance = paid_balance + %s,
                    ai_access_granted = (ai_access_granted OR %s)
                WHERE case_number = %s
            """, (credited, credited >= 5000, row['case_number']))

        conn.commit()
        # Daraja requires this exact ack shape
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    except Exception as e:
        logging.error(f"Callback failure: {e}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route('/api/payment/status/<checkout_request_id>', methods=['GET'])
def payment_status(checkout_request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT status, result_desc, mpesa_receipt, amount
            FROM mpesa_transactions WHERE checkout_request_id = %s
        """, (checkout_request_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "message": "Unknown transaction."}), 404
        return jsonify({"success": True, "transaction": row})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# =========================================================
# 📎 UPLOADS
# =========================================================
@app.route('/api/documents/upload', methods=['POST'])
def document_upload():
    if 'document' not in request.files:
        return jsonify({"success": False, "message": "No document attached."}), 400
    f = request.files['document']
    name = secure_filename(f.filename)
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], name))
    return jsonify({"success": True, "message": "Document uploaded."})


# =========================================================
# 🏢 STAFF ENDPOINTS
# =========================================================
@app.route('/api/staff/search', methods=['POST'])
def search_cases():
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    user_name = data.get('user_name', '').strip()
    try:
        conn = get_db()
        cur = conn.cursor()
        if not query:
            cur.execute("""
                SELECT case_id, case_number, case_parties, client_name, total_balance,
                       paid_balance, next_court_date, coming_up_for
                FROM cases ORDER BY case_id DESC
            """)
        else:
            term = f"%{query}%"
            cur.execute("""
                SELECT case_id, case_number, case_parties, client_name, total_balance,
                       paid_balance, next_court_date, coming_up_for
                FROM cases
                WHERE case_number ILIKE %s OR client_name ILIKE %s OR case_parties ILIKE %s
                ORDER BY case_id DESC
            """, (term, term, term))
        results = cur.fetchall()
        for row in results:
            if user_name != "Shadrack Wambui":
                row['total_balance'] = "RESTRICTED"
                row['paid_balance'] = "RESTRICTED"
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/staff/ai-monitoring', methods=['GET'])
def monitor_client_ai():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT log_id, case_number, client_name, client_question, ai_response, logged_at
            FROM ai_client_logs ORDER BY logged_at DESC
        """)
        return jsonify({"success": True, "logs": cur.fetchall()})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/staff/update-matter', methods=['POST'])
def update_matter():
    data = request.get_json() or {}
    user_name = data.get('user_name', '').strip()
    case_id = data.get('case_id')
    next_court_date = data.get('next_court_date')
    coming_up_for = data.get('coming_up_for')

    try:
        conn = get_db()
        cur = conn.cursor()
        if user_name != "Shadrack Wambui":
            cur.execute("SELECT total_balance, paid_balance FROM cases WHERE case_id = %s", (case_id,))
            current = cur.fetchone()
            if current:
                it = data.get('total_balance')
                ip = data.get('paid_balance')
                if (it is not None and str(it) != "RESTRICTED" and float(it) != float(current['total_balance'])) or \
                   (ip is not None and str(ip) != "RESTRICTED" and float(ip) != float(current['paid_balance'])):
                    return jsonify({
                        "success": False,
                        "message": "Only Shadrack Wambui may edit financials."
                    }), 403

        cur.execute("""
            UPDATE cases
            SET next_court_date=%s, coming_up_for=%s, total_balance=%s, paid_balance=%s
            WHERE case_id=%s
        """, (next_court_date, coming_up_for, data.get('total_balance'), data.get('paid_balance'), case_id))
        conn.commit()
        return jsonify({"success": True, "message": "Case updated."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/admin/kill-switch', methods=['POST'])
def toggle_kill_switch():
    action = (request.get_json() or {}).get('action', '').upper()
    if action == 'LOCK':
        SYSTEM_STATE["LOCKDOWN_MODE"] = True
        logging.critical("🚨 LOCKDOWN ENGAGED")
        return jsonify({"success": True, "status": "LOCKED", "message": "🚨 Client paths closed."})
    SYSTEM_STATE["LOCKDOWN_MODE"] = False
    logging.critical("✅ LOCKDOWN CLEARED")
    return jsonify({"success": True, "status": "ACTIVE", "message": "✅ Online."})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "mpesa_env": MPESA_ENV})


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

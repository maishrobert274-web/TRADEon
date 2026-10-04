from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, random, time, requests, base64
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tradeon-demo-secret-change-later")
DB = "tradeon.db"

def init_db():
    with sqlite3.connect(DB) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )""")
        cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "verified" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        con.execute("""CREATE TABLE IF NOT EXISTS deposits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, phone TEXT NOT NULL, merchant_request_id TEXT, checkout_request_id TEXT UNIQUE, mpesa_receipt TEXT, status TEXT NOT NULL DEFAULT "PENDING", created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS wallets (user_id INTEGER PRIMARY KEY, balance REAL NOT NULL DEFAULT 0)""")
init_db()

def normalize_phone(phone):
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("07") or p.startswith("01"):
        return "+254" + p[1:]
    if p.startswith("254"):
        return "+" + p
    return p

def send_otp(phone, otp):
    api_key = os.environ.get("AT_API_KEY")
    username = os.environ.get("AT_USERNAME", "sandbox")
    sender = os.environ.get("AT_SENDER_ID", "")
    if not api_key:
        return False
    data = {
        "username": username,
        "to": phone,
        "message": f"Your TRADEON verification code is {otp}. It expires in 5 minutes."
    }
    if sender:
        data["from"] = sender
    try:
        response = requests.post(
            "https://api.africastalking.com/version1/messaging",
            headers={"apiKey": api_key, "Accept": "application/json"},
            data=data,
            timeout=15
        )
        return response.status_code < 300
    except requests.RequestException:
        return False

@app.route("/")
def home():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", user_name=session.get("user_name"))

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        if not name or not phone or len(password) < 6:
            error = "Enter your name, a valid phone number and a password of at least 6 characters."
        else:
            try:
                with sqlite3.connect(DB) as con:
                    cur = con.execute(
                        "INSERT INTO users (name,phone,password,verified) VALUES (?,?,?,0)",
                        (name, phone, generate_password_hash(password))
                    )
                    user_id = cur.lastrowid
                otp = str(random.randint(100000, 999999))
                session["verify_user_id"] = user_id
                session["verify_otp_hash"] = generate_password_hash(otp)
                session["verify_expires"] = time.time() + 300
                if send_otp(phone, otp):
                    return redirect(url_for("verify_phone"))
                with sqlite3.connect(DB) as con:
                    con.execute("DELETE FROM users WHERE id=?", (user_id,))
                session.pop("verify_user_id", None)
                session.pop("verify_otp_hash", None)
                session.pop("verify_expires", None)
                error = "SMS could not be sent. SMS service credentials must be configured first."
            except sqlite3.IntegrityError:
                error = "That phone number is already registered."
    return render_template("register.html", error=error)

@app.route("/verify-phone", methods=["GET", "POST"])
def verify_phone():
    if "verify_user_id" not in session:
        return redirect(url_for("register"))
    error = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if time.time() > session.get("verify_expires", 0):
            error = "The code has expired. Please register again."
        elif check_password_hash(session.get("verify_otp_hash", ""), code):
            with sqlite3.connect(DB) as con:
                user = con.execute(
                    "SELECT id,name FROM users WHERE id=?",
                    (session["verify_user_id"],)
                ).fetchone()
                if not user:
                    return redirect(url_for("register"))
                con.execute("UPDATE users SET verified=1 WHERE id=?", (user[0],))
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            for key in ("verify_user_id", "verify_otp_hash", "verify_expires"):
                session.pop(key, None)
            return redirect(url_for("home"))
        else:
            error = "Incorrect verification code."
    return render_template("verify_phone.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        with sqlite3.connect(DB) as con:
            user = con.execute(
                "SELECT id,name,password,verified FROM users WHERE phone=?",
                (phone,)
            ).fetchone()
        if user and check_password_hash(user[2], password):
            if not user[3]:
                session["verify_user_id"] = user[0]
                return redirect(url_for("verify_phone"))
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            return redirect(url_for("home"))
        error = "Incorrect phone number or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

def daraja_token():
    key = os.environ.get("MPESA_CONSUMER_KEY")
    secret = os.environ.get("MPESA_CONSUMER_SECRET")
    if not key or not secret:
        raise RuntimeError("M-Pesa credentials are not configured.")
    base = os.environ.get("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke")
    r = requests.get(base + "/oauth/v1/generate?grant_type=client_credentials", auth=(key, secret), timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def mpesa_timestamp():
    return datetime.now().strftime("%Y%m%d%H%M%S")

def mpesa_password(shortcode, passkey, timestamp):
    return base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()

@app.route("/deposit", methods=["GET", "POST"])
def deposit():
    if "user_id" not in session:
        return redirect(url_for("login"))
    error = None
    message = None
    amount = request.form.get("amount", "").strip() if request.method == "POST" else ""
    if request.method == "POST":
        phone = normalize_phone(request.form.get("phone", "")).replace("+", "")
        try:
            value = float(amount)
            if value < 10 or value > 150000:
                error = "Enter an amount between KSh 10 and KSh 150,000."
            elif not (phone.isdigit() and phone.startswith("254") and len(phone) == 12):
                error = "Enter a valid Kenyan M-Pesa number."
            else:
                shortcode = os.environ.get("MPESA_SHORTCODE")
                passkey = os.environ.get("MPESA_PASSKEY")
                callback_base = os.environ.get("MPESA_CALLBACK_BASE_URL", os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
                if not shortcode or not passkey or not callback_base:
                    error = "M-Pesa integration is not configured on the server yet."
                else:
                    token = daraja_token()
                    ts = mpesa_timestamp()
                    payload = {
                        "BusinessShortCode": shortcode,
                        "Password": mpesa_password(shortcode, passkey, ts),
                        "Timestamp": ts,
                        "TransactionType": os.environ.get("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
                        "Amount": int(value),
                        "PartyA": phone,
                        "PartyB": shortcode,
                        "PhoneNumber": phone,
                        "CallBackURL": callback_base + "/mpesa/callback",
                        "AccountReference": f"TRADEON-{session['user_id']}",
                        "TransactionDesc": "TRADEON wallet deposit"
                    }
                    base = os.environ.get("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke")
                    r = requests.post(base + "/mpesa/stkpush/v1/processrequest", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=30)
                    data = r.json()
                    if r.ok and data.get("ResponseCode") == "0":
                        with sqlite3.connect(DB) as con:
                            con.execute("INSERT INTO deposits (user_id,amount,phone,merchant_request_id,checkout_request_id,status) VALUES (?,?,?,?,?,?)", (session["user_id"], value, phone, data.get("MerchantRequestID"), data.get("CheckoutRequestID"), "PENDING"))
                            con.execute("INSERT OR IGNORE INTO wallets (user_id,balance) VALUES (?,0)", (session["user_id"],))
                        message = "M-Pesa prompt sent. Check your phone and enter your M-Pesa PIN. Your wallet will only be credited after Safaricom confirms the payment."
                    else:
                        error = data.get("errorMessage") or data.get("ResponseDescription") or "Safaricom could not start the payment."
        except (ValueError, requests.RequestException, RuntimeError, KeyError):
            error = "Unable to start the M-Pesa payment. Check the server configuration and try again."
    return render_template("deposit.html", error=error, message=message, amount=amount)

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json(silent=True) or {}
    callback = data.get("Body", {}).get("stkCallback", {})
    checkout_id = callback.get("CheckoutRequestID")
    result_code = callback.get("ResultCode")
    items = callback.get("CallbackMetadata", {}).get("Item", [])
    meta = {item.get("Name"): item.get("Value") for item in items if item.get("Name")}
    receipt = meta.get("MpesaReceiptNumber")
    amount = meta.get("Amount")
    if checkout_id:
        with sqlite3.connect(DB) as con:
            row = con.execute("SELECT id,user_id,amount,status FROM deposits WHERE checkout_request_id=?", (checkout_id,)).fetchone()
            if row and row[3] == "PENDING":
                if result_code == 0 and receipt and amount is not None and float(amount) == float(row[2]):
                    con.execute("UPDATE deposits SET status='SUCCESS', mpesa_receipt=? WHERE id=?", (receipt, row[0]))
                    con.execute("INSERT OR IGNORE INTO wallets (user_id,balance) VALUES (?,0)", (row[1],))
                    con.execute("UPDATE wallets SET balance=balance+? WHERE user_id=?", (float(amount), row[1]))
                elif result_code != 0:
                    con.execute("UPDATE deposits SET status='FAILED' WHERE id=?", (row[0],))
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@app.route("/trade")
def trade():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("trade.html", user_name=session.get("user_name"))

@app.route("/transactions")
def transactions():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("simple_page.html", title="Transactions", message="Your transaction history will appear here.")

@app.route("/profile")
def profile():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("simple_page.html", title="Profile", message="Your TRADEON profile will appear here.")

@app.route("/refer")
def refer():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("simple_page.html", title="Refer & Earn", message="Your referral link and earnings will appear here.")

@app.route("/how-to-trade")
def how_to_trade():
    if "user_id" not in session: return redirect(url_for("login"))
    return render_template("simple_page.html", title="How to Trade", message="Trading instructions will appear here.")
# ---------------- TRADEON MANAGER (ADMIN) ----------------
def admin_required():
    return session.get("admin_logged_in") is True

@app.route("/manager/login", methods=["GET", "POST"])
def manager_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if admin_password and username == admin_username and password == admin_password:
            session["admin_logged_in"] = True
            return redirect(url_for("manager_dashboard"))
        error = "Invalid manager login."
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>TRADEON Manager</title><style>body{font-family:Arial;background:#f3f6f9;display:grid;place-items:center;min-height:100vh}.card{background:white;padding:28px;border-radius:16px;width:min(420px,90%)}input,button{box-sizing:border-box;width:100%;padding:13px;margin:8px 0;border-radius:9px;border:1px solid #ccd3da}button{background:#111;color:white;border:0;font-weight:bold}.error{color:#b00020}</style></head><body><div class="card"><h1>TRADEON MANAGER</h1><p>Manager Login</p>""" + (f"<p class='error'>{error}</p>" if error else "") + """<form method="post"><input name="username" placeholder="Username" required><input name="password" type="password" placeholder="Password" required><button>Sign in</button></form></div></body></html>"""

@app.route("/manager/logout")
def manager_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("manager_login"))

@app.route("/manager")
def manager_dashboard():
    if not admin_required():
        return redirect(url_for("manager_login"))
    with sqlite3.connect(DB) as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        verified = con.execute("SELECT COUNT(*) FROM users WHERE verified=1").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM deposits WHERE status='PENDING'").fetchone()[0]
        successful = con.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status='SUCCESS'").fetchone()[0]
        deposits = con.execute("""
            SELECT d.id, u.name, d.phone, d.amount, d.status, d.mpesa_receipt, d.created_at
            FROM deposits d JOIN users u ON u.id=d.user_id
            ORDER BY d.id DESC LIMIT 50
        """).fetchall()
    rows = "".join(
        f"<tr><td>{d[0]}</td><td>{d[1]}</td><td>{d[2]}</td><td>KSh {d[3]:,.2f}</td><td>{d[4]}</td><td>{d[5] or '-'}</td><td>{d[6]}</td></tr>"
        for d in deposits
    )
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>TRADEON Manager</title><style>body{{font-family:Arial;margin:0;background:#f5f7fa;color:#111}}header{{background:#111;color:white;padding:18px 5%;display:flex;justify-content:space-between}}main{{padding:24px 5%}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.card{{background:white;padding:18px;border-radius:12px;box-shadow:0 2px 10px #0001}}table{{width:100%;border-collapse:collapse;background:white;margin-top:20px;font-size:13px}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left}}.tablewrap{{overflow:auto}}a{{color:white}}</style></head><body><header><b>TRADEON MANAGER</b><a href="/manager/logout">Logout</a></header><main><h2>Dashboard</h2><div class="cards"><div class="card"><b>Users</b><h2>{users}</h2></div><div class="card"><b>Verified</b><h2>{verified}</h2></div><div class="card"><b>Pending deposits</b><h2>{pending}</h2></div><div class="card"><b>Successful deposits</b><h2>KSh {successful:,.2f}</h2></div></div><h2>Recent deposits</h2><div class="tablewrap"><table><tr><th>ID</th><th>User</th><th>Phone</th><th>Amount</th><th>Status</th><th>Receipt</th><th>Date</th></tr>{rows or "<tr><td colspan='7'>No deposits yet.</td></tr>"}</table></div></main></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

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
        con.execute("""CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT NOT NULL, action TEXT NOT NULL, entry_price TEXT, stop_loss TEXT, take_profit TEXT, strength TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, active INTEGER NOT NULL DEFAULT 1)""")
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

@app.route("/signals")
def signals():
    if "user_id" not in session:
        return redirect(url_for("login"))
    with sqlite3.connect(DB) as con:
        rows = con.execute("""
            SELECT id,pair,action,entry_price,stop_loss,take_profit,strength,note,created_at
            FROM signals WHERE active=1 ORDER BY id DESC LIMIT 50
        """).fetchall()
    cards = ""
    for s in rows:
        action_class = "buy" if s[2] == "BUY" else "sell"
        cards += f"""<div class="card">
            <div class="top"><h2>{s[1]}</h2><span class="{action_class}">{s[2]}</span></div>
            <p><b>Strength:</b> {s[6]}</p>
            <p><b>Entry:</b> {s[3] or '-'} &nbsp; <b>Stop Loss:</b> {s[4] or '-'} &nbsp; <b>Take Profit:</b> {s[5] or '-'}</p>
            <p>{s[7] or ''}</p>
            <small>Published: {s[8]}</small>
        </div>"""
    if not cards:
        cards = "<div class='card'><h3>No active signals</h3><p>New signals will appear here when the TradeOn Manager publishes them.</p></div>"
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TRADEON Signals</title>
    <style>
    body{{font-family:Arial;margin:0;background:#f5f7fa;color:#111}}header{{background:#111;color:white;padding:18px 5%}}
    main{{padding:24px 5%;max-width:900px;margin:auto}}.card{{background:white;padding:18px;border-radius:12px;margin:12px 0;box-shadow:0 2px 8px #0001}}
    .top{{display:flex;justify-content:space-between;align-items:center}}.buy{{color:green;font-size:22px;font-weight:bold}}.sell{{color:#c00;font-size:22px;font-weight:bold}}
    a{{color:white;text-decoration:none}}
    </style></head><body><header><b>TRADEON — SIGNALS</b> &nbsp; <a href="/">Dashboard</a></header>
    <main><h1>Trading Signals</h1><p>Signals published by the TradeOn Manager. Trading involves risk.</p>{cards}</main></body></html>"""

@app.route("/manager/signals", methods=["GET", "POST"])
def manager_signals():
    if not admin_required():
        return redirect(url_for("manager_login"))
    error = None
    if request.method == "POST":
        pair = request.form.get("pair", "").strip().upper()
        action = request.form.get("action", "").strip().upper()
        entry = request.form.get("entry_price", "").strip()
        stop_loss = request.form.get("stop_loss", "").strip()
        take_profit = request.form.get("take_profit", "").strip()
        strength = request.form.get("strength", "Medium").strip()
        note = request.form.get("note", "").strip()
        if not pair or action not in ("BUY", "SELL"):
            error = "Enter a pair and choose BUY or SELL."
        else:
            with sqlite3.connect(DB) as con:
                con.execute("""INSERT INTO signals
                    (pair,action,entry_price,stop_loss,take_profit,strength,note,active)
                    VALUES (?,?,?,?,?,?,?,1)""",
                    (pair, action, entry, stop_loss, take_profit, strength, note))
            return redirect(url_for("manager_signals"))

    with sqlite3.connect(DB) as con:
        rows = con.execute("""SELECT id,pair,action,entry_price,stop_loss,take_profit,strength,note,created_at,active
                              FROM signals ORDER BY id DESC LIMIT 100""").fetchall()
    items = ""
    for s in rows:
        items += f"""<tr><td>{s[0]}</td><td>{s[1]}</td><td>{s[2]}</td><td>{s[3] or '-'}</td>
        <td>{s[4] or '-'}</td><td>{s[5] or '-'}</td><td>{s[6]}</td><td>{'Active' if s[9] else 'Hidden'}</td>
        <td><form method="post" action="/manager/signals/{s[0]}/toggle"><button>{'Hide' if s[9] else 'Publish'}</button></form></td></tr>"""
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Manage Signals</title><style>
    body{{font-family:Arial;margin:0;background:#f5f7fa}}header{{background:#111;color:white;padding:18px 5%}}
    main{{padding:24px 5%}}.card{{background:white;padding:18px;border-radius:12px;margin-bottom:18px}}
    input,select,textarea,button{{box-sizing:border-box;width:100%;padding:12px;margin:6px 0;border-radius:8px;border:1px solid #ccc}}
    button{{background:#111;color:white;font-weight:bold}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
    .wrap{{overflow:auto}}table{{width:100%;min-width:850px;border-collapse:collapse;background:white}}th,td{{padding:9px;border-bottom:1px solid #eee;text-align:left;font-size:13px}}
    a{{color:white;text-decoration:none}}.error{{color:#b00020;font-weight:bold}}
    </style></head><body><header><b>TRADEON MANAGER — SIGNALS</b> &nbsp; <a href="/manager">Dashboard</a></header>
    <main><h2>Create Signal</h2><div class="card">{("<p class='error'>"+error+"</p>") if error else ""}
    <form method="post">
      <div class="grid"><input name="pair" placeholder="Pair e.g. BTC/USDT" required>
      <select name="action"><option value="BUY">BUY</option><option value="SELL">SELL</option></select></div>
      <div class="grid"><input name="entry_price" placeholder="Entry price"><input name="stop_loss" placeholder="Stop loss"></div>
      <div class="grid"><input name="take_profit" placeholder="Take profit">
      <select name="strength"><option>Low</option><option selected>Medium</option><option>High</option></select></div>
      <textarea name="note" rows="3" placeholder="Signal note / reason"></textarea>
      <button>Publish Signal</button>
    </form></div>
    <h2>Published Signals</h2><div class="wrap"><table><tr><th>ID</th><th>Pair</th><th>Action</th><th>Entry</th><th>Stop</th><th>Take</th><th>Strength</th><th>Status</th><th>Action</th></tr>{items or "<tr><td colspan='9'>No signals yet.</td></tr>"}</table></div>
    </main></body></html>"""

@app.route("/manager/signals/<int:signal_id>/toggle", methods=["POST"])
def manager_signal_toggle(signal_id):
    if not admin_required():
        return redirect(url_for("manager_login"))
    with sqlite3.connect(DB) as con:
        con.execute("UPDATE signals SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (signal_id,))
    return redirect(url_for("manager_signals"))

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

@app.route("/manager/users")
def manager_users():
    if not admin_required():
        return redirect(url_for("manager_login"))
    q = request.args.get("q", "").strip()
    with sqlite3.connect(DB) as con:
        like = "%" + q + "%"
        if q:
            users = con.execute("""SELECT u.id,u.name,u.phone,u.verified,COALESCE(w.balance,0),(SELECT COUNT(*) FROM deposits d WHERE d.user_id=u.id),(SELECT COALESCE(SUM(d.amount),0) FROM deposits d WHERE d.user_id=u.id AND d.status='SUCCESS') FROM users u LEFT JOIN wallets w ON w.user_id=u.id WHERE u.name LIKE ? OR u.phone LIKE ? ORDER BY u.id DESC""",(like,like)).fetchall()
        else:
            users = con.execute("""SELECT u.id,u.name,u.phone,u.verified,COALESCE(w.balance,0),(SELECT COUNT(*) FROM deposits d WHERE d.user_id=u.id),(SELECT COALESCE(SUM(d.amount),0) FROM deposits d WHERE d.user_id=u.id AND d.status='SUCCESS') FROM users u LEFT JOIN wallets w ON w.user_id=u.id ORDER BY u.id DESC""").fetchall()
    rows="".join(f"<tr><td>{u[0]}</td><td>{u[1]}</td><td>{u[2]}</td><td>{'Verified' if u[3] else 'Unverified'}</td><td>KSh {u[4]:,.2f}</td><td>{u[5]}</td><td>KSh {u[6]:,.2f}</td><td><a href='/manager/users/{u[0]}'>View</a></td></tr>" for u in users)
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Users</title><style>body{{font-family:Arial;margin:0;background:#f5f7fa}}header{{background:#111;color:white;padding:18px 5%}}main{{padding:24px 5%}}input,button{{padding:12px;border-radius:8px;border:1px solid #ccc}}button{{background:#111;color:white}}.wrap{{overflow:auto}}table{{width:100%;min-width:800px;border-collapse:collapse;background:white}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left;font-size:13px}}a{{background:#111;color:white;padding:7px 10px;border-radius:7px;text-decoration:none}}</style></head><body><header><b>TRADEON MANAGER — USERS</b> &nbsp; <a href="/manager">Dashboard</a></header><main><h2>Users Management</h2><form method="get"><input name="q" value="{q}" placeholder="Search name or phone"><button>Search</button></form><p>{len(users)} user(s) found.</p><div class="wrap"><table><tr><th>ID</th><th>Name</th><th>Phone</th><th>Verification</th><th>Wallet</th><th>Deposits</th><th>Total deposited</th><th>Action</th></tr>{rows or "<tr><td colspan='8'>No users found.</td></tr>"}</table></div></main></body></html>"""

@app.route("/manager/users/<int:user_id>")
def manager_user_detail(user_id):
    if not admin_required():
        return redirect(url_for("manager_login"))
    with sqlite3.connect(DB) as con:
        user=con.execute("SELECT id,name,phone,verified FROM users WHERE id=?",(user_id,)).fetchone()
        wallet=con.execute("SELECT COALESCE(balance,0) FROM wallets WHERE user_id=?",(user_id,)).fetchone()
        deposits=con.execute("SELECT id,amount,status,mpesa_receipt,created_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 50",(user_id,)).fetchall()
    if not user: return "User not found",404
    rows="".join(f"<tr><td>{d[0]}</td><td>KSh {d[1]:,.2f}</td><td>{d[2]}</td><td>{d[3] or '-'}</td><td>{d[4]}</td></tr>" for d in deposits)
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>User details</title><style>body{{font-family:Arial;margin:0;background:#f5f7fa}}header{{background:#111;color:white;padding:18px 5%}}main{{padding:24px 5%}}.card{{background:white;padding:18px;border-radius:12px;margin-bottom:18px}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left;font-size:13px}}a{{color:white}}</style></head><body><header><b>TRADEON MANAGER — USER DETAILS</b> &nbsp; <a href="/manager/users">Users</a></header><main><div class="card"><h2>{user[1]}</h2><p>User ID: {user[0]}</p><p>Phone: {user[2]}</p><p>Verification: {'Verified' if user[3] else 'Unverified'}</p><p>Wallet balance: KSh {(wallet[0] if wallet else 0):,.2f}</p></div><h2>Deposit history</h2><table><tr><th>ID</th><th>Amount</th><th>Status</th><th>Receipt</th><th>Date</th></tr>{rows or "<tr><td colspan='5'>No deposits yet.</td></tr>"}</table></main></body></html>"""

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
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>TRADEON Manager</title><style>body{{font-family:Arial;margin:0;background:#f5f7fa;color:#111}}header{{background:#111;color:white;padding:18px 5%;display:flex;justify-content:space-between}}main{{padding:24px 5%}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.card{{background:white;padding:18px;border-radius:12px;box-shadow:0 2px 10px #0001}}table{{width:100%;border-collapse:collapse;background:white;margin-top:20px;font-size:13px}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left}}.tablewrap{{overflow:auto}}a{{color:white}}</style></head><body><header><b>TRADEON MANAGER</b><a href="/manager/logout">Logout</a></header><main><p><a href="/manager/users" style="display:inline-block;background:#111;color:white;padding:10px 14px;border-radius:8px;text-decoration:none">👥 Manage Users</a></p><p><a href="/manager/signals" style="display:inline-block;background:#111;color:white;padding:10px 14px;border-radius:8px;text-decoration:none">📈 Create & Manage Signals</a></p><h2>Dashboard</h2><div class="cards"><div class="card"><b>Users</b><h2>{users}</h2></div><div class="card"><b>Verified</b><h2>{verified}</h2></div><div class="card"><b>Pending deposits</b><h2>{pending}</h2></div><div class="card"><b>Successful deposits</b><h2>KSh {successful:,.2f}</h2></div></div><h2>Recent deposits</h2><div class="tablewrap"><table><tr><th>ID</th><th>User</th><th>Phone</th><th>Amount</th><th>Status</th><th>Receipt</th><th>Date</th></tr>{rows or "<tr><td colspan='7'>No deposits yet.</td></tr>"}</table></div></main></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

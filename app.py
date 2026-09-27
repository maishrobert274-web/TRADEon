from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, random, time, requests

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

@app.route("/deposit", methods=["GET", "POST"])
def deposit():
    if "user_id" not in session:
        return redirect(url_for("login"))
    error = None
    amount = request.form.get("amount", "").strip() if request.method == "POST" else ""
    if request.method == "POST":
        try:
            value = float(amount)
            if value < 10:
                error = "Minimum deposit is KSh 10."
            elif value > 150000:
                error = "Maximum deposit is KSh 150,000 per request."
            else:
                return render_template("deposit.html", amount=f"{value:,.2f}", message="M-Pesa payment integration will be connected next. This is currently a demo deposit screen.")
        except ValueError:
            error = "Enter a valid KSh amount."
    return render_template("deposit.html", error=error, amount=amount)

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

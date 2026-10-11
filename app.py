from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, requests, base64
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
        if "country" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN country TEXT NOT NULL DEFAULT 'Kenya'")
        con.execute("""CREATE TABLE IF NOT EXISTS deposits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, phone TEXT NOT NULL, merchant_request_id TEXT, checkout_request_id TEXT UNIQUE, mpesa_receipt TEXT, status TEXT NOT NULL DEFAULT "PENDING", created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS wallets (user_id INTEGER PRIMARY KEY, balance REAL NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS demo_wallets (user_id INTEGER PRIMARY KEY, balance REAL NOT NULL DEFAULT 10000)""")
        con.execute("""CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, pair TEXT NOT NULL, side TEXT NOT NULL, amount REAL NOT NULL, entry_price REAL NOT NULL, status TEXT NOT NULL DEFAULT "OPEN", pnl REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, closed_at TEXT)""")
        trade_cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
        for column, definition in [("mode", "TEXT NOT NULL DEFAULT 'SPOT'"), ("duration_seconds", "INTEGER"), ("expires_at", "TEXT"), ("settlement_price", "REAL")]:
            if column not in trade_cols:
                con.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
        con.execute("""CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT NOT NULL, action TEXT NOT NULL, entry_price TEXT, stop_loss TEXT, take_profit TEXT, strength TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, active INTEGER NOT NULL DEFAULT 1)""")
init_db()

def normalize_phone(phone):
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("07") or p.startswith("01"):
        return "+254" + p[1:]
    if p.startswith("254"):
        return "+" + p
    return p

@app.route("/")
def home():
    # Keep one signed-in home: the trading terminal. Public visitors see the
    # welcome page until they register or log in.
    if "user_id" in session:
        return redirect(url_for("trade"))
    return render_template("public_home.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = normalize_phone(request.form.get("phone", ""))
        country = request.form.get("country", "").strip()
        password = request.form.get("password", "")
        allowed_countries = {"Kenya", "Uganda", "Tanzania", "Rwanda", "Nigeria", "Ghana", "South Africa", "United States", "United Kingdom", "Canada", "Australia", "India", "Pakistan", "Bangladesh", "Philippines", "Indonesia", "Malaysia", "Singapore", "United Arab Emirates", "Saudi Arabia", "Germany", "France", "Italy", "Spain", "Netherlands", "Brazil", "Mexico", "Japan", "South Korea", "Turkey", "Egypt", "Other"}
        if not name or not phone or not country or len(password) < 6:
            error = "Enter your name, country, international phone number and a password of at least 6 characters."
        elif country not in allowed_countries:
            error = "Please choose a country from the list."
        elif not phone.startswith("+") or len("".join(ch for ch in phone if ch.isdigit())) < 8 or len("".join(ch for ch in phone if ch.isdigit())) > 15:
            error = "Enter your phone number with its international country code, for example +254712345678."
        else:
            try:
                with sqlite3.connect(DB) as con:
                    cur = con.execute(
                        "INSERT INTO users (name,phone,password,verified,country) VALUES (?,?,?,0,?)",
                        (name, phone, generate_password_hash(password), country)
                    )
                    user_id = cur.lastrowid
                # Phone verification is disabled for simple registration.
                # The phone number is used as the account login identifier.
                with sqlite3.connect(DB) as con:
                    con.execute("UPDATE users SET verified=1 WHERE id=?", (user_id,))
                    con.execute("INSERT OR IGNORE INTO wallets (user_id,balance) VALUES (?,0)", (user_id,))
                    user = con.execute("SELECT id,name FROM users WHERE id=?", (user_id,)).fetchone()
                session["user_id"] = user[0]
                session["user_name"] = user[1]
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                error = "That phone number is already registered."
    return render_template("register.html", error=error)

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

@app.route("/api/trade", methods=["POST"])
def api_trade():
    if "user_id" not in session:
        return {"error": "login required"}, 401
    data = request.get_json(silent=True) or {}
    pair = str(data.get("pair", "")).upper()
    side = str(data.get("side", "")).upper()
    mode = str(data.get("mode", "SPOT")).upper()
    try:
        amount = float(data.get("amount", 0))
        duration = int(data.get("duration_seconds", 0))
    except (TypeError, ValueError):
        amount, duration = 0, 0
    symbols = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT"}
    allowed_sides = ("UP", "DOWN") if mode == "DURATION" else ("BUY", "SELL")
    if pair not in symbols or side not in allowed_sides or amount < 10 or mode not in ("SPOT", "DURATION"):
        return {"error": "Invalid demo trade details. Minimum is KSh 10."}, 400
    if mode == "DURATION" and duration not in (30, 60, 300, 900):
        return {"error": "Choose a supported demo duration."}, 400
    try:
        response = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": symbols[pair]}, timeout=(1.5, 3))
        response.raise_for_status()
        price = float(response.json()["price"])
    except (requests.RequestException, KeyError, ValueError):
        return {"error": "Could not get the current market price."}, 503
    db_side = ("BUY" if side == "UP" else "SELL") if mode == "DURATION" else side
    with sqlite3.connect(DB) as con:
        con.execute("INSERT OR IGNORE INTO demo_wallets (user_id,balance) VALUES (?,10000)", (session["user_id"],))
        balance = con.execute("SELECT balance FROM demo_wallets WHERE user_id=?", (session["user_id"],)).fetchone()[0]
        if amount > balance:
            return {"error": "Insufficient demo balance. Available: KSh %.2f" % balance}, 400
        con.execute("UPDATE demo_wallets SET balance=balance-? WHERE user_id=?", (amount, session["user_id"]))
        cur = con.execute("INSERT INTO trades (user_id,pair,side,amount,entry_price,status,mode,duration_seconds,expires_at) VALUES (?,?,?,?,?,'OPEN',?,?,CASE WHEN ? > 0 THEN datetime('now', '+' || ? || ' seconds') ELSE NULL END)", (session["user_id"], pair, db_side, amount, price, mode, duration if mode == "DURATION" else None, duration if mode == "DURATION" else 0, duration if mode == "DURATION" else 0))
        trade_id = cur.lastrowid
    return {"ok": True, "trade_id": trade_id, "pair": pair, "side": side, "mode": mode, "duration_seconds": duration if mode == "DURATION" else None, "entry_price": price, "status": "OPEN"}

@app.route("/api/trades/settle-due", methods=["POST"])
def settle_due_demo_trades():
    if "user_id" not in session:
        return {"error": "login required"}, 401
    symbols = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT"}
    with sqlite3.connect(DB) as con:
        due = con.execute("SELECT id,pair,side,amount,entry_price FROM trades WHERE user_id=? AND mode='DURATION' AND status='OPEN' AND expires_at<=CURRENT_TIMESTAMP", (session["user_id"],)).fetchall()
        results = []
        for row in due:
            if row[1] not in symbols:
                continue
            try:
                response = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": symbols[row[1]]}, timeout=(1.5, 3))
                response.raise_for_status()
                exit_price = float(response.json()["price"])
            except (requests.RequestException, KeyError, ValueError):
                continue
            entry_price, amount = float(row[4]), float(row[3])
            won = (row[2] == "BUY" and exit_price > entry_price) or (row[2] == "SELL" and exit_price < entry_price)
            pnl = round(amount * 0.8, 2) if won else -round(amount, 2)
            con.execute("UPDATE trades SET status='CLOSED',pnl=?,closed_at=CURRENT_TIMESTAMP,settlement_price=? WHERE id=? AND user_id=? AND status='OPEN'", (pnl, exit_price, row[0], session["user_id"]))
            con.execute("UPDATE demo_wallets SET balance=balance+? WHERE user_id=?", (amount + pnl if won else 0, session["user_id"]))
            results.append({"trade_id": row[0], "won": won, "pnl": pnl, "exit_price": exit_price})
    return {"ok": True, "settled": results}

@app.route("/trades/<int:trade_id>/close", methods=["POST"])
def close_demo_trade(trade_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    symbols = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT"}
    with sqlite3.connect(DB) as con:
        row = con.execute("SELECT pair,side,amount,entry_price,status FROM trades WHERE id=? AND user_id=?", (trade_id, session["user_id"])).fetchone()
        if not row or row[4] != "OPEN" or row[0] not in symbols:
            return redirect(url_for("trades"))
        try:
            response = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": symbols[row[0]]}, timeout=(1.5, 3))
            response.raise_for_status()
            exit_price = float(response.json()["price"])
        except (requests.RequestException, KeyError, ValueError):
            return redirect(url_for("trades") + "?close=price_unavailable")
        entry_price, amount = float(row[3]), float(row[2])
        direction = 1 if row[1] == "BUY" else -1
        pnl = round(amount * ((exit_price - entry_price) / entry_price) * direction, 2) if entry_price else 0
        con.execute("UPDATE trades SET status='CLOSED',pnl=?,closed_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='OPEN'", (pnl, trade_id, session["user_id"]))
        con.execute("INSERT OR IGNORE INTO demo_wallets (user_id,balance) VALUES (?,10000)", (session["user_id"],))
        con.execute("UPDATE demo_wallets SET balance=balance+? WHERE user_id=?", (amount + pnl, session["user_id"]))
    return redirect(url_for("trades") + "?closed=1")

MARKET_CACHE = {}
MARKET_CACHE_SECONDS = 15

@app.route("/api/market/history")
def market_history():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    interval = request.args.get("interval", "1h").lower()
    products = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "SOLUSDT": "SOL-USD"}
    granularities = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
    if symbol not in products:
        return {"error": "unsupported symbol"}, 400
    if interval not in granularities:
        return {"error": "unsupported interval"}, 400
    try:
        response = requests.get(
            f"https://api.exchange.coinbase.com/products/{products[symbol]}/candles",
            params={"granularity": granularities[interval]},
            headers={"User-Agent": "TRADEON-price-chart"},
            timeout=(1.5, 3)
        )
        response.raise_for_status()
        candles = sorted(response.json(), key=lambda candle: candle[0])
        # Coinbase rows: [time, low, high, open, close, volume]
        points = [{"time": int(c[0]), "low": float(c[1]), "high": float(c[2]),
                   "open": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
                   "price": float(c[4])} for c in candles[-40:]]
        if len(points) < 2:
            return {"error": "Not enough chart data yet."}, 503
        return {"symbol": symbol, "interval": interval, "points": points, "source": "Coinbase"}
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {"error": "Price chart data is temporarily unavailable."}, 503

@app.route("/api/market")
def market_price():
    # Public market data is cached briefly to reduce repeated network calls.
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    allowed = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    if symbol not in allowed:
        return {"error": "unsupported symbol"}, 400
    cached = MARKET_CACHE.get(symbol)
    now = datetime.now().timestamp()
    if cached and now - cached["cached_at"] < MARKET_CACHE_SECONDS:
        return {key: value for key, value in cached.items() if key != "cached_at"}
    try:
        response = requests.get(
            "https://data-api.binance.vision/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=(1.5, 2.5)
        )
        response.raise_for_status()
        data = response.json()
        result = {
            "symbol": symbol,
            "price": float(data["lastPrice"]),
            "change_percent": float(data["priceChangePercent"]),
            "source": "Binance"
        }
        MARKET_CACHE[symbol] = {**result, "cached_at": datetime.now().timestamp()}
        return result
    except (requests.RequestException, KeyError, ValueError):
        # Quickly fall back if Binance is blocked or slow from the hosting service.
        products = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "SOLUSDT": "SOL-USD"}
        try:
            response = requests.get(
                f"https://api.exchange.coinbase.com/products/{products[symbol]}/stats",
                headers={"User-Agent": "TRADEON-market-price"},
                timeout=(1.5, 2.5)
            )
            response.raise_for_status()
            data = response.json()
            last = float(data["last"])
            open_price = float(data["open"])
            change = ((last - open_price) / open_price * 100) if open_price else 0
            result = {"symbol": symbol, "price": last, "change_percent": change, "source": "Coinbase (USD estimate)"}
            MARKET_CACHE[symbol] = {**result, "cached_at": datetime.now().timestamp()}
            return result
        except (requests.RequestException, KeyError, ValueError):
            return {"error": "Market data is temporarily unavailable from both providers."}, 503

@app.route("/signals")
def signals():
    # Published signals are public; manager drafts remain hidden by active=1.
    with sqlite3.connect(DB) as con:
        rows = con.execute("""
            SELECT id,pair,action,entry_price,stop_loss,take_profit,strength,note,created_at
            FROM signals WHERE active=1 ORDER BY id DESC LIMIT 50
        """).fetchall()
    cards = ""
    for s in rows:
        action_class = "buy" if s[2] == "BUY" else "sell"
        cards += f"""<div class="card">
            <div class="signal-top"><h2>{s[1]}</h2><span class="{action_class}">{s[2]}</span></div>
            <div class="details"><div class="detail">Entry<b>{s[3] or '-'}</b></div><div class="detail">Stop loss<b>{s[4] or '-'}</b></div><div class="detail">Take profit<b>{s[5] or '-'}</b></div></div>
            <p class="note"><b>Strength:</b> {s[6]}<br>{s[7] or ''}</p>
            <small>Published: {s[8]}</small>
        </div>"""
    if not cards:
        cards = "<div class='card'><h3>No active signals</h3><p>New signals will appear here when the TradeOn Manager publishes them.</p></div>"
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TRADEON Signals</title>
    <style>
    *{{box-sizing:border-box}}body{{font-family:Arial,sans-serif;margin:0;background:#0b0e11;color:#e8edf2}}
    header{{background:#151a21;border-bottom:1px solid #29313a;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px;position:sticky;top:0}}
    .brand{{font-size:20px;font-weight:900;letter-spacing:.4px}}.brand span{{color:#20c997}}header a{{color:#c5d0dc;text-decoration:none;font-size:13px}}
    main{{padding:16px 16px 100px;max-width:1000px;margin:auto}}h1{{font-size:22px;margin:8px 0}}.intro{{color:#8c98a7;font-size:13px;margin-bottom:16px}}
    .card{{background:#151a21;border:1px solid #29313a;padding:16px;border-radius:12px;margin:12px 0}}
    .signal-top{{display:flex;justify-content:space-between;align-items:center;gap:10px}}.signal-top h2{{font-size:17px;margin:0}}
    .buy,.sell{{font-size:13px;font-weight:900;padding:7px 10px;border-radius:7px}}.buy{{color:#a5f3c4;background:#183b31}}.sell{{color:#ffb4b4;background:#482426}}
    .details{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:12px 0}}.detail{{background:#0e1319;border:1px solid #29313a;border-radius:8px;padding:10px;font-size:12px;color:#9aa5b1}}.detail b{{display:block;color:#e8edf2;margin-top:5px;overflow-wrap:anywhere}}
    .note{{font-size:13px;line-height:1.5}}small{{color:#8c98a7}}.shortcut-grid{{position:fixed;left:0;right:0;bottom:0;z-index:20;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:8px 10px calc(8px + env(safe-area-inset-bottom,0px));background:#10151b;border-top:1px solid #29313a;max-width:none;margin:0 auto;box-shadow:0 -6px 20px #0005}}
    .shortcut{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;min-height:54px;text-decoration:none;color:#dce5ed;background:#202833;border:1px solid #303b48;border-radius:9px;padding:8px 3px;font-size:11px;font-weight:800}}
    .shortcut-icon{{font-size:17px;color:#20c997}}@media(max-width:500px){{main{{padding:10px}}.card{{padding:12px}}.details{{gap:5px}}.detail{{padding:7px 5px;font-size:10px}}.signal-top h2{{font-size:15px}}header{{padding:12px 10px}}.brand{{font-size:18px}}}}
    </style></head><body><header><div class="brand">TRADE<span>ON</span></div><a href="/trade">← Back to trading</a></header>
    <main><h1>Insights</h1><p class="intro">Signals published by the TRADEON Manager. Market signals are informational and are not guaranteed results.</p>{cards}</main>
    <nav class="shortcut-grid" aria-label="Main sections">
    <a class="shortcut" href="/trades"><span class="shortcut-icon">↔</span><span>Trades</span></a>
    <a class="shortcut" href="/signals"><span class="shortcut-icon">◈</span><span>Insights</span></a>
    <a class="shortcut" href="/social-trading"><span class="shortcut-icon">◎</span><span>Social Trading</span></a>
    <a class="shortcut" href="/deposit"><span class="shortcut-icon">＋</span><span>Deposit</span></a></nav></body></html>"""


@app.route("/social-trading")
def social_trading():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TRADEON — Social Trading</title><style>
    *{box-sizing:border-box}body{margin:0;background:#0b0e11;color:#e8edf2;font-family:Arial,sans-serif}
    header{background:#151a21;border-bottom:1px solid #29313a;padding:14px 16px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0}
    .brand{font-size:20px;font-weight:900}.brand span{color:#20c997}a{color:#c5d0dc;text-decoration:none;font-size:13px}
    main{max-width:900px;margin:auto;padding:16px 16px 100px}.muted{color:#8c98a7;font-size:13px;line-height:1.5}
    .card{background:#151a21;border:1px solid #29313a;border-radius:12px;padding:16px;margin:12px 0}
    .card h2{font-size:17px;margin:0 0 8px}.pill{display:inline-block;background:#183b31;color:#a5f3c4;border-radius:6px;padding:6px 9px;font-size:11px;font-weight:bold}
    .shortcut-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:10px;max-width:900px;margin:0 auto 14px}
    .shortcut{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;min-height:54px;text-decoration:none;color:#dce5ed;background:#202833;border:1px solid #303b48;border-radius:9px;padding:8px 3px;font-size:11px;font-weight:800}
    .shortcut-icon{font-size:17px;color:#20c997}@media(max-width:500px){header{padding:12px 10px}.brand{font-size:18px}main{padding:10px}.card{padding:13px}}
    </style></head><body><header><div class="brand">TRADE<span>ON</span></div><a href="/trade">← Back to trading</a></header>
    <main><h1>Social Trading</h1><p class="muted">Follow TRADEON market discussions and learn how other traders analyse the market.</p>
    <div class="card"><span class="pill">COMING NEXT</span><h2>Trader community</h2><p class="muted">The community feed and trader profiles are not active yet. For now, use Insights to view signals published by the TRADEON Manager.</p><a href="/signals">Open Insights →</a></div>
    <div class="card"><h2>Stay in control</h2><p class="muted">Signals and demo trades are for learning only. Never assume another trader's results will repeat.</p></div></main>
    <nav class="shortcut-grid" aria-label="Main sections"><a class="shortcut" href="/trades"><span class="shortcut-icon">↔</span><span>Trades</span></a><a class="shortcut" href="/signals"><span class="shortcut-icon">◈</span><span>Insights</span></a><a class="shortcut" href="/social-trading"><span class="shortcut-icon">◎</span><span>Social Trading</span></a><a class="shortcut" href="/deposit"><span class="shortcut-icon">＋</span><span>Deposit</span></a></nav></body></html>"""

@app.route("/manager/auto-signals", methods=["POST"])
def manager_auto_signals():
    if not admin_required():
        return redirect(url_for("manager_login"))
    pair = request.form.get("pair", "BTC/USDT").upper()
    symbols = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT"}
    if pair not in symbols:
        return redirect(url_for("manager_signals") + "?auto=unsupported")
    try:
        try:
            response = requests.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={"symbol": symbols[pair], "interval": "1h", "limit": 60},
                timeout=15
            )
            response.raise_for_status()
            candles = response.json()
            closes = [float(candle[4]) for candle in candles[:-1]]
        except requests.RequestException as binance_error:
            # Render may be blocked by Binance. Fall back to Coinbase's free public hourly candles.
            coinbase_products = {
                "BTC/USDT": "BTC-USD",
                "ETH/USDT": "ETH-USD",
                "SOL/USDT": "SOL-USD"
            }
            response = requests.get(
                f"https://api.exchange.coinbase.com/products/{coinbase_products[pair]}/candles",
                params={"granularity": 3600},
                headers={"User-Agent": "TRADEON-market-signal-test"},
                timeout=15
            )
            response.raise_for_status()
            candles = sorted(response.json(), key=lambda candle: candle[0])
            closes = [float(candle[4]) for candle in candles[:-1]]
            app.logger.warning(
                "Binance rejected market data; Coinbase public candles used instead: %s",
                binance_error
            )
        if len(closes) < 30:
            return redirect(url_for("manager_signals") + "?auto=data_error")
        def ema(values, period):
            alpha = 2 / (period + 1)
            result = values[0]
            for value in values[1:]:
                result = value * alpha + result * (1 - alpha)
            return result
        def rsi(values, period=14):
            changes = [values[i] - values[i - 1] for i in range(1, len(values))]
            gains = [max(change, 0) for change in changes[-period:]]
            losses = [max(-change, 0) for change in changes[-period:]]
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            if avg_loss == 0:
                return 100.0
            return 100 - (100 / (1 + avg_gain / avg_loss))
        price = closes[-1]
        fast_now, slow_now = ema(closes[-9:], 9), ema(closes[-21:], 21)
        fast_prev, slow_prev = ema(closes[-10:-1], 9), ema(closes[-22:-1], 21)
        momentum = rsi(closes)
        action = None
        if fast_prev <= slow_prev and fast_now > slow_now and 40 <= momentum <= 68:
            action = "BUY"
        elif fast_prev >= slow_prev and fast_now < slow_now and 32 <= momentum <= 60:
            action = "SELL"
        if not action:
            return redirect(url_for("manager_signals") + "?auto=no_setup")
        if action == "BUY":
            stop, target = price * 0.985, price * 1.03
        else:
            stop, target = price * 1.015, price * 0.97
        note = (
            f"TEST MODE — automatically generated from Binance 1h EMA(9/21) crossover "
            f"and RSI({momentum:.1f}). Review before publishing; not financial advice."
        )
        with sqlite3.connect(DB) as con:
            con.execute(
                """INSERT INTO signals (pair,action,entry_price,stop_loss,take_profit,strength,note,active)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (pair, action, f"{price:.8f}", f"{stop:.8f}", f"{target:.8f}", "Medium", note)
            )
        return redirect(url_for("manager_signals") + "?auto=draft")
    except (requests.RequestException, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        app.logger.exception("Automatic signal generation failed: %s", exc)
        return redirect(url_for("manager_signals") + "?auto=data_error")

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
    <main><h2>Automatic Signal Generator (TEST MODE)</h2>{("<div class=\"card error\">Market data could not be loaded. Please try again later.</div>" if request.args.get("auto") == "data_error" else "<div class=\"card\">No clear EMA/RSI setup was found. Try again later.</div>" if request.args.get("auto") == "no_setup" else "<div class=\"card\">Test signal saved as a hidden draft. Review it below before publishing.</div>" if request.args.get("auto") == "draft" else "")}<div class="card"><p>This checks Binance 1-hour candles using EMA and RSI. Generated signals are saved as hidden drafts and will not be visible to users until you publish them.</p><form method="post" action="/manager/auto-signals"><select name="pair"><option>BTC/USDT</option><option>ETH/USDT</option><option>SOL/USDT</option></select><button>Analyse Market & Generate Test Signal</button></form><p>After analysis, check the signal list below. New automatic signals start as Hidden drafts.</p></div><h2>Create Signal Manually</h2><div class="card">{("<p class='error'>"+error+"</p>") if error else ""}
    <form method="post">
      <div class="grid"><select name="pair" required><option value="BTC/USD">BTC/USD</option><option value="BTC/USDT">BTC/USDT</option><option value="ETH/USD">ETH/USD</option><option value="ETH/USDT">ETH/USDT</option><option value="SOL/USDT">SOL/USDT</option><option value="BNB/USDT">BNB/USDT</option><option value="XRP/USDT">XRP/USDT</option></select>
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
    if "user_id" not in session:
        return redirect(url_for("login"))
    with sqlite3.connect(DB) as con:
        con.execute("INSERT OR IGNORE INTO demo_wallets (user_id,balance) VALUES (?,10000)", (session["user_id"],))
        demo_balance = con.execute("SELECT balance FROM demo_wallets WHERE user_id=?", (session["user_id"],)).fetchone()[0]
    return render_template("trade.html", user_name=session.get("user_name"), demo_balance=demo_balance)

@app.route("/trades")
def trades():
    if "user_id" not in session:
        return redirect(url_for("login"))
    with sqlite3.connect(DB) as con:
        rows = con.execute(
            "SELECT id,pair,side,amount,entry_price,status,pnl,created_at FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (session["user_id"],)
        ).fetchall()
    rendered_rows = []
    for r in rows:
        action = (
            f'<form method="post" action="/trades/{r[0]}/close"><button type="submit">Close demo trade</button></form>'
            if r[5] == "OPEN" else "Closed"
        )
        pnl = float(r[6] or 0)
        pnl_color = "#12b76a" if pnl >= 0 else "#d92d20"
        rendered_rows.append(
            f"<tr><td>{r[1]}</td><td>{r[2]}</td><td>KSh {r[3]:,.2f}</td>"
            f"<td>{r[4]:,.8f}</td><td>{r[5]}</td>"
            f"<td style='color:{pnl_color}'>KSh {pnl:,.2f}</td><td>{r[7]}</td><td>{action}</td></tr>"
        )
    table_rows = "".join(rendered_rows)
    notice = ""
    if request.args.get("closed") == "1":
        notice = "<p class='notice'>Demo trade closed and simulated P/L recorded.</p>"
    elif request.args.get("close") == "price_unavailable":
        notice = "<p class='notice error'>Market price unavailable. Your demo trade remains open; please try again.</p>"
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>TRADEON Trades</title>
    <style>*{{box-sizing:border-box}}body{{font-family:Arial;margin:0;background:#0b0e11;color:#e8edf2}}header{{background:#151a21;color:#fff;padding:18px;border-bottom:1px solid #29313a}}main{{max-width:1100px;margin:auto;padding:18px 18px 110px}}.wrap{{overflow:auto;background:#151a21;border:1px solid #29313a;border-radius:10px}}table{{width:100%;min-width:900px;border-collapse:collapse;background:#151a21}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #29313a;font-size:13px}}a{{color:#20c997}}button{{background:#159f6b;color:white;border:0;padding:9px 11px;border-radius:7px;font-weight:700}}.notice{{padding:12px;background:#183b31;border-radius:8px;color:#a5f3c4}}.error{{background:#482426;color:#ffb4b4}}.bottom-nav{{position:fixed;left:0;right:0;bottom:0;z-index:20;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;padding:8px 10px calc(8px + env(safe-area-inset-bottom,0px));background:#10151b;border-top:1px solid #29313a;box-shadow:0 -6px 20px #0005}}.bottom-nav a{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;min-height:48px;border:1px solid #303b48;border-radius:9px;background:#202833;color:#dce5ed;text-decoration:none;font-size:10px;font-weight:800}}.bottom-nav span{{font-size:16px;color:#20c997}}</style></head>
    <body><header><b>TRADEON — MY DEMO TRADES</b></header><main><p><a href="/trade">← Back to trading</a> · <a href="/">Dashboard</a></p><h2>My demo trades</h2>
    <p>Orders shown here are simulated and are not sent to a cryptocurrency exchange. P/L is illustrative and does not represent real money.</p>{notice}<div class="wrap"><table><tr><th>Pair</th><th>Side</th><th>Amount</th><th>Entry price</th><th>Status</th><th>Demo P/L</th><th>Date</th><th>Action</th></tr>{table_rows or "<tr><td colspan='8'>No trades yet.</td></tr>"}</table></div></main><nav class="bottom-nav" aria-label="Main sections"><a href="/trades"><span>↔</span>Trades</a><a href="/signals"><span>◈</span>Insights</a><a href="/social-trading"><span>◎</span>Social Trading</a><a href="/deposit"><span>＋</span>Deposit</a></nav></body></html>"""


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

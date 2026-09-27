from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os

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

init_db()

@app.route("/")
def home():
    if "user_id" in session:
        return render_template("index.html", user_name=session.get("user_name"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        if not name or not phone or len(password) < 6:
            error = "Enter your name, phone number and a password of at least 6 characters."
        else:
            try:
                with sqlite3.connect(DB) as con:
                    cur = con.execute(
                        "INSERT INTO users (name, phone, password) VALUES (?, ?, ?)",
                        (name, phone, generate_password_hash(password))
                    )
                    session["user_id"] = cur.lastrowid
                    session["user_name"] = name
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                error = "That phone number is already registered."
    return render_template("register.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        with sqlite3.connect(DB) as con:
            user = con.execute(
                "SELECT id, name, password FROM users WHERE phone = ?", (phone,)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

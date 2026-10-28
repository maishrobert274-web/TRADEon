import os
import sqlite3
import tempfile
import unittest

import app as tradeon


class EmailLoginMigrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        tradeon.DB = self.db_path
        with sqlite3.connect(self.db_path) as con:
            con.execute("""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 1,
                country TEXT NOT NULL DEFAULT 'Kenya',
                blocked INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT
            )""")
            con.execute(
                "INSERT INTO users (name,phone,password,verified,country) VALUES (?,?,?,?,?)",
                ("Legacy User", "+254712345678",
                 tradeon.generate_password_hash("legacy-pass"), 1, "Kenya")
            )
        tradeon.init_db()
        tradeon.init_manager_wallet_controls()
        tradeon.migrate_users_phone_nullable()
        tradeon.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = tradeon.app.test_client()

    def tearDown(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_legacy_phone_login_still_works_after_migration(self):
        response = self.client.post("/login", data={
            "identifier": "+254712345678", "password": "legacy-pass"
        })
        self.assertEqual(response.status_code, 302)
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT id,phone,email FROM users WHERE name='Legacy User'"
            ).fetchone()
        self.assertEqual(row[1], "+254712345678")
        self.assertIsNone(row[2])

    def test_email_registration_and_login_work(self):
        response = self.client.post("/register", data={
            "name": "Email User",
            "email": "New.User@example.com",
            "country": "Kenya",
            "password": "email-pass-123"
        })
        self.assertEqual(response.status_code, 302)
        self.client.get("/logout")
        response = self.client.post("/login", data={
            "identifier": "new.user@example.com",
            "password": "email-pass-123"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"new.user@example.com", response.data)
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT phone,email,email_verified FROM users WHERE email=?",
                ("new.user@example.com",)
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "new.user@example.com")
        self.assertEqual(row[2], 0)

    def test_user_can_add_phone_in_profile_later(self):
        self.client.post("/register", data={
            "name": "Profile Phone User",
            "email": "profile.phone@example.com",
            "country": "Kenya",
            "password": "email-pass-123"
        })
        response = self.client.post("/profile", data={
            "name": "Profile Phone User",
            "phone": "0712345678"
        })
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as con:
            phone = con.execute(
                "SELECT phone FROM users WHERE email=?",
                ("profile.phone@example.com",)
            ).fetchone()[0]
        self.assertEqual(phone, "+254712345678")
        self.assertIn(b"+254712345678", response.data)

    def test_duplicate_email_is_rejected(self):
        data = {
            "name": "First User", "email": "same@example.com",
            "country": "Kenya", "password": "password123"
        }
        self.client.post("/register", data=data)
        self.client.get("/logout")
        response = self.client.post("/register", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already registered", response.data)


if __name__ == "__main__":
    unittest.main()

# TRADEON PostgreSQL migration notes

## Safety rule
Do not point the live Render service at PostgreSQL until the application code has been adapted and tested. The live app currently uses SQLite through many direct `sqlite3.connect(DB)` calls.

## Current database tables
The application initialization creates or uses:
- users
- deposits
- wallets
- demo_wallets
- trades
- signals
- app_settings
- wallet_transactions

The app also performs SQLite-specific schema checks (`PRAGMA table_info`), backup discovery (`sqlite_master`), `INSERT OR IGNORE`, `lastrowid`, and SQLite date arithmetic. These must be handled deliberately; setting `DATABASE_URL` alone is not a migration.

## Safe migration sequence
1. Preserve the current production configuration and database.
2. Implement a database access layer or systematically adapt every SQL operation for PostgreSQL.
3. Add an explicit schema initialization/migration path for PostgreSQL.
4. Add a one-time importer that can read an exported TRADEON Manager ZIP (CSV files) and insert rows in dependency order, preserving IDs and relationships. Make it safe to re-run or clearly refuse duplicate imports.
5. Run syntax checks and tests against a disposable PostgreSQL database before touching production.
6. Configure the Render service with its private internal PostgreSQL connection string as `DATABASE_URL` only after steps 2–5 pass. The secret must be entered in Render, never committed to GitHub or pasted into chat.
7. Verify registration, login, Manager pages, signals, demo trades, wallet/deposit records, and monthly backup on the test deployment before production cutover.

## Important limitation
A CSV ZIP can preserve exported table rows, but it does not replace a reliable backup of the current database file or guarantee that every schema detail is captured. Keep the existing app database untouched until the import and verification are complete.

## Long-term retention
The current Render PostgreSQL instance is on a Free plan with a stated expiration of 2026-11-10. It is not suitable to rely on for a ten-year retention promise unless the service is moved to an appropriate paid plan and a separate backup/restore plan is maintained.

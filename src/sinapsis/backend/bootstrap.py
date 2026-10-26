"""Idempotent startup bootstrap for the Sinapsis BFF (runs on every start; safe to repeat).

The schema (including the `sessions` table) lives in src/postgres/init.sql.
If SINAPSIS_ADMIN_PASSWORD is set and the human ADMIN_USER does not exist yet, creates
   it (kind='human', role='admin', bcrypt hash, must_change_password=true). It never overwrites an
   existing user and never logs the password. The repository ships no default password.
"""
import logging
import os

log = logging.getLogger("sinapsis.bootstrap")


def run(pg, hash_password, admin_user: str) -> None:
    password = os.environ.get("SINAPSIS_ADMIN_PASSWORD", "")
    if not password:
        return
    with pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE lower(name)=lower(%s)", (admin_user,))
        if cur.fetchone():
            return
        # api_key_hash is NOT NULL in the Cerebro schema; a human logs in by password,
        # so it gets an unusable random hash (no API key exists for it).
        import hashlib, secrets
        unusable = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        cur.execute(
            "INSERT INTO agents (name, api_key_hash, kind, role, password_hash, must_change_password, active) "
            "VALUES (%s, %s, 'human', 'admin', %s, true, true)",
            (admin_user, unusable, hash_password(password)))
        conn.commit()
        log.info("bootstrap: human admin %r created (must change password on first login)", admin_user)

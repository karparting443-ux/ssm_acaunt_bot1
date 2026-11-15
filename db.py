import os
from decimal import Decimal

import asyncpg

pool: asyncpg.Pool | None = None

DEFAULTS = {
    "price_tjs": "10",
    "price_rub": "100",
    "duration_hours": "24",
    "per_hour": "1",
    "plan_enabled": "1",
    "pay_details_tjs": "Реквизиты не заданы (админ: /admin → Реквизиты TJS)",
    "pay_details_rub": "Реквизиты не заданы (админ: /admin → Реквизиты RUB)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    data TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'free',
    user_id BIGINT,
    issued_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    currency TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    file_id TEXT,
    file_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    payment_id INT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    next_issue_at TIMESTAMPTZ NOT NULL,
    issued_count INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_sub
    ON subscriptions(user_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS issue_history (
    id SERIAL PRIMARY KEY,
    subscription_id INT,
    user_id BIGINT NOT NULL,
    account_id INT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_history_user ON issue_history(user_id, issued_at DESC);
"""


async def init():
    global pool
    dsn = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or "").strip()
    if not dsn or "@" not in dsn or "${{" in dsn:
        raise RuntimeError(
            "DATABASE_URL пуст или неверный (получено: %r). Задайте в Variables сервиса бота: "
            "DATABASE_URL = ${{Postgres.DATABASE_URL}} (имя сервиса БД должно совпадать)." % dsn[:15]
        )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        for k, v in DEFAULTS.items():
            await c.execute(
                "INSERT INTO settings(key, value) VALUES($1, $2) ON CONFLICT DO NOTHING", k, v
            )


# ---------- settings ----------
async def get_settings() -> dict:
    rows = await pool.fetch("SELECT key, value FROM settings")
    s = dict(DEFAULTS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


async def set_setting(key: str, value: str):
    await pool.execute(
        "INSERT INTO settings(key, value) VALUES($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value,
    )


# ---------- users ----------
async def upsert_user(uid: int, username: str | None, full_name: str):
    await pool.execute(
        "INSERT INTO users(id, username, full_name) VALUES($1, $2, $3) "
        "ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username, full_name = EXCLUDED.full_name",
        uid, username, full_name,
    )


async def get_user(uid: int):
    return await pool.fetchrow("SELECT * FROM users WHERE id = $1", uid)


# ---------- stock ----------
async def add_accounts(lines: list[str]) -> tuple[int, int]:
    added = 0
    async with pool.acquire() as c:
        for line in lines:
            r = await c.execute(
                "INSERT INTO accounts(data) VALUES($1) ON CONFLICT (data) DO NOTHING", line
            )
            if r.endswith("1"):
                added += 1
    return added, len(lines) - added


async def delete_accounts(lines: list[str]) -> int:
    """Удаляет только НЕвыданные аккаунты."""
    r = await pool.execute(
        "DELETE FROM accounts WHERE status = 'free' AND data = ANY($1::text[])", lines
    )
    return int(r.split()[-1])


async def stock_counts() -> dict:
    r = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE status='free') AS free, "
        "count(*) FILTER (WHERE status='issued') AS issued, count(*) AS total FROM accounts"
    )
    return dict(r)


# ---------- payments ----------
async def pending_payment_of(uid: int):
    return await pool.fetchrow(
        "SELECT * FROM payments WHERE user_id=$1 AND status='pending' LIMIT 1", uid
    )


async def create_payment(uid, currency, amount: Decimal, file_id, file_type):
    return await pool.fetchrow(
        "INSERT INTO payments(user_id, currency, amount, file_id, file_type) "
        "VALUES($1,$2,$3,$4,$5) RETURNING *",
        uid, currency, amount, file_id, file_type,
    )


async def get_payment(pid: int):
    return await pool.fetchrow("SELECT * FROM payments WHERE id=$1", pid)


async def pending_payments():
    return await pool.fetch("SELECT * FROM payments WHERE status='pending' ORDER BY id")


async def decide_payment(pid: int, admin_id: int, status: str):
    """Атомарно: решение принимается только один раз."""
    return await pool.fetchrow(
        "UPDATE payments SET status=$3, decided_by=$2, decided_at=now() "
        "WHERE id=$1 AND status='pending' RETURNING *",
        pid, admin_id, status,
    )


# ---------- subscriptions ----------
async def active_sub(uid: int):
    return await pool.fetchrow(
        "SELECT * FROM subscriptions WHERE user_id=$1 AND status='active'", uid
    )


async def activate_subscription(uid: int, payment_id: int, hours: int):
    """Создаёт подписку; если уже есть активная — продлевает её."""
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "UPDATE subscriptions SET expires_at = expires_at + make_interval(hours => $2) "
                "WHERE user_id=$1 AND status='active' RETURNING *",
                uid, hours,
            )
            if row:
                return row, True
            row = await c.fetchrow(
                "INSERT INTO subscriptions(user_id, payment_id, expires_at, next_issue_at) "
                "VALUES($1, $2, now() + make_interval(hours => $3), now()) RETURNING *",
                uid, payment_id, hours,
            )
            return row, False


async def due_subscriptions():
    return await pool.fetch(
        "SELECT * FROM subscriptions WHERE status='active' "
        "AND next_issue_at <= now() AND next_issue_at < expires_at ORDER BY id"
    )


async def expire_due():
    return await pool.fetch(
        "UPDATE subscriptions SET status='expired' "
        "WHERE status='active' AND expires_at <= now() "
        "RETURNING id, user_id, issued_count"
    )


async def issue_accounts(sub_id: int, user_id: int, n: int):
    """
    Атомарная выдача n аккаунтов.
    Возвращает: list[str] — выданные аккаунты; [] — выдавать пока нечего; None — склад пуст.
    FOR UPDATE SKIP LOCKED гарантирует, что один аккаунт не получат двое.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            sub = await c.fetchrow(
                "SELECT id FROM subscriptions WHERE id=$1 AND status='active' "
                "AND next_issue_at <= now() AND next_issue_at < expires_at FOR UPDATE",
                sub_id,
            )
            if not sub:
                return []
            rows = await c.fetch(
                "SELECT id, data FROM accounts WHERE status='free' "
                "ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED",
                n,
            )
            if len(rows) < n:
                return None
            ids = [r["id"] for r in rows]
            await c.execute(
                "UPDATE accounts SET status='issued', user_id=$1, issued_at=now() "
                "WHERE id = ANY($2::int[])",
                user_id, ids,
            )
            await c.executemany(
                "INSERT INTO issue_history(subscription_id, user_id, account_id) VALUES($1,$2,$3)",
                [(sub_id, user_id, i) for i in ids],
            )
            await c.execute(
                "UPDATE subscriptions SET issued_count = issued_count + $2, "
                "next_issue_at = next_issue_at + interval '1 hour' WHERE id=$1",
                sub_id, len(ids),
            )
            return [r["data"] for r in rows]


async def history(uid: int, limit: int = 30):
    return await pool.fetch(
        "SELECT a.data, h.issued_at FROM issue_history h "
        "JOIN accounts a ON a.id = h.account_id "
        "WHERE h.user_id=$1 ORDER BY h.issued_at DESC, h.id DESC LIMIT $2",
        uid, limit,
    )

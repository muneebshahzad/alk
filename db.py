import os

import psycopg2
from psycopg2.extras import RealDictCursor


_LAST_DB_ERROR = ""


def _set_last_db_error(message: str):
    global _LAST_DB_ERROR
    _LAST_DB_ERROR = message


def get_last_db_error() -> str:
    return _LAST_DB_ERROR


def _ensure_app_settings_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_order_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_order_overrides (
            order_id TEXT PRIMARY KEY,
            amount_received NUMERIC(12,2) NOT NULL DEFAULT 0,
            packaging_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            delivery_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            delivery_status TEXT NOT NULL DEFAULT 'Inprocess',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_item_cost_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_item_cost_overrides (
            item_key TEXT PRIMARY KEY,
            product_id TEXT,
            variant_id TEXT,
            title TEXT NOT NULL,
            cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def _ensure_aghaje_order_item_cost_overrides_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aghaje_order_item_cost_overrides (
            order_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            product_id TEXT,
            variant_id TEXT,
            title TEXT NOT NULL,
            cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (order_id, item_key)
        )
        """
    )


def _ensure_admin_passkeys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_passkeys (
            credential_id BYTEA PRIMARY KEY,
            public_key BYTEA NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            device_name TEXT NOT NULL DEFAULT 'Mobile device',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ
        )
        """
    )


def _ensure_employee_passkeys_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_passkeys (
            credential_id BYTEA PRIMARY KEY,
            public_key BYTEA NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            device_name TEXT NOT NULL DEFAULT 'Mobile device',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ
        )
        """
    )


def _ensure_finance_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_accounts (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            account_type TEXT NOT NULL CHECK (account_type IN ('asset','liability','equity','income','expense')),
            normal_side CHAR(1) NOT NULL CHECK (normal_side IN ('D','C')),
            system_key TEXT UNIQUE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS finance_journals (
            id BIGSERIAL PRIMARY KEY,
            public_id UUID NOT NULL UNIQUE,
            transaction_date DATE NOT NULL,
            reference TEXT,
            description TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            external_id TEXT,
            status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
            reversal_of BIGINT REFERENCES finance_journals(id),
            created_by TEXT NOT NULL DEFAULT 'Owner',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reversed_at TIMESTAMPTZ
        );
        CREATE UNIQUE INDEX IF NOT EXISTS finance_journals_source_external_idx
            ON finance_journals(source, external_id) WHERE external_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS finance_lines (
            id BIGSERIAL PRIMARY KEY,
            journal_id BIGINT NOT NULL REFERENCES finance_journals(id) ON DELETE RESTRICT,
            account_id BIGINT NOT NULL REFERENCES finance_accounts(id) ON DELETE RESTRICT,
            debit NUMERIC(16,2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
            credit NUMERIC(16,2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
            memo TEXT,
            CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
        );
        CREATE INDEX IF NOT EXISTS finance_lines_journal_idx ON finance_lines(journal_id);
        CREATE INDEX IF NOT EXISTS finance_lines_account_idx ON finance_lines(account_id);
        """
    )
    accounts = (
        ('1000', 'Bank', 'asset', 'D', 'bank'),
        ('1010', 'Cash', 'asset', 'D', 'cash'),
        ('1100', 'DigiDokaan Receivable', 'asset', 'D', 'digidokaan_receivable'),
        ('1200', 'Inventory', 'asset', 'D', 'inventory'),
        ('2000', 'Accounts Payable', 'liability', 'C', 'accounts_payable'),
        ('3000', "Owner's Equity", 'equity', 'C', 'owner_equity'),
        ('3100', "Owner's Drawings", 'equity', 'D', 'owner_drawings'),
        ('4000', 'Sales Revenue', 'income', 'C', 'sales_revenue'),
        ('4010', 'Delivery Income', 'income', 'C', 'delivery_income'),
        ('4090', 'Other Income', 'income', 'C', 'other_income'),
        ('5000', 'Cost of Goods Sold', 'expense', 'D', 'cost_of_goods'),
        ('5100', 'Advertising', 'expense', 'D', 'advertising'),
        ('5110', 'Salaries', 'expense', 'D', 'salaries'),
        ('5120', 'Logistics & Courier', 'expense', 'D', 'logistics'),
        ('5130', 'Packaging', 'expense', 'D', 'packaging'),
        ('5140', 'Software & Subscriptions', 'expense', 'D', 'software'),
        ('5190', 'Other Expense', 'expense', 'D', 'other_expense')
    )
    cur.executemany(
        """
        INSERT INTO finance_accounts (code, name, account_type, normal_side, system_key)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            account_type = EXCLUDED.account_type,
            normal_side = EXCLUDED.normal_side,
            system_key = EXCLUDED.system_key
        """,
        accounts,
    )


def get_conn():
    url = (
        os.getenv("DATABASE_URL", "")
        or os.getenv("POSTGRES_URL", "")
        or os.getenv("POSTGRESQL_URL", "")
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url:
        return psycopg2.connect(url)

    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT") or "5432"
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    database = (
        os.getenv("PGDATABASE")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
    )

    if host and user and database:
        return psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=database,
        )

    raise RuntimeError(
        "Database configuration missing. Set DATABASE_URL (preferred) or PGHOST/PGUSER/PGPASSWORD/PGDATABASE."
    )


def init_db():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_statuses (
                        key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                _ensure_app_settings_table(cur)
                _ensure_aghaje_order_overrides_table(cur)
                _ensure_aghaje_item_cost_overrides_table(cur)
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                _ensure_admin_passkeys_table(cur)
                _ensure_employee_passkeys_table(cur)
                _ensure_finance_tables(cur)
            conn.commit()
        _set_last_db_error("")
        print("DB initialized.")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB init error: {e}")


def load_order_statuses() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT key, status FROM order_statuses")
                _set_last_db_error("")
                return {row["key"]: row["status"] for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load error: {e}")
        return {}


def upsert_order_status(key: str, status: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_statuses (key, status)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET status = EXCLUDED.status,
                            updated_at = NOW()
                    """,
                    (key, status),
                )
            conn.commit()
        _set_last_db_error("")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert error: {e}")


def delete_order_status(key: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM order_statuses WHERE key = %s", (key,))
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB delete error: {e}")
        return False


def get_app_setting(key: str, default: str = "") -> str:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                _set_last_db_error("")
                return row[0] if row and row[0] is not None else default
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB get_app_setting error: {e}")
        return default


def set_app_setting(key: str, value: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = NOW()
                    """,
                    (key, value),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB set_app_setting error: {e}")
        return False


def load_admin_passkeys() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_admin_passkeys_table(cur)
                cur.execute(
                    "SELECT credential_id, public_key, sign_count, device_name, created_at, last_used_at FROM admin_passkeys ORDER BY created_at"
                )
                rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        _set_last_db_error("")
        return rows
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load admin passkeys error: {e}")
        return []


def save_admin_passkey(credential_id: bytes, public_key: bytes, sign_count: int, device_name: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_admin_passkeys_table(cur)
                cur.execute(
                    """
                    INSERT INTO admin_passkeys (credential_id, public_key, sign_count, device_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (credential_id) DO UPDATE
                        SET public_key = EXCLUDED.public_key,
                            sign_count = EXCLUDED.sign_count,
                            device_name = EXCLUDED.device_name
                    """,
                    (credential_id, public_key, sign_count, device_name),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB save admin passkey error: {e}")
        return False


def update_admin_passkey_usage(credential_id: bytes, sign_count: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_admin_passkeys_table(cur)
                cur.execute(
                    "UPDATE admin_passkeys SET sign_count = %s, last_used_at = NOW() WHERE credential_id = %s",
                    (sign_count, credential_id),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB update admin passkey error: {e}")
        return False


def load_employee_passkeys() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_employee_passkeys_table(cur)
                cur.execute("SELECT credential_id, public_key, sign_count, device_name FROM employee_passkeys ORDER BY created_at")
                rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows
    except Exception as e:
        print(f"DB load employee passkeys error: {e}")
        return []


def save_employee_passkey(credential_id: bytes, public_key: bytes, sign_count: int, device_name: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_employee_passkeys_table(cur)
                cur.execute(
                    """INSERT INTO employee_passkeys (credential_id, public_key, sign_count, device_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (credential_id) DO UPDATE SET public_key=EXCLUDED.public_key, sign_count=EXCLUDED.sign_count, device_name=EXCLUDED.device_name""",
                    (credential_id, public_key, sign_count, device_name),
                )
            conn.commit()
        return True
    except Exception as e:
        print(f"DB save employee passkey error: {e}")
        return False


def update_employee_passkey_usage(credential_id: bytes, sign_count: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_employee_passkeys_table(cur)
                cur.execute("UPDATE employee_passkeys SET sign_count=%s, last_used_at=NOW() WHERE credential_id=%s", (sign_count, credential_id))
            conn.commit()
        return True
    except Exception as e:
        print(f"DB update employee passkey error: {e}")
        return False


def load_aghaje_order_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_order_overrides_table(cur)
                cur.execute(
                    """
                    SELECT order_id, amount_received, packaging_cost, delivery_cost, payment_status, delivery_status, updated_at
                    FROM aghaje_order_overrides
                    """
                )
                _set_last_db_error("")
                return {row["order_id"]: dict(row) for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje overrides error: {e}")
        return {}


def upsert_aghaje_order_override(
    order_id: str,
    amount_received: str | float,
    packaging_cost: str | float,
    delivery_cost: str | float,
    payment_status: str,
    delivery_status: str,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_order_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_order_overrides (
                        order_id,
                        amount_received,
                        packaging_cost,
                        delivery_cost,
                        payment_status,
                        delivery_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO UPDATE
                        SET amount_received = EXCLUDED.amount_received,
                            packaging_cost = EXCLUDED.packaging_cost,
                            delivery_cost = EXCLUDED.delivery_cost,
                            payment_status = EXCLUDED.payment_status,
                            delivery_status = EXCLUDED.delivery_status,
                            updated_at = NOW()
                    """,
                    (
                        order_id,
                        amount_received,
                        packaging_cost,
                        delivery_cost,
                        payment_status,
                        delivery_status,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje override error: {e}")
        return False


def load_aghaje_item_cost_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    SELECT item_key, product_id, variant_id, title, cost
                    FROM aghaje_item_cost_overrides
                    """
                )
                _set_last_db_error("")
                return {row["item_key"]: dict(row) for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje item cost overrides error: {e}")
        return {}


def load_aghaje_order_item_cost_overrides() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    SELECT order_id, item_key, product_id, variant_id, title, cost
                    FROM aghaje_order_item_cost_overrides
                    """
                )
                _set_last_db_error("")
                grouped = {}
                for row in cur.fetchall():
                    grouped.setdefault(row["order_id"], {})[row["item_key"]] = dict(row)
                return grouped
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load aghaje order item cost overrides error: {e}")
        return {}


def upsert_aghaje_item_cost_override(
    item_key: str,
    title: str,
    cost: str | float,
    product_id: str | int | None = None,
    variant_id: str | int | None = None,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_item_cost_overrides (
                        item_key,
                        product_id,
                        variant_id,
                        title,
                        cost
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (item_key) DO UPDATE
                        SET product_id = EXCLUDED.product_id,
                            variant_id = EXCLUDED.variant_id,
                            title = EXCLUDED.title,
                            cost = EXCLUDED.cost,
                            updated_at = NOW()
                    """,
                    (
                        item_key,
                        str(product_id) if product_id is not None else None,
                        str(variant_id) if variant_id is not None else None,
                        title,
                        cost,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje item cost override error: {e}")
        return False


def upsert_aghaje_order_item_cost_override(
    order_id: str,
    item_key: str,
    title: str,
    cost: str | float,
    product_id: str | int | None = None,
    variant_id: str | int | None = None,
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_aghaje_order_item_cost_overrides_table(cur)
                cur.execute(
                    """
                    INSERT INTO aghaje_order_item_cost_overrides (
                        order_id,
                        item_key,
                        product_id,
                        variant_id,
                        title,
                        cost
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id, item_key) DO UPDATE
                        SET product_id = EXCLUDED.product_id,
                            variant_id = EXCLUDED.variant_id,
                            title = EXCLUDED.title,
                            cost = EXCLUDED.cost,
                            updated_at = NOW()
                    """,
                    (
                        order_id,
                        item_key,
                        str(product_id) if product_id is not None else None,
                        str(variant_id) if variant_id is not None else None,
                        title,
                        cost,
                    ),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert aghaje order item cost override error: {e}")
        return False

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from psycopg2.extras import RealDictCursor

from db import get_conn


MONEY = Decimal("0.01")


def money(value):
    try:
        result = Decimal(str(value or "0")).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid amount.")
    if result <= 0:
        raise ValueError("Amount must be greater than zero.")
    return result


def build_entry(kind, amount, cash_account="bank", category=None, destination=None, deduction=0, foreign_amount=0):
    amount = money(amount)
    try:
        deduction_amount = Decimal(str(deduction or "0")).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid courier deduction.")
    if deduction_amount < 0:
        raise ValueError("Courier deductions cannot be negative.")
    if kind == "income":
        lines = [(cash_account, amount, 0), (category or "sales_revenue", 0, amount)]
    elif kind == "expense":
        lines = [(category or "other_expense", amount, 0), (cash_account, 0, amount)]
    elif kind == "owner_contribution":
        lines = [(cash_account, amount, 0), ("owner_equity", 0, amount)]
    elif kind == "owner_drawing":
        lines = [("owner_drawings", amount, 0), (cash_account, 0, amount)]
    elif kind == "transfer":
        if not destination or destination == cash_account:
            raise ValueError("Choose two different accounts for a transfer.")
        lines = [(destination, amount, 0), (cash_account, 0, amount)]
    elif kind == "supplier_bill":
        lines = [(category or "other_expense", amount, 0), ("accounts_payable", 0, amount)]
    elif kind == "supplier_payment":
        lines = [("accounts_payable", amount, 0), (cash_account, 0, amount)]
    elif kind in {"digidokaan_cheque", "call_courier_invoice"}:
        if deduction_amount >= amount:
            raise ValueError("Courier deductions must be less than the invoice amount.")
        lines = [(cash_account, amount - deduction_amount, 0)]
        if deduction_amount:
            lines.append(("logistics", deduction_amount, 0))
        lines.append(("sales_revenue", 0, amount))
    else:
        raise ValueError("Unsupported transaction type.")

    payoneer_involved = cash_account == "payoneer_usd" or destination == "payoneer_usd"
    if not payoneer_involved:
        return lines
    native = money(foreign_amount)
    enriched = []
    for account, debit, credit in lines:
        if account == "payoneer_usd":
            enriched.append((account, debit, credit, native if debit else 0, native if credit else 0))
        else:
            enriched.append((account, debit, credit, 0, 0))
    return enriched


def _account_map(cur):
    cur.execute("SELECT id, system_key FROM finance_accounts WHERE active = TRUE")
    return {row[1]: row[0] for row in cur.fetchall() if row[1]}


def post_journal(transaction_date, description, reference, lines, source="manual", external_id=None):
    if not description or not str(description).strip():
        raise ValueError("Description is required.")
    normalized = [tuple(line) + (0, 0) if len(line) == 3 else tuple(line) for line in lines]
    debit = sum(Decimal(str(line[1])) for line in normalized)
    credit = sum(Decimal(str(line[2])) for line in normalized)
    if debit != credit or debit <= 0:
        raise ValueError("Transaction is not balanced.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            accounts = _account_map(cur)
            missing = [line[0] for line in normalized if line[0] not in accounts]
            if missing:
                raise ValueError("Unknown account: " + ", ".join(missing))
            public_id = str(uuid4())
            cur.execute(
                """INSERT INTO finance_journals
                   (public_id, transaction_date, reference, description, source, external_id)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (public_id, transaction_date or date.today(), reference or None,
                 str(description).strip(), source, external_id or None),
            )
            journal_id = cur.fetchone()[0]
            cur.executemany(
                """INSERT INTO finance_lines (journal_id, account_id, debit, credit, native_debit, native_credit)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [(journal_id, accounts[key], debit, credit, native_debit, native_credit)
                 for key, debit, credit, native_debit, native_credit in normalized],
            )
        conn.commit()
    return public_id


def reverse_journal(public_id, reversal_date=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM finance_journals WHERE public_id = %s FOR UPDATE", (public_id,))
            original = cur.fetchone()
            if not original:
                raise ValueError("Transaction not found.")
            if original["status"] == "reversed":
                raise ValueError("This transaction has already been reversed.")
            cur.execute(
                """SELECT a.system_key, l.debit, l.credit, l.native_debit, l.native_credit
                   FROM finance_lines l JOIN finance_accounts a ON a.id=l.account_id
                   WHERE l.journal_id=%s ORDER BY l.id""", (original["id"],)
            )
            lines = [(row["system_key"], row["credit"], row["debit"], row["native_credit"], row["native_debit"]) for row in cur.fetchall()]
            reversal_id = str(uuid4())
            cur.execute(
                """INSERT INTO finance_journals
                   (public_id, transaction_date, reference, description, source, reversal_of)
                   VALUES (%s,%s,%s,%s,'reversal',%s) RETURNING id""",
                (reversal_id, reversal_date or date.today(), original["reference"],
                 f'Reversal: {original["description"]}', original["id"]),
            )
            new_id = cur.fetchone()[0]
            accounts = _account_map(cur)
            cur.executemany(
                """INSERT INTO finance_lines
                   (journal_id, account_id, debit, credit, native_debit, native_credit)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [(new_id, accounts[key], debit, credit, native_debit, native_credit)
                 for key, debit, credit, native_debit, native_credit in lines],
            )
            cur.execute("UPDATE finance_journals SET status='reversed', reversed_at=NOW() WHERE id=%s", (original["id"],))
        conn.commit()
    return reversal_id


def finance_dashboard(period_start=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT a.code, a.name, a.account_type, a.normal_side, a.system_key, a.currency,
                          COALESCE(SUM(l.debit),0) debit, COALESCE(SUM(l.credit),0) credit,
                          COALESCE(SUM(l.native_debit),0) native_debit,
                          COALESCE(SUM(l.native_credit),0) native_credit
                   FROM finance_accounts a LEFT JOIN finance_lines l ON l.account_id=a.id
                   WHERE a.active=TRUE GROUP BY a.id ORDER BY a.code"""
            )
            accounts = []
            for row in cur.fetchall():
                row = dict(row)
                row["balance"] = row["debit"] - row["credit"] if row["normal_side"] == "D" else row["credit"] - row["debit"]
                row["native_balance"] = row["native_debit"] - row["native_credit"] if row["normal_side"] == "D" else row["native_credit"] - row["native_debit"]
                accounts.append(row)
            cur.execute(
                """SELECT a.account_type,
                          COALESCE(SUM(CASE WHEN a.normal_side='D' THEN l.debit-l.credit ELSE l.credit-l.debit END),0) balance
                   FROM finance_lines l JOIN finance_accounts a ON a.id=l.account_id
                   JOIN finance_journals j ON j.id=l.journal_id
                   WHERE j.transaction_date >= COALESCE(%s::date, DATE_TRUNC('month', CURRENT_DATE)::date)
                     AND j.transaction_date < (COALESCE(%s::date, DATE_TRUNC('month', CURRENT_DATE)::date) + INTERVAL '1 month')
                     AND a.account_type IN ('income','expense')
                   GROUP BY a.account_type""",
                (period_start, period_start),
            )
            period_totals = {row["account_type"]: row["balance"] for row in cur.fetchall()}
            cur.execute(
                """SELECT j.public_id, j.transaction_date, j.reference, j.description, j.source,
                          j.status, j.reversal_of, j.created_at, SUM(l.debit) amount,
                          STRING_AGG(CASE WHEN l.debit>0 THEN a.name END, ', ') debit_accounts,
                          STRING_AGG(CASE WHEN l.credit>0 THEN a.name END, ', ') credit_accounts
                   FROM finance_journals j JOIN finance_lines l ON l.journal_id=j.id
                   JOIN finance_accounts a ON a.id=l.account_id
                   GROUP BY j.id ORDER BY j.transaction_date DESC, j.id DESC LIMIT 100"""
            )
            journals = [dict(row) for row in cur.fetchall()]
    by_key = {row["system_key"]: row for row in accounts}
    income = period_totals.get("income", Decimal("0.00"))
    expenses = period_totals.get("expense", Decimal("0.00"))
    return {
        "accounts": accounts,
        "journals": journals,
        "by_key": by_key,
        "income": income,
        "expenses": expenses,
        "profit": income - expenses,
    }

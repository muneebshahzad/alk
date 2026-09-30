from decimal import Decimal

import pytest

from finance import build_entry, money


def totals(lines):
    return sum(Decimal(str(row[1])) for row in lines), sum(Decimal(str(row[2])) for row in lines)


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("income", {"category": "sales_revenue"}),
        ("expense", {"category": "advertising"}),
        ("owner_contribution", {}),
        ("owner_drawing", {}),
        ("transfer", {"destination": "cash"}),
        ("supplier_bill", {"category": "other_expense"}),
        ("supplier_payment", {}),
        ("digidokaan_cheque", {"deduction": "125.50"}),
        ("call_courier_invoice", {"deduction": "125.50"}),
    ],
)
def test_guided_entries_are_balanced(kind, kwargs):
    lines = build_entry(kind, "1000", **kwargs)
    debit, credit = totals(lines)
    assert debit == credit == Decimal("1000.00")


def test_courier_invoice_splits_bank_and_deduction():
    lines = build_entry("digidokaan_cheque", "1000", deduction="125.50")
    assert lines == [
        ("bank", Decimal("874.50"), 0),
        ("logistics", Decimal("125.50"), 0),
        ("sales_revenue", 0, Decimal("1000.00")),
    ]


def test_owner_drawing_is_not_an_expense():
    lines = build_entry("owner_drawing", "5000")
    assert lines[0][0] == "owner_drawings"
    assert all(line[0] != "other_expense" for line in lines)


@pytest.mark.parametrize("value", ["", "0", "-1", "abc", None])
def test_money_rejects_invalid_or_non_positive_values(value):
    with pytest.raises(ValueError):
        money(value)


def test_transfer_requires_different_accounts():
    with pytest.raises(ValueError):
        build_entry("transfer", "100", cash_account="bank", destination="bank")


def test_courier_deduction_cannot_consume_settlement():
    with pytest.raises(ValueError):
        build_entry("digidokaan_cheque", "100", deduction="100")


def test_payoneer_keeps_usd_amount_and_pkr_ledger_value():
    lines = build_entry("expense", "27950", cash_account="payoneer_usd", category="advertising", foreign_amount="100")
    assert lines == [
        ("advertising", Decimal("27950.00"), 0, 0, 0),
        ("payoneer_usd", 0, Decimal("27950.00"), 0, Decimal("100.00")),
    ]

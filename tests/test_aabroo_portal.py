import pytest

main = pytest.importorskip("main", reason="application dependencies are not installed in the lightweight test environment")


def _order(**overrides):
    value = {
        "id": 42, "name": "#AB1001", "created_at": "2026-10-01T10:00:00+05:00",
        "fulfillment_status": "fulfilled", "financial_status": "pending",
        "current_subtotal_price": "3000", "current_total_price": "3200",
        "customer": {"first_name": "Aabroo", "last_name": "Customer"},
        "shipping_address": {"city": "Lahore", "phone": "03000000000"},
        "note_attributes": [
            {"name": "hxs_courier_status", "value": "Delivered"},
            {"name": "hxs_courier_tracking", "value": "TRK-1"},
        ],
        "line_items": [{"product_id": 10, "variant_id": 11, "title": "Dress", "quantity": 2}],
    }
    value.update(overrides)
    return value


def test_aabroo_accounting_includes_cod_and_vendor_cash(monkeypatch):
    monkeypatch.setattr(main, "_aabroo_fetch_orders", lambda: [_order()])
    monkeypatch.setattr(main, "_aabroo_shop_domain", lambda: "aabroo.myshopify.com")
    monkeypatch.setattr(main, "load_aghaje_order_overrides", lambda: {"#AB1001": {
        "amount_received": 3200, "packaging_cost": 100, "delivery_cost": 200,
    }})
    monkeypatch.setattr(main, "load_aghaje_item_cost_overrides", lambda: {"10:11": {"cost": 500}})
    monkeypatch.setattr(main, "load_aghaje_order_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "_aabroo_settlements", lambda: [{"amount": -500}])

    data = main.build_aabroo_portal_data()

    assert data["summary"]["total_cost"] == 1300
    assert data["summary"]["total_cod"] == 3200
    assert data["summary"]["total_cash_paid"] == -500
    assert data["summary"]["balance"] == 1400
    assert data["orders"][0]["payable"] == 1900


def test_aabroo_portal_requires_its_own_login(monkeypatch):
    monkeypatch.setattr(main, "AABROO_PORTAL_PASSWORD", "aabroo-secret")
    client = main.app.test_client()
    assert client.get("/aabroo_portal").status_code == 200
    assert b"Portal password" in client.get("/aabroo_portal").data
    assert client.post("/aabroo_portal", data={"password": "wrong"}).status_code == 401
    response = client.post("/aabroo_portal", data={"password": "aabroo-secret"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/aabroo-portal")


def test_aabroo_api_failure_renders_safe_empty_portal(monkeypatch):
    monkeypatch.setattr(main, "_aabroo_fetch_orders", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(main, "load_aghaje_order_overrides", lambda: {})
    monkeypatch.setattr(main, "load_aghaje_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "load_aghaje_order_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "_aabroo_settlements", lambda: [])
    data = main.build_aabroo_portal_data()
    assert data["orders"] == []
    assert data["error_message"] == "offline"

import pytest

main = pytest.importorskip("main", reason="application dependencies are not installed in the lightweight test environment")


@pytest.fixture(autouse=True)
def _avoid_aabroo_product_api(monkeypatch):
    monkeypatch.setattr(main, "_aabroo_product_image_map", lambda _orders: {})
    main._aabroo_tracking_cache.clear()


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


def test_aabroo_line_image_prefers_variant_then_product_image():
    images = {
        ("10", ""): "https://cdn.example/product.jpg",
        ("10", "11"): "https://cdn.example/variant.jpg",
    }

    assert main._aabroo_line_image({"product_id": 10, "variant_id": 11}, images) == "https://cdn.example/variant.jpg"
    assert main._aabroo_line_image({"product_id": 10, "variant_id": 12}, images) == "https://cdn.example/product.jpg"
    assert main._aabroo_line_image({"image": "https://cdn.example/order.jpg", "product_id": 10}, images) == "https://cdn.example/order.jpg"


def test_aabroo_tracking_falls_back_to_shopify_fulfillment():
    source = _order(
        note_attributes=[],
        fulfillments=[{
            "tracking_number": "22320269889521",
            "tracking_company": "DigiDokaan",
        }],
    )

    assert main._aabroo_tracking_details(source) == ("22320269889521", "DigiDokaan")


def test_aabroo_digidokaan_tracking_uses_live_status(monkeypatch):
    async def live(_lookups):
        return {"22320269889521": "Out for Delivery"}

    monkeypatch.setattr(main, "_aabroo_fetch_live_tracking_statuses_async", live)
    source = _order(note_attributes=[
        {"name": "hxs_courier_name", "value": "DigiDokaan"},
        {"name": "hxs_courier_tracking", "value": "22320269889521"},
    ])

    statuses = main._aabroo_resolve_tracking_statuses([source])
    delivery, detail = main._aabroo_delivery(source, main._aabroo_notes(source), statuses["22320269889521"])

    assert detail == "Out for Delivery"
    assert delivery == "Inprocess"


def test_aabroo_tracking_failure_preserves_last_valid_status(monkeypatch):
    responses = iter(({"22320269889521": "In Transit"}, {}))

    async def live(_lookups):
        return next(responses)

    monkeypatch.setattr(main, "_aabroo_fetch_live_tracking_statuses_async", live)
    source = _order(note_attributes=[
        {"name": "hxs_courier_name", "value": "DigiDokaan"},
        {"name": "hxs_courier_tracking", "value": "22320269889521"},
    ])

    assert main._aabroo_resolve_tracking_statuses([source], force=True)["22320269889521"] == "In Transit"
    assert main._aabroo_resolve_tracking_statuses([source], force=True)["22320269889521"] == "In Transit"


def test_aabroo_return_delivery_is_not_misclassified_as_delivered():
    delivery, detail = main._aabroo_delivery({}, {}, "Return - Delivered to Shipper")
    assert delivery == "Returned"
    assert detail == "Return - Delivered to Shipper"


def test_aabroo_tracking_without_scans_uses_booked_baseline():
    assert main._aabroo_delivery({}, {}, tracking_number="22320269889521") == ("Inprocess", "Booked")
    assert main._aabroo_delivery({}, {}) == ("Inprocess", "Not booked with courier yet")


def test_aabroo_orders_are_available_to_scanner(monkeypatch):
    monkeypatch.setattr(main, "build_aabroo_portal_data", lambda: {
        "all_orders": [{
            "shopify_order_id": 987,
            "order_id": "#1020",
            "delivery_status": "Returned",
            "customer_name": "Customer",
            "tracking_number": "22320269889521",
            "items": [{"title": "Stole", "qty": 1, "image": "https://cdn.example/stole.jpg"}],
        }],
        "error_message": "",
    })

    orders = main.build_aabroo_employee_portal_orders()

    assert orders[0]["source"] == "aabroo"
    assert orders[0]["shopify_id"] == 987
    assert orders[0]["items"][0]["tracking_number"] == "22320269889521"


def test_employee_return_report_tags_aabroo_store(monkeypatch):
    tagged = []
    monkeypatch.setattr(main, "apply_employee_portal_order_tag", lambda source, order_id, tag: tagged.append((source, order_id, tag)) or True)
    client = main.app.test_client()
    with client.session_transaction() as session:
        session[main.EMPLOYEE_PORTAL_SESSION_KEY] = True

    response = client.post("/employee_portal/report", json={
        "mode": "return",
        "orders": [{"source": "aabroo", "shopify_id": "987", "order_id": "#1020"}],
    })

    assert response.status_code == 200
    assert tagged == [("aabroo", "987", "Return Received")]
    assert response.get_json()["tagged_by_source"] == {"Aabroo": 1}


def test_aabroo_accounting_includes_cod_and_vendor_cash(monkeypatch):
    monkeypatch.setattr(main, "_aabroo_fetch_orders", lambda: [_order()])
    monkeypatch.setattr(main, "_aabroo_shop_domain", lambda: "aabroo.myshopify.com")
    monkeypatch.setattr(main, "load_aghaje_order_overrides", lambda: {"#AB1001": {
        "amount_received": 3200, "packaging_cost": 100, "delivery_cost": 200,
    }})
    monkeypatch.setattr(main, "load_aghaje_item_cost_overrides", lambda: {"variant:11": {"cost": 500}})
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
    assert b'placeholder="Password"' in client.get("/aabroo_portal").data
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


def test_aabroo_internal_ledger_renders_full_order_workflow(monkeypatch):
    monkeypatch.setattr(main, "_aabroo_fetch_orders", lambda: [_order()])
    monkeypatch.setattr(main, "_aabroo_shop_domain", lambda: "aabroo.myshopify.com")
    monkeypatch.setattr(main, "load_aghaje_order_overrides", lambda: {})
    monkeypatch.setattr(main, "load_aghaje_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "load_aghaje_order_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "_aabroo_settlements", lambda: [])

    response = main.app.test_client().get("/aabroo-orders")

    assert response.status_code == 200
    assert b"Aabroo Orders" in response.data
    assert b"#AB1001" in response.data
    assert b"Apply to selected" in response.data


def test_aabroo_order_update_persists_costs_and_ledger_fields(monkeypatch):
    saved = {"items": [], "defaults": [], "order": []}
    monkeypatch.setattr(main, "load_aghaje_item_cost_overrides", lambda: {})
    monkeypatch.setattr(main, "upsert_aghaje_order_item_cost_override", lambda *args: saved["items"].append(args) or True)
    monkeypatch.setattr(main, "upsert_aghaje_item_cost_override", lambda *args: saved["defaults"].append(args) or True)
    monkeypatch.setattr(main, "upsert_aghaje_order_override", lambda *args: saved["order"].append(args) or True)

    response = main.app.test_client().post("/aabroo-orders/update", json={
        "order_id": "#AB1001",
        "payment_status": "Paid",
        "delivery_status": "Delivered",
        "amount_received": 3200,
        "packaging_cost": 100,
        "delivery_cost": 200,
        "item_costs": [{"cost_key": "variant:11", "title": "Dress", "cost": 500, "product_id": 10, "variant_id": 11}],
    })

    assert response.status_code == 200
    assert saved["items"][0][:4] == ("#AB1001", "variant:11", "Dress", 500.0)
    assert saved["defaults"][0][:3] == ("variant:11", "Dress", 500.0)
    assert saved["order"][0] == ("#AB1001", 3200.0, 100.0, 200.0, "Paid", "Delivered")

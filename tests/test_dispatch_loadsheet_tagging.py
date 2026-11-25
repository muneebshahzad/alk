from pathlib import Path

import main


ROOT = Path(__file__).resolve().parents[1]
EMPLOYEE_TEMPLATE = (ROOT / "templates" / "employee_portal.html").read_text()
SCANNER_TEMPLATE = (ROOT / "templates" / "scanner.html").read_text()


def test_employee_load_sheet_saves_dispatch_tags_first():
    function = EMPLOYEE_TEMPLATE.split("async function createLoadSheet()", 1)[1].split(
        "function showLoadSheetViewer", 1
    )[0]
    assert function.index("applyQueueTags(false)") < function.index("fetch('/api/scan/load-sheet'")
    assert "warehouse_tag: warehouseTag" in EMPLOYEE_TEMPLATE


def test_legacy_scanner_load_sheet_saves_dispatch_tags_first():
    function = SCANNER_TEMPLATE.split("async function generateLoadSheet()", 1)[1].split(
        "// 4. Generate Report Logic", 1
    )[0]
    assert function.index("fetch('/api/apply_bulk_tag'") < function.index("fetch('/api/scan/load-sheet'")
    assert "tag_type: 'DISPATCHED'" in function
    assert "warehouse_tag: warehouseTag" in function


def test_both_scanners_offer_only_the_supported_warehouses():
    for template in (EMPLOYEE_TEMPLATE, SCANNER_TEMPLATE):
        assert 'value="Azam Market"' in template
        assert 'value="Engineer Town"' in template


def test_dispatch_mode_only_shows_the_load_sheet_action():
    assert "reportBtn.style.display = 'none'" in EMPLOYEE_TEMPLATE
    assert "reportBtn.style.display = 'block'" in EMPLOYEE_TEMPLATE
    assert "classList.toggle('hidden', mode === 'DISPATCHED')" in SCANNER_TEMPLATE


def test_legacy_bulk_tag_saves_selected_warehouse(monkeypatch):
    class Order:
        tags = "DigiDokaan"

        def save(self):
            return True

    order = Order()
    monkeypatch.setattr(main.shopify.Order, "find", lambda _order_id: order)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    response = main.app.test_client().post("/api/apply_bulk_tag", json={
        "order_ids": [123],
        "tag_type": "DISPATCHED",
        "warehouse_tag": "Engineer Town",
    })

    assert response.status_code == 200
    assert "Engineer Town" in order.tags
    assert "DISPATCHED (" in order.tags

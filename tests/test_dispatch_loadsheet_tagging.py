from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EMPLOYEE_TEMPLATE = (ROOT / "templates" / "employee_portal.html").read_text()
SCANNER_TEMPLATE = (ROOT / "templates" / "scanner.html").read_text()


def test_employee_load_sheet_saves_dispatch_tags_first():
    function = EMPLOYEE_TEMPLATE.split("async function createLoadSheet()", 1)[1].split(
        "function showLoadSheetViewer", 1
    )[0]
    assert function.index("applyQueueTags(false)") < function.index("fetch('/api/scan/load-sheet'")


def test_legacy_scanner_load_sheet_saves_dispatch_tags_first():
    function = SCANNER_TEMPLATE.split("async function generateLoadSheet()", 1)[1].split(
        "// 4. Generate Report Logic", 1
    )[0]
    assert function.index("fetch('/api/apply_bulk_tag'") < function.index("fetch('/api/scan/load-sheet'")
    assert "tag_type: 'DISPATCHED'" in function

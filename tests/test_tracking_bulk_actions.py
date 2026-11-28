from pathlib import Path


TEMPLATE = (Path(__file__).resolve().parents[1] / "templates" / "track.html").read_text()


def test_out_for_delivery_filter_has_no_mark_all_delivered_override():
    assert 'id="btnDeliver"' not in TEMPLATE
    assert "applyDeliveredToAll" not in TEMPLATE
    assert "Bulk Mark Delivered" not in TEMPLATE


def test_delivered_remains_available_for_explicitly_selected_orders():
    assert "bulkMark('Delivered')" in TEMPLATE


def test_all_orders_in_the_current_filtered_view_can_be_selected():
    assert 'id="selectViewBtn"' in TEMPLATE
    assert "function visibleOrderIds()" in TEMPLATE
    assert "function toggleSelectAllInView()" in TEMPLATE
    assert "card.style.display !== 'none'" in TEMPLATE
    assert "checkbox.checked = !deselect" in TEMPLATE

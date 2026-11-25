from pathlib import Path


TEMPLATE = (Path(__file__).resolve().parents[1] / "templates" / "pending.html").read_text()


def test_order_and_item_status_filters_are_independent():
    for control in (
        "showBookedOrders", "showUnbookedOrders",
        "showBookedItems", "showUnbookedItems",
        "showDispatchedOrders", "showDispatchedItems",
    ):
        assert f'id="{control}"' in TEMPLATE
    assert "bookedOrders: document.getElementById('showBookedOrders')" in TEMPLATE
    assert "bookedItems: document.getElementById('showBookedItems')" in TEMPLATE
    assert "dispatchedOrders: document.getElementById('showDispatchedOrders')" in TEMPLATE
    assert "dispatchedItems: document.getElementById('showDispatchedItems')" in TEMPLATE


def test_filterable_rows_expose_status_membership_and_visible_counts():
    assert 'class="order-card" data-booked=' in TEMPLATE
    assert 'class="summary-row" data-booked=' in TEMPLATE
    assert 'data-dispatched=' in TEMPLATE
    assert 'id="visibleOrderCount"' in TEMPLATE
    assert 'id="visibleItemCount"' in TEMPLATE
    assert "initializePendingFilters();" in TEMPLATE

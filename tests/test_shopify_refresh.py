import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
function = next(
    node for node in source.body
    if isinstance(node, ast.FunctionDef) and node.name == "shopify_order_query_parameters"
)
namespace = {
    "datetime": datetime,
    "timedelta": timedelta,
    "SHOPIFY_ORDER_LOOKBACK_DAYS": 30,
}
exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)


class ShopifyRefreshTests(unittest.TestCase):
    def test_refresh_includes_closed_orders_in_bounded_window(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        query = namespace["shopify_order_query_parameters"](now)

        self.assertEqual(query["status"], "any")
        self.assertEqual(query["limit"], 250)
        self.assertEqual(
            query["created_at_min"],
            "2026-08-25T12:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()

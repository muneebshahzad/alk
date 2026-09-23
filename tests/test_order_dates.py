import ast
from datetime import datetime
from pathlib import Path
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
functions = [
    node
    for node in source.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"parse_date_for_sort", "parse_date_timestamp"}
]
namespace = {"datetime": datetime}
exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class OrderDateTests(unittest.TestCase):
    def test_mixed_shopify_and_daraz_dates_sort_together(self):
        orders = [
            {"source": "Shopify", "date": "Sep 21, 2026"},
            {"source": "Daraz", "date": "2026-09-22T09:30:00+05:00"},
            {"source": "Unknown", "date": ""},
        ]

        result = sorted(
            orders,
            key=lambda order: namespace["parse_date_timestamp"](order["date"]),
            reverse=True,
        )

        self.assertEqual([order["source"] for order in result], ["Daraz", "Shopify", "Unknown"])


if __name__ == "__main__":
    unittest.main()

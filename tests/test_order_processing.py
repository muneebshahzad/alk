import ast
import asyncio
from pathlib import Path
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
function = next(
    node for node in source.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "safe_process_order"
)


class OrderProcessingTests(unittest.TestCase):
    def test_limiter_can_be_used_by_repeated_refresh_event_loops(self):
        processed = []

        async def process_order(session, order):
            await asyncio.sleep(0)
            processed.append(order)
            return order

        namespace = {"asyncio": asyncio, "process_order": process_order}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)

        async def refresh(offset):
            return await asyncio.gather(*(
                namespace["safe_process_order"](None, offset + number)
                for number in range(12)
            ))

        first = asyncio.run(refresh(0))
        second = asyncio.run(refresh(100))

        self.assertEqual(first, list(range(12)))
        self.assertEqual(second, list(range(100, 112)))
        self.assertEqual(len(processed), 24)


if __name__ == "__main__":
    unittest.main()

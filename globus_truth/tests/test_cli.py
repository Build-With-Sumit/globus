from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from globus_truth.__main__ import _read_json, main


class CliTests(unittest.TestCase):
    def test_bare_module_command_launches_safe_demo(self) -> None:
        with patch("globus_truth.__main__._serve", return_value=0) as serve:
            self.assertEqual(main([]), 0)
        args, kwargs = serve.call_args
        self.assertEqual(args[0].command, "demo")
        self.assertTrue(kwargs["load_demo"])
        self.assertEqual(args[0].host, "127.0.0.1")

    def test_cli_rejects_non_finite_json_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text('{"metadata":{"latency":NaN}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_json(str(path))


if __name__ == "__main__":
    unittest.main()

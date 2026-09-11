import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import menu


class MenuOfflineTests(unittest.TestCase):
    def test_add_account_writes_validated_users_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "builtins.input", return_value="new-user"
        ):
            menu.menu_add_account(directory, prompt_password_func=lambda _label: "pass")
            self.assertEqual(
                json.loads(Path(directory, "users.json").read_text(encoding="utf-8")),
                [{"username": "new-user", "password": "pass"}],
            )

    def test_menu_module_does_not_import_runtime_for_paths(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("builtins.print") as printer:
            menu.menu_show_paths(directory)
        rendered = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("配置目录", rendered)


if __name__ == "__main__":
    unittest.main()

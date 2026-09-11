import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


class ConfigResolutionTests(unittest.TestCase):
    def test_priority_cli_then_users_then_multi_env_then_single_env(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "users.json").write_text(
                json.dumps([{"username": "file-user", "password": "file-pass"}]),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "SWU_USERS": json.dumps([{"username": "multi-user", "password": "multi-pass"}]),
                    "SWU_USERNAME": "single-user",
                    "SWU_PASSWORD": "single-pass",
                },
                clear=True,
            ):
                self.assertEqual(
                    config.resolve_accounts("cli-user", "cli-pass", config_dir=directory).source,
                    "命令行参数",
                )
                self.assertEqual(
                    config.resolve_accounts(config_dir=directory).source,
                    "users.json",
                )

            Path(directory, "users.json").unlink()
            with mock.patch.dict(
                os.environ,
                {
                    "SWU_USERS": json.dumps([{"username": "multi-user", "password": "multi-pass"}]),
                    "SWU_USERNAME": "single-user",
                    "SWU_PASSWORD": "single-pass",
                },
                clear=True,
            ):
                self.assertEqual(config.resolve_accounts(config_dir=directory).source, "环境变量 SWU_USERS")

            with mock.patch.dict(
                os.environ,
                {"SWU_USERNAME": "single-user", "SWU_PASSWORD": "single-pass"},
                clear=True,
            ):
                self.assertEqual(
                    config.resolve_accounts(config_dir=directory).source,
                    "环境变量 SWU_USERNAME/SWU_PASSWORD",
                )

    def test_invalid_high_priority_source_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "users.json").write_text("{broken", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"SWU_USERNAME": "single-user", "SWU_PASSWORD": "single-pass"},
                clear=True,
            ):
                result = config.resolve_accounts(config_dir=directory)
            self.assertEqual(result.source, "users.json")
            self.assertEqual(result.accounts, [])
            self.assertTrue(result.errors)

    def test_empty_sources_fall_through(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "users.json").write_text("[]", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "SWU_USERS": "[]",
                    "SWU_USERNAME": "single-user",
                    "SWU_PASSWORD": "single-pass",
                },
                clear=True,
            ):
                result = config.resolve_accounts(config_dir=directory)
            self.assertEqual(result.source, "环境变量 SWU_USERNAME/SWU_PASSWORD")
            self.assertEqual(result.accounts[0]["username"], "single-user")

    def test_dotenv_is_loaded_before_resolution_without_overriding_process_env(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "SWU_USERNAME=from-file\nSWU_PASSWORD='file password'\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                result = config.resolve_accounts(config_dir=directory)
                self.assertEqual(result.accounts[0]["username"], "from-file")
                self.assertEqual(result.accounts[0]["password"], "file password")

            with mock.patch.dict(
                os.environ,
                {"SWU_USERNAME": "from-process", "SWU_PASSWORD": "process-password"},
                clear=True,
            ):
                result = config.resolve_accounts(config_dir=directory)
            self.assertEqual(result.accounts[0]["username"], "from-process")
            self.assertEqual(result.accounts[0]["password"], "process-password")


if __name__ == "__main__":
    unittest.main()

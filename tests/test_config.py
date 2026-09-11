import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


class ConfigResolutionTests(unittest.TestCase):
    def test_runtime_options_share_defaults_and_bounds(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            options = config.parse_runtime_options()
        self.assertEqual(
            options,
            config.RuntimeOptions(
                max_workers=3,
                max_rounds=3,
                retry_interval_seconds=300,
                run_deadline_seconds=900,
                push_deadline_seconds=60,
            ),
        )

        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_WORKERS": "32",
                "SWU_MAX_ROUNDS": "20",
                "SWU_RETRY_INTERVAL_SECONDS": "3600",
                "SWU_RUN_DEADLINE_SECONDS": "1",
                "SWU_PUSH_DEADLINE_SECONDS": "3600",
            },
            clear=True,
        ):
            options = config.parse_runtime_options()
        self.assertEqual(options.max_workers, 32)
        self.assertEqual(options.max_rounds, 20)
        self.assertEqual(options.retry_interval_seconds, 3600)
        self.assertEqual(options.run_deadline_seconds, 1)
        self.assertEqual(options.push_deadline_seconds, 3600)

        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_WORKERS": "0",
                "SWU_MAX_ROUNDS": "21",
                "SWU_RETRY_INTERVAL_SECONDS": "not-a-number",
                "SWU_RUN_DEADLINE_SECONDS": "3601",
                "SWU_PUSH_DEADLINE_SECONDS": "-1",
            },
            clear=True,
        ):
            options = config.parse_runtime_options()
            issues = config.runtime_parameter_issues()
        self.assertEqual(options.max_workers, 3)
        self.assertEqual(options.max_rounds, 3)
        self.assertEqual(options.retry_interval_seconds, 300)
        self.assertEqual(options.run_deadline_seconds, 900)
        self.assertEqual(options.push_deadline_seconds, 60)
        self.assertEqual(
            {issue.spec.env_name for issue in issues},
            {
                "SWU_MAX_WORKERS",
                "SWU_MAX_ROUNDS",
                "SWU_RETRY_INTERVAL_SECONDS",
                "SWU_RUN_DEADLINE_SECONDS",
                "SWU_PUSH_DEADLINE_SECONDS",
            },
        )

    def test_push_channel_table_drives_configuration_checks(self):
        environ = {
            "PUSH_DINGTALK_TOKEN": "ding-token",
            "PUSH_DINGTALK_SECRET": "",
            "PUSH_TELEGRAM_BOT_TOKEN": "telegram-token",
        }
        self.assertEqual(
            config.configured_push_channels(environ),
            ["DingTalk"],
        )
        self.assertEqual(
            config.push_configuration_errors(environ),
            ["Telegram 推送缺少 PUSH_TELEGRAM_CHAT_ID。"],
        )
        self.assertEqual(
            [channel.name for channel in config.PUSH_CHANNELS],
            ["DingTalk", "WeChat Work", "Bark", "ServerChan", "PushDeer", "Telegram"],
        )

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

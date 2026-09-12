"""打包元数据的一致性检查：版本号、控制台脚本与包布局。"""

import importlib
import re
import tomllib
import unittest
from pathlib import Path

import swu_checkin

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def project_metadata():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class PackagingMetadataTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self):
        # 发布 workflow 会校验 tag 与 pyproject.toml 一致，这里再保证运行时代码
        # 里的 __version__ 不会落后。
        self.assertEqual(swu_checkin.__version__, project_metadata()["project"]["version"])

    def test_console_script_points_at_the_cli_entry_point(self):
        scripts = project_metadata()["project"]["scripts"]
        self.assertEqual(scripts["swu-checkin"], "swu_checkin.cli:main")

        module_name, attribute = scripts["swu-checkin"].split(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, attribute)))

    def test_src_layout_declares_every_subpackage(self):
        package = project_metadata()["tool"]["setuptools"]
        self.assertEqual(package["package-dir"], {"": "src"})
        self.assertEqual(package["packages"]["find"]["where"], ["src"])

        for name in (
            "swu_checkin",
            "swu_checkin.api",
            "swu_checkin.auth",
        ):
            with self.subTest(package=name):
                module = importlib.import_module(name)
                self.assertTrue(module.__file__)

    def test_ci_matrix_covers_the_declared_python_versions(self):
        # README 与 pyproject 声明 3.11+，CI 必须真的跑这两个版本，
        # 否则兼容性声明没有证据。
        requires_python = project_metadata()["project"]["requires-python"]
        self.assertEqual(requires_python, ">=3.11")

        workflow = (REPO_ROOT / ".github" / "workflows" / "pr-ci.yml").read_text(encoding="utf-8")
        self.assertIn('python-version: ["3.11", "3.12"]', workflow)
        self.assertIn(f'"{requires_python.removeprefix(">=")}"', workflow)

    def test_every_documented_test_command_works_from_a_clean_checkout(self):
        # 实现位于 src/，因此 `unittest discover` 必须带 `-t .`（tests 作为包
        # 被导入时才会把 src 加进 sys.path）。这条用例防止文档和 CI 再次写错。
        targets = [REPO_ROOT / "README.md"]
        targets += sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        commands = []
        for path in targets:
            for command in re.findall(r"unittest discover[^\n`]*", path.read_text(encoding="utf-8")):
                commands.append((path.name, command))
        self.assertTrue(commands, "没有找到任何 unittest discover 命令")
        for name, command in commands:
            with self.subTest(path=name, command=command):
                self.assertIn("-t .", command)

    def test_container_keeps_the_non_root_runtime_and_compatibility_entry(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)
        # 非 root 用户必须能在共享路径找到 Chromium。
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright", dockerfile)
        self.assertIn('CMD ["python", "check_in.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()

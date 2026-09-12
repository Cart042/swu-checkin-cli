"""打包元数据的一致性检查：版本号、控制台脚本与包布局。"""

import importlib
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


if __name__ == "__main__":
    unittest.main()

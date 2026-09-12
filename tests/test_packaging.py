"""打包元数据的一致性检查：版本号、控制台脚本与包布局。"""

import importlib
import re
import tomllib
import unittest
from pathlib import Path

import swu_checkin

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
# 这两个文件只存在于仓库检出里，不会进 sdist；发布出去的源码包仍然要能跑测试，
# 所以依赖它们的仓库级断言在 sdist 中跳过。
PR_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-ci.yml"
CHECKIN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "swu-check.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"


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

    @unittest.skipUnless(PR_CI_WORKFLOW.is_file(), "只有仓库检出才带 CI 工作流")
    def test_ci_matrix_covers_the_declared_python_versions(self):
        # README 与 pyproject 声明 3.11+，CI 必须真的跑这两个版本，
        # 否则兼容性声明没有证据。
        requires_python = project_metadata()["project"]["requires-python"]
        self.assertEqual(requires_python, ">=3.11")

        workflow = PR_CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('python-version: ["3.11", "3.12"]', workflow)
        self.assertIn(f'"{requires_python.removeprefix(">=")}"', workflow)

    @unittest.skipUnless(PR_CI_WORKFLOW.is_file(), "只有仓库检出才带 CI 工作流")
    def test_ci_check_tools_match_the_declared_dev_versions(self):
        # ruff / mypy 在 CI 里是按写死的版本安装的，而本地开发装的是 pyproject
        # 的 dev 附加依赖。两边漂移时会出现「本地绿、CI 红」或者反过来，而且
        # Dependabot 只会改其中一边，所以在这里锁死。
        dev = project_metadata()["project"]["optional-dependencies"]["dev"]
        pinned = {}
        for spec in dev:
            name, separator, version = spec.partition("==")
            if separator:
                pinned[name] = version
        self.assertEqual(sorted(pinned), ["mypy", "ruff"])

        workflow = PR_CI_WORKFLOW.read_text(encoding="utf-8")
        for name, version in sorted(pinned.items()):
            with self.subTest(tool=name):
                self.assertIn(f'"{name}=={version}"', workflow)

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

    @unittest.skipUnless(DOCKERFILE.is_file(), "只有仓库检出才带 Dockerfile")
    def test_container_keeps_the_non_root_runtime_and_compatibility_entry(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)
        # 非 root 用户必须能在共享路径找到 Chromium。
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright", dockerfile)
        self.assertIn('CMD ["python", "check_in.py"]', dockerfile)

    @unittest.skipUnless(CHECKIN_WORKFLOW.is_file(), "只有仓库检出才带签到工作流")
    def test_scheduled_check_in_requires_the_explicit_enable_switch(self):
        # 每天自动打卡必须先手动打开仓库变量开关，未开启时定时运行不得打卡；
        # 手动触发要一直可用，方便临时补打卡。这条用例防止后续改动把开关去掉。
        workflow = CHECKIN_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("- cron:", workflow)
        self.assertIn("vars.SWU_CHECKIN_ENABLED == 'true'", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' ||", workflow)

    def test_sdist_manifest_keeps_the_repo_self_testable(self):
        # setuptools 默认只把 tests/test*.py 放进 sdist，不会带 tests/fixtures/，
        # 那样发布出去的源码包里 fixture 回归测试必然失败。MANIFEST.in 保证
        # sdist 仍然是一份能直接跑测试的仓库快照。
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for pattern in (
            "include check_in.py",
            "include requirements.txt",
            "graft docs",
            "graft scripts",
            "graft tests",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, manifest)

        # 这些路径必须真实存在，否则 MANIFEST.in 只是空挂一条规则。
        for relative in (
            "check_in.py",
            "requirements.txt",
            "tests/fixtures/login/idm_login_form.html",
            "docs/branch-protection.md",
            "scripts/smoke_runtime.py",
        ):
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())


if __name__ == "__main__":
    unittest.main()

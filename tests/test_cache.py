"""Token 缓存持久化（权限、原子写与 JSON 结构）的离线回归测试。"""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from swu_checkin import cache


class TokenCacheTests(unittest.TestCase):
    def test_cache_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "nested" / ".token_cache.json"
            cache.save_cached_token("student", "secret-token", str(cache_path))

            self.assertEqual(cache.load_cached_token("student", str(cache_path)), "secret-token")
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cache_path.parent.stat().st_mode), 0o700)
            self.assertEqual(list(cache_path.parent.glob(".token-cache-*")), [])
            self.assertEqual(
                json.loads(cache_path.read_text(encoding="utf-8")),
                {"student": "secret-token"},
            )


if __name__ == "__main__":
    unittest.main()

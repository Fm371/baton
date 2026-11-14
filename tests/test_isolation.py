import unittest

from baton.config import DEFAULT_TEST_PATHS
from baton.isolation import outside_test_paths


class TestPathsTest(unittest.TestCase):
    def test_default_globs(self):
        inside = ["tests/test_a.py", "tests/unit/x.py", "src/__tests__/a.tsx", "__tests__/b.ts",
                  "src/a.test.ts", "a.test.tsx", "app/test_wallet.py", "test_root.py"]
        outside = ["app/wallet.py", "src/a.ts", "testsx/a.py", "src/__tests__x/a.ts", "app/wallet_test.py"]
        self.assertEqual(outside_test_paths(inside + outside, DEFAULT_TEST_PATHS), outside)


if __name__ == "__main__":
    unittest.main()

"""A guarda do `tests/live/readonly_check.py` recusa tudo que não seja GET ou o POST do WIQL."""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "tests" / "live"))
import readonly_check  # noqa: E402
from adoplug.http import HttpError  # noqa: E402


class Guard(unittest.TestCase):
    def setUp(self):
        self.h = readonly_check.ReadOnlyHttp(None, ["OrgA"])

    def test_recusa_escrita(self):
        for method, path in (("PATCH", "/OrgA/_apis/wit/workitems/1"), ("POST", "/OrgA/_apis/wit/workitems/$Task"),
                             ("DELETE", "/OrgA/_apis/wit/workitems/1"), ("POST", "/OrgA/_apis/wit/attachments")):
            with self.subTest(method=method, path=path), self.assertRaises(HttpError) as cm:
                self.h._send(method, "https://dev.azure.com" + path, "OrgA", None, None, None, None, False)
            self.assertEqual(cm.exception.reason_code, "READONLY_GUARD")


if __name__ == "__main__":
    unittest.main()

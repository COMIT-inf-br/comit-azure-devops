"""Prévia: hash canônico, validade, campos legíveis e journal."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from adoplug import preview  # noqa: E402

PLAN = {"project_id": "p", "type": "Task", "patch": [{"op": "add", "path": "/fields/System.Title", "value": "T"},
                                                      {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": 2}]}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class HashTest(unittest.TestCase):
    def test_ordem_das_chaves_nao_muda(self):
        a = preview.compute_hash("create", "OrgA", {"b": 1, "a": [1, {"y": 2, "x": "ç"}]})
        b = preview.compute_hash("create", "OrgA", {"a": [1, {"x": "ç", "y": 2}], "b": 1})
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_tipo_do_valor_muda(self):
        self.assertNotEqual(preview.compute_hash("create", "OrgA", {"v": 2}), preview.compute_hash("create", "OrgA", {"v": "2"}))
        self.assertNotEqual(preview.compute_hash("create", "OrgA", PLAN), preview.compute_hash("update", "OrgA", PLAN))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="adoplug-prev-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        self.store = preview.PreviewStore(self.dir / "previews", clock=self.clock)
        self.rec = self.store.create("create", "OrgA", PLAN, {"type": "Task", "title": "T"}, {"x": 1})
        self.claims = {"operation": "create", "org": "OrgA", "type": "Task", "title": "T"}

    def test_grava_e_confere(self):
        rec = self.store.check_apply(self.rec["preview_id"], self.rec["hash"], self.claims)
        self.assertEqual(rec["plan"], PLAN)
        self.assertEqual(rec["state"], "prepared")
        f = self.dir / "previews" / f"{self.rec['preview_id']}.json"
        if os.name != "nt":
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_campos_legiveis_tolerantes_a_caixa_e_espaco(self):
        c = dict(self.claims, org="orga", title="  t ")
        self.store.check_apply(self.rec["preview_id"], self.rec["hash"].upper(), c)

    def assertCode(self, code, *a):
        with self.assertRaises(preview.PreviewError) as cm:
            self.store.check_apply(*a)
        self.assertEqual(cm.exception.reason_code, code)

    def test_recusas(self):
        pid, h = self.rec["preview_id"], self.rec["hash"]
        self.assertCode("HASH_MISMATCH", pid, "0" * 64, self.claims)
        self.assertCode("PREVIEW_MISMATCH", pid, h, dict(self.claims, title="Outro"))
        self.assertCode("PREVIEW_MISMATCH", pid, h, dict(self.claims, type=None))
        self.assertCode("PREVIEW_MISMATCH", pid, h, dict(self.claims, operation="update"))
        self.assertCode("PREVIEW_NOT_FOUND", "../../etc/passwd", h, self.claims)
        self.assertCode("PREVIEW_NOT_FOUND", "f" * 32, h, self.claims)

    def test_vencida(self):
        self.clock.t += preview.TTL_SECONDS + 1
        self.assertCode("PREVIEW_EXPIRED", self.rec["preview_id"], self.rec["hash"], self.claims)

    def test_estados(self):
        pid, h = self.rec["preview_id"], self.rec["hash"]
        rec = self.store.load(pid)
        self.store.transition(rec, "applying")
        self.assertCode("NEEDS_RECONCILE", pid, h, self.claims)
        self.store.transition(rec, "ambiguous")
        self.assertCode("NEEDS_RECONCILE", pid, h, self.claims)
        self.store.transition(rec, "not_applied")
        self.assertCode("PREVIEW_CLOSED", pid, h, self.claims)
        self.store.transition(rec, "applied", item_id=5)
        self.assertCode("ALREADY_APPLIED", pid, h, self.claims)
        j = self.store.load(pid)
        self.assertEqual([e["state"] for e in j["journal"]], ["prepared", "applying", "ambiguous", "not_applied", "applied"])
        self.assertIn("applying_at", j["result"])

    def test_parcial_nao_vence(self):
        rec = self.store.load(self.rec["preview_id"])
        self.store.transition(rec, "partial", upload_url="u")
        self.clock.t += preview.TTL_SECONDS + 1
        self.store.check_apply(self.rec["preview_id"], self.rec["hash"], self.claims)

    def test_arquivo_adulterado(self):
        f = self.dir / "previews" / f"{self.rec['preview_id']}.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        d["plan"]["patch"][0]["value"] = "Outro"
        f.write_text(json.dumps(d), encoding="utf-8")
        self.assertCode("PREVIEW_TAMPERED", self.rec["preview_id"], self.rec["hash"], self.claims)

    def test_limpeza_de_7_dias(self):
        old = self.rec["preview_id"]
        self.clock.t += preview.RETENTION_SECONDS + 1
        new = self.store.create("create", "OrgA", PLAN, {"type": "Task", "title": "T"})
        self.assertFalse((self.dir / "previews" / f"{old}.json").exists())
        self.assertTrue((self.dir / "previews" / f"{new['preview_id']}.json").exists())

    def test_legiveis_obrigatorios(self):
        with self.assertRaises(preview.PreviewError):
            self.store.create("create", "OrgA", PLAN, {"type": "Task"})
        with self.assertRaises(preview.PreviewError):
            self.store.create("delete", "OrgA", PLAN, {})


if __name__ == "__main__":
    unittest.main()

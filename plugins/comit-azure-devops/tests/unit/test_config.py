import json
import os
import stat
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402
from adoplug import config  # noqa: E402
from adoplug.config import ConfigError  # noqa: E402


class NormalizeOrgTest(unittest.TestCase):
    def test_nome(self):
        self.assertEqual(config.normalize_org("Minha-Org"), "Minha-Org")

    def test_url_de_item(self):
        pid = str(uuid.uuid4())
        self.assertEqual(config.normalize_org(f"https://dev.azure.com/Minha-Org/{pid}/_workitems/edit/12"),
                         "Minha-Org")

    def test_url_sem_esquema(self):
        self.assertEqual(config.normalize_org("dev.azure.com/OrgX/Projeto"), "OrgX")

    def test_recusa_visualstudio_e_outros_hosts(self):
        for u in ("https://orgx.visualstudio.com/P", "https://evil.example/OrgX", "http://dev.azure.com/OrgX",
                  "https://dev.azure.com:8443/OrgX", "https://u:p@dev.azure.com/OrgX"):
            with self.subTest(u=u), self.assertRaises(ConfigError) as cm:
                config.normalize_org(u)
            self.assertEqual(cm.exception.reason_code, "UNSUPPORTED_HOST")

    def test_recusa_nome_invalido(self):
        for n in ("", "-org", "org-", "a b", "org;rm", "x" * 60):
            with self.subTest(n=n), self.assertRaises(ConfigError):
                config.normalize_org(n)


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.path = self.h.home / "cfg" / "config.json"

    def test_ausente_devolve_padrao(self):
        c = config.load_config(self.path)
        self.assertEqual(c["orgs"], {})
        self.assertIsNone(c["default_org"])

    def test_ida_e_volta(self):
        c = config.set_org(config.default_config(), "OrgA", tenant=str(uuid.uuid4()))
        config.save_config(c, self.path)
        d = config.load_config(self.path)
        self.assertEqual(d["default_org"], "OrgA")
        self.assertEqual(d["orgs"]["OrgA"]["auth"], "az")
        self.assertEqual(d["orgs"]["OrgA"]["pat_ref"]["os_service"], "comit-azure-devops")
        self.assertEqual(d["orgs"]["OrgA"]["pat_ref"]["os_account"], "OrgA")

    @unittest.skipIf(os.name == "nt", "permissão POSIX")
    def test_permissao_0600(self):
        config.save_config(config.default_config(), self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_recusa_chave_de_segredo(self):
        for bad in ({"pat": "x"}, {"orgs": {"OrgA": {"token": "x"}}},
                    {"orgs": {"OrgA": {"pat_ref": {"password": "x"}}}}, {"x": [{"Secret": 1}]}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                config.save_config({**config.default_config(), **bad}, self.path)
            self.assertEqual(cm.exception.reason_code, "SECRET_IN_CONFIG")
        self.assertFalse(self.path.exists())

    def test_load_recusa_segredo_no_arquivo(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"orgs": {"OrgA": {"pat": "x"}}}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            config.load_config(self.path)

    def test_set_org_recusa_segredo(self):
        with self.assertRaises(ConfigError):
            config.set_org(config.default_config(), "OrgA", token="x")

    def test_json_invalido(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            config.load_config(self.path)
        self.assertEqual(cm.exception.reason_code, "CONFIG_INVALID")

    def test_auth_invalido(self):
        c = config.set_org(config.default_config(), "OrgA", auth="env")
        with self.assertRaises(ConfigError):
            config.save_config(c, self.path)

    def test_find_org_sem_maiusculas(self):
        c = config.set_org(config.default_config(), "OrgA")
        self.assertEqual(config.find_org(c, "orga"), "OrgA")
        self.assertIsNone(config.find_org(c, "OrgB"))
        self.assertEqual(config.configured_orgs(c), ("OrgA",))
        with self.assertRaises(ConfigError) as cm:
            config.get_org(c, "OrgB")
        self.assertEqual(cm.exception.reason_code, "ORG_NOT_CONFIGURED")

    def test_set_org_mescla_pat_ref(self):
        c = config.set_org(config.default_config(), "OrgA")
        c = config.set_org(c, "orga", pat_ref={"nordpass_title": "ADO_PAT_ORGA"})
        e = c["orgs"]["OrgA"]["pat_ref"]
        self.assertEqual(e["nordpass_title"], "ADO_PAT_ORGA")
        self.assertEqual(e["os_service"], "comit-azure-devops")


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.learned = self.h.home / "learned"
        self.embedded = self.h.root / "embedded"
        self.embedded.mkdir()

    def test_ausente(self):
        p, src = config.load_profile("OrgA", self.learned, self.embedded)
        self.assertIsNone(p)
        self.assertEqual(src, "absent")

    def test_embutido(self):
        (self.embedded / "OrgA.json").write_text('{"org": "OrgA", "v": "e"}', encoding="utf-8")
        p, src = config.load_profile("orga", self.learned, self.embedded)
        self.assertEqual((p["v"], src), ("e", "embedded"))

    def test_aprendido_prevalece(self):
        (self.embedded / "OrgA.json").write_text('{"v": "e"}', encoding="utf-8")
        path = config.save_learned_profile("OrgA", {"v": "l"}, self.learned)
        self.assertTrue(str(path).startswith(str(self.learned)))
        p, src = config.load_profile("OrgA", self.learned, self.embedded)
        self.assertEqual((p["v"], src), ("l", "learned"))

    def test_aprendido_recusa_segredo(self):
        with self.assertRaises(ConfigError):
            config.save_learned_profile("OrgA", {"auth": {"token": "x"}}, self.learned)

    def test_nome_de_org_invalido(self):
        with self.assertRaises(ConfigError):
            config.load_profile("../x", self.learned, self.embedded)


if __name__ == "__main__":
    unittest.main()

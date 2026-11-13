"""CLI de terminal: doctor, setup-org, login, pat-set, pat-check e discover. O PAT nunca aparece na saída."""
import base64
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes", "tests/unit"):
    sys.path.insert(0, str(PLUGIN / sub))
import ado_cli  # noqa: E402
import fakelib  # noqa: E402
import test_tools  # noqa: E402
from adoplug import config as cfgmod  # noqa: E402

CLI = PLUGIN / "server" / "ado_cli.py"


class CliBase(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.e = test_tools.Env(self, **self.ENV)
        self.pat = fakelib.sentinel("Pat")
        self.out = io.StringIO()

    def run_cli(self, *argv, pat=None, tty=True, **kw):
        asked = []

        def fake_getpass(prompt):
            asked.append(prompt)
            return pat

        self.asked = asked
        return ado_cli.main(list(argv), ctx=self.e.ctx, out=self.out, getpass_fn=fake_getpass,
                            is_tty=lambda: tty, **kw)

    def cfg(self):
        return cfgmod.load_config(cfgmod.paths.config_file(self.e.cdir))

    def text(self):
        return self.out.getvalue()

    def tearDown(self):
        text = self.text() + self.e.leak_text()
        for secret in (self.pat, self.e.tok):
            for f in fakelib.leak_forms(secret):
                self.assertNotIn(f, text)
        self.assertNotIn(str(len(self.pat)), self.text().replace("http=200", ""))  # nem o tamanho


class DoctorTest(unittest.TestCase):
    def test_ok(self):
        e = test_tools.Env(self, az=True)
        out = io.StringIO()
        rc = ado_cli.main(["doctor"], ctx=e.ctx, out=out)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("[ok] az: versão 9.9.9", out.getvalue())
        self.assertIn("login=ok", out.getvalue())
        self.assertIn("remoto=ok", out.getvalue())
        self.assertIn("resultado=ok", out.getvalue())

    def test_offline_e_alias_status(self):
        e = test_tools.Env(self, az=True)
        out = io.StringIO()
        self.assertEqual(ado_cli.main(["status", "--offline", "--org", "OrgA"], ctx=e.ctx, out=out), 0)
        self.assertEqual(e.ado.requests, [])
        self.assertNotIn("remoto", out.getvalue())

    def test_pendencias(self):
        e = test_tools.Env(self, configured=False)
        out = io.StringIO()
        self.assertEqual(ado_cli.main(["doctor"], ctx=e.ctx, out=out), 1)
        self.assertIn("NO_ORG", out.getvalue())
        self.assertIn("resultado=pendencias", out.getvalue())

    def test_config_invalida(self):
        e = test_tools.Env(self)
        cfgmod.paths.config_file(e.cdir).write_text("{", encoding="utf-8")
        e.ctx.reload()
        out = io.StringIO()
        self.assertEqual(ado_cli.main(["doctor"], ctx=e.ctx, out=out), 1)
        self.assertIn("erro=CONFIG_INVALID", out.getvalue())


class SetupLoginTest(CliBase):
    ENV = {"configured": False, "az": True}

    def test_setup_org(self):
        self.assertEqual(self.run_cli("setup-org", "--org", "https://dev.azure.com/OrgA/Projeto/_workitems/edit/5"), 0)
        self.assertIn("org=OrgA tenant=descoberto", self.text())
        self.assertEqual(self.cfg()["orgs"]["OrgA"]["tenant"], test_tools.TENANT)

    def test_setup_org_invalida(self):
        self.assertEqual(self.run_cli("setup-org", "--org", "https://x.visualstudio.com/"), 1)
        self.assertIn("erro=UNSUPPORTED_HOST", self.text())

    def test_login_device_code(self):
        self.run_cli("setup-org", "--org", "OrgA")
        self.out = io.StringIO()
        rc = self.run_cli("login", "--org", "OrgA", "--device-code", sleep=time.sleep)
        self.assertEqual(rc, 0, self.text())
        self.assertIn("Abra https://microsoft.com/devicelogin e digite o código FAKE12345.", self.text())
        self.assertIn("login=ok", self.text())

    def test_login_org_desconhecida(self):
        self.assertEqual(self.run_cli("login", "--org", "OrgA"), 1)
        self.assertIn("erro=ORG_NOT_CONFIGURED", self.text())


class PatTest(CliBase):
    def test_pat_set_cofre_do_so(self):
        rc = self.run_cli("pat-set", "--org", "OrgA", pat=self.pat)
        self.assertEqual(rc, 0, self.text())
        t = self.text()
        self.assertIn("gravado=sim cofre=keychain", t)
        self.assertIn("fonte=keychain encontrado=sim", t)
        self.assertIn("http=200", t)
        self.assertIn("auth=pat", t)
        self.assertEqual(self.cfg()["orgs"]["OrgA"]["auth"], "pat")
        self.assertEqual(len(self.asked), 1)
        # o cofre falso guarda o valor; a config não
        self.assertIn(self.pat, Path(self.e.keychain).read_text(encoding="utf-8"))
        # o GET de conferência usou o PAT como Basic
        last = self.e.ado.requests[-1]
        self.assertEqual(last["path"], "/OrgA/_apis/connectionData")
        self.assertEqual(last["query"]["api-version"], "7.1-preview.1")
        self.assertEqual(last["headers"]["authorization"], "Basic " + base64.b64encode(f":{self.pat}".encode()).decode())
        # o valor não passou pelo argv do security
        for entry in self.e.h.log(Path(self.e.keychain)):
            self.assertNotIn(self.pat, " ".join(entry["argv"]))

    def test_pat_set_sem_terminal(self):
        rc = self.run_cli("pat-set", "--org", "OrgA", pat=self.pat, tty=False)
        self.assertEqual(rc, 1)
        self.assertIn("erro=NO_TTY", self.text())
        self.assertEqual(self.asked, [])
        self.assertEqual(self.cfg()["orgs"]["OrgA"]["auth"], "az")

    def test_pat_set_vazio(self):
        self.assertEqual(self.run_cli("pat-set", "--org", "OrgA", pat="  "), 1)
        self.assertIn("erro=EMPTY_PAT", self.text())

    def test_pat_set_org_fora_da_config(self):
        rc = self.run_cli("pat-set", "--org", "OrgNova", pat=self.pat)
        # o cofre grava; o ADO falso só conhece OrgA, então a conferência falha e o modo não muda
        self.assertEqual(rc, 1)
        self.assertIn("gravado=sim", self.text())
        self.assertNotIn("OrgNova", self.cfg()["orgs"])

    def test_pat_set_nordpass_so_registra_a_referencia(self):
        e = test_tools.Env(self, np_items=[{"id": "item-1", "title": "ADO_PAT_ORGA", "password": self.pat}])
        self.e = e
        rc = self.run_cli("pat-set", "--org", "OrgA", "--nordpass-title", "ADO_PAT_ORGA", "--nordpass-folder", "Trabalho")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.asked, [])  # não pede valor
        entry = self.cfg()["orgs"]["OrgA"]
        self.assertEqual((entry["auth"], entry["pat_ref"]["nordpass_title"], entry["pat_ref"]["nordpass_folder"]),
                         ("pat", "ADO_PAT_ORGA", "Trabalho"))
        self.assertIn("referencia=nordpass gravado=sim", self.text())
        self.assertIn("fonte=nordpass encontrado=sim", self.text())
        self.assertIn("http=200", self.text())
        self.assertEqual(json.loads(Path(self.e.keychain).read_text(encoding="utf-8")), {})

    def test_pat_set_nordpass_travado(self):
        e = self.e
        np_cmd, _ = e.h.np(mode="locked", items=[])
        e.ctx.vault.np_cmd = np_cmd
        rc = self.run_cli("pat-set", "--org", "OrgA", "--nordpass-title", "ADO_PAT_ORGA")
        self.assertEqual(rc, 1)
        self.assertIn("motivo=NP_LOCKED", self.text())
        self.assertIn("np unlock", self.text())
        self.assertEqual(self.cfg()["orgs"]["OrgA"]["auth"], "az")  # modo só muda com PAT validado
        self.assertEqual(self.cfg()["orgs"]["OrgA"]["pat_ref"]["nordpass_title"], "ADO_PAT_ORGA")

    def test_pat_set_pasta_sem_titulo(self):
        self.assertEqual(self.run_cli("pat-set", "--org", "OrgA", "--nordpass-folder", "X", pat=self.pat), 1)
        self.assertIn("erro=INVALID_ARGUMENTS", self.text())

    def test_pat_check_sem_pat(self):
        rc = self.run_cli("pat-check", "--org", "OrgA")
        self.assertEqual(rc, 1)
        self.assertIn("encontrado=não motivo=PAT_NOT_FOUND", self.text())
        self.assertEqual(self.e.ado.requests, [])

    def test_pat_check_nao_muda_a_config(self):
        self.run_cli("pat-set", "--org", "OrgA", pat=self.pat)
        before = self.cfg()
        self.out = io.StringIO()
        self.assertEqual(self.run_cli("pat-check", "--org", "OrgA"), 0)
        self.assertEqual(self.cfg(), before)

    def test_pat_check_http_recusado(self):
        self.run_cli("pat-set", "--org", "OrgA", pat=self.pat)
        self.out = io.StringIO()
        orig = self.e.ado.dispatch
        self.e.ado.dispatch = lambda *a, **k: (401, {"message": "TF400813"})
        self.addCleanup(setattr, self.e.ado, "dispatch", orig)
        self.assertEqual(self.run_cli("pat-check", "--org", "OrgA"), 1)
        self.assertIn("http=401", self.text())


class DiscoverTest(CliBase):
    ENV = {"org": "ORG-EXEMPLO", "discovery": True, "profile": False}

    def test_discover_e_save(self):
        rc = self.run_cli("discover", "--org", "ORG-EXEMPLO", "--project", test_tools.PROJ)
        self.assertEqual(rc, 0, self.text())
        prof = json.loads(self.text())
        self.assertEqual(prof["org"], "ORG-EXEMPLO")
        self.assertEqual(list((self.e.cdir / "profiles").iterdir()), [])
        self.out = io.StringIO()
        self.assertEqual(self.run_cli("discover", "--org", "ORG-EXEMPLO", "--project", test_tools.PROJ, "--save"), 0)
        self.assertTrue((self.e.cdir / "profiles" / "ORG-EXEMPLO.json").is_file())
        self.assertIn("perfil=gravado", self.text())

    def test_projeto_inexistente(self):
        self.assertEqual(self.run_cli("discover", "--org", "ORG-EXEMPLO", "--project", "Nada"), 1)
        self.assertIn("erro=PROJECT_NOT_FOUND", self.text())


class ProcessTest(unittest.TestCase):
    def test_ajuda_e_saida_utf8(self):
        r = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        for cmd in ("doctor", "pat-set", "pat-check", "discover", "login", "setup-org", "status"):
            self.assertIn(cmd.encode(), r.stdout)

    def test_sem_comando(self):
        r = subprocess.run([sys.executable, str(CLI)], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 2)

    def test_nao_ha_argumento_para_o_valor_do_pat(self):
        p = ado_cli.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["pat-set", "--org", "X", "--pat", "valor"])
        with self.assertRaises(SystemExit):
            p.parse_args(["pat-set", "--org", "X", "--value", "valor"])


if __name__ == "__main__":
    unittest.main()

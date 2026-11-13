import base64
import os
import re
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402
from adoplug import config  # noqa: E402
from adoplug.auth import AuthError, AuthManager  # noqa: E402
from adoplug.azcli import AzCli, AzError  # noqa: E402
from adoplug.vault import VaultTools  # noqa: E402

class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class AuthBase(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.tok = fakelib.sentinel()
        self.pat = fakelib.sentinel("Pat")
        self.tenant = str(uuid.uuid4())
        self.clock = Clock()

    def manager(self, auth="az", az_state=None, keychain=None, np_items=None, **org):
        cfg = config.set_org(config.default_config(), "OrgA", auth=auth, tenant=self.tenant, **org)
        st = {"token": self.tok, "tenant": self.tenant, "expires_on": int(self.clock.t) + 3600}
        st.update(az_state or {})
        self.az_cmd, self.az_state = self.h.az(**st)
        np_cmd, _ = self.h.np(items=np_items or []) if np_items is not None else ([], None)
        sec_cmd, _ = self.h.security(keychain)
        tools = VaultTools(np_cmd=np_cmd, security_cmd=sec_cmd, secret_tool_cmd=[], platform="darwin")
        return AuthManager(cfg, az_factory=lambda: AzCli(self.az_cmd), vault_tools=tools, clock=self.clock)

    def az_calls(self):
        return [e for e in self.h.log(self.az_state) if e["argv"][:2] == ["account", "get-access-token"]]


class AzModeTest(AuthBase):
    def test_token_do_az_com_tenant(self):
        m = self.manager()
        c = m.get_credential("orga")
        self.assertEqual((c.kind, c.source, c.secret.reveal()), ("az", "az", self.tok))
        self.assertEqual(c.header().reveal(), "Bearer " + self.tok)
        self.assertEqual(m.get_token("OrgA").reveal(), self.tok)
        argv = self.az_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tenant") + 1], self.tenant)
        self.assertNotIn(self.tok, repr(c))

    def test_cache_em_memoria(self):
        m = self.manager()
        m.get_credential("OrgA")
        m.get_credential("OrgA")
        self.assertEqual(len(self.az_calls()), 1)

    def test_renova_perto_de_vencer(self):
        m = self.manager()
        m.get_credential("OrgA")
        self.clock.t += 3600 - 200  # dentro da margem de 5 min
        m.get_credential("OrgA")
        self.assertEqual(len(self.az_calls()), 2)

    def test_invalidate(self):
        m = self.manager()
        m.get_credential("OrgA")
        m.invalidate("OrgA")
        m.get_credential("OrgA")
        self.assertEqual(len(self.az_calls()), 2)

    def test_known_secrets(self):
        m = self.manager()
        m.get_credential("OrgA")
        self.assertEqual([s.reveal() for s in m.known_secrets()], [self.tok])

    def test_falha_do_az_nao_cai_para_pat(self):
        m = self.manager(az_state={"mode": "not_logged_in"}, keychain={f"comit-azure-devops\tOrgA": self.pat})
        with self.assertRaises(AuthError) as cm:
            m.get_credential("OrgA")
        self.assertEqual(cm.exception.reason_code, "AZ_NOT_LOGGED_IN")

    def test_org_nao_configurada(self):
        m = self.manager()
        with self.assertRaises(AuthError) as cm:
            m.get_credential("OutraOrg")
        self.assertEqual(cm.exception.reason_code, "ORG_NOT_CONFIGURED")
        self.assertEqual(self.az_calls(), [])

    def test_az_ausente(self):
        def nao_acha():
            raise AzError("AZ_NOT_FOUND", "sem az")
        cfg = config.set_org(config.default_config(), "OrgA")
        with self.assertRaises(AuthError) as cm:
            AuthManager(cfg, az_factory=nao_acha).get_credential("OrgA")
        self.assertEqual(cm.exception.reason_code, "AZ_NOT_FOUND")


class PatModeTest(AuthBase):
    def test_pat_do_keychain(self):
        m = self.manager(auth="pat", keychain={"comit-azure-devops\tOrgA": self.pat})
        c = m.get_credential("OrgA")
        self.assertEqual((c.kind, c.source), ("pat", "keychain"))
        want = "Basic " + base64.b64encode((":" + self.pat).encode()).decode()
        self.assertEqual(c.header().reveal(), want)
        self.assertEqual(self.az_calls(), [])  # modo pat não chama o az

    def test_pat_do_nordpass_primeiro(self):
        items = [{"id": "id-1", "title": "PAT_ORGA", "password": self.pat}]
        m = self.manager(auth="pat", np_items=items, keychain={"comit-azure-devops\tOrgA": "outro-valor"},
                         pat_ref={"nordpass_title": "PAT_ORGA"})
        c = m.get_credential("OrgA")
        self.assertEqual((c.source, c.secret.reveal()), ("nordpass", self.pat))

    def test_sem_pat(self):
        m = self.manager(auth="pat")
        with self.assertRaises(AuthError) as cm:
            m.get_credential("OrgA")
        self.assertEqual(cm.exception.reason_code, "PAT_NOT_FOUND")


class EnvNuncaEFonteTest(AuthBase):
    """I3, parte dinâmica."""

    def setUp(self):
        super().setUp()
        self.env_pat = fakelib.sentinel("Env")
        self._old = os.environ.get("AZURE_DEVOPS_EXT_PAT")
        os.environ["AZURE_DEVOPS_EXT_PAT"] = self.env_pat

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AZURE_DEVOPS_EXT_PAT", None)
        else:
            os.environ["AZURE_DEVOPS_EXT_PAT"] = self._old

    def test_az_nao_recebe_e_auth_nao_usa(self):
        m = self.manager()
        self.assertNotEqual(m.get_token("OrgA").reveal(), self.env_pat)
        self.assertFalse(self.az_calls()[0]["pat_in_env"])

    def test_modo_pat_ignora_variavel(self):
        m = self.manager(auth="pat")
        with self.assertRaises(AuthError):
            m.get_credential("OrgA")


class StaticEnvScanTest(unittest.TestCase):
    """I3, parte estática: nenhum módulo lê variável de ambiente com nome de segredo."""

    SECRET_NAME = re.compile(r"PAT|TOKEN|SECRET|PASSW|SENHA|KEY|CRED", re.I)
    ALLOWED = {"APPDATA", "XDG_CONFIG_HOME", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"}
    READ = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|env\.get\s*\(\s*)["']([^"']+)["']""")

    def test_controle_negativo_do_regex(self):
        sample = ('os.environ.get("AZURE_DEVOPS_EXT_PAT")\nos.environ["MY_TOKEN"]\n'
                  "os.getenv('SECRET')\nenv.get(\"APPDATA\")")
        self.assertEqual(self.READ.findall(sample), ["AZURE_DEVOPS_EXT_PAT", "MY_TOKEN", "SECRET", "APPDATA"])

    def test_sem_leitura_de_segredo(self):
        files = sorted(fakelib.SERVER.rglob("*.py"))
        self.assertTrue(files)
        found = []
        for f in files:
            text = f.read_text(encoding="utf-8")
            for name in self.READ.findall(text):
                if name not in self.ALLOWED or self.SECRET_NAME.search(name):
                    found.append(f"{f.name}: {name}")
            # leitura com nome vindo de variável também é proibida (os.environ[x], getenv(x))
            for m in re.finditer(r"os\.(?:environ\s*\[|environ\.get\s*\(|getenv\s*\()\s*([A-Za-z_]\w*)\s*[\],)]",
                                 text):
                found.append(f"{f.name}: dinâmico {m.group(1)}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()

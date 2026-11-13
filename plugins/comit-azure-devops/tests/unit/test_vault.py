"""V4: ordem NordPass -> cofre do SO com np, security e secret-tool falsos num HOME temporário."""
import contextlib
import io
import json
import os
import pickle
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402
from adoplug import vault  # noqa: E402
from adoplug.vault import Secret, VaultError, VaultTools  # noqa: E402

ORG = "OrgTeste"
SVC = "comit-azure-devops"


class SecretTest(unittest.TestCase):
    def test_nao_mostra_valor(self):
        v = fakelib.sentinel()
        s = Secret(v)
        self.assertNotIn(v, str(s))
        self.assertNotIn(v, repr(s))
        self.assertNotIn(v, repr([s, {"k": s}]))
        self.assertEqual(s.reveal(), v)
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        with self.assertRaises(ValueError):
            Secret("")

    def test_redact(self):
        v = fakelib.sentinel()
        jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
        text = " | ".join(fakelib.leak_forms(v) + [jwt, "Authorization: Basic QUJDREVGR0hJSktM"])
        out = vault.redact(text, [Secret(v)])
        for f in fakelib.leak_forms(v) + [jwt, "QUJDREVGR0hJSktM"]:
            self.assertNotIn(f, out)
        self.assertLessEqual(len(vault.redact("x" * 5000)), 1001)


class VaultBase(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.pat = fakelib.sentinel("Pat")
        self.os_pat = fakelib.sentinel("OsPat")

    def tools(self, np_state=None, keychain=None, platform="darwin", secret_tool=None):
        np_cmd, self.np_state = (self.h.np(**np_state) if np_state is not None else ([], None))
        sec_cmd, self.kc = self.h.security(keychain)
        st_cmd, self.st = self.h.secret_tool(secret_tool)
        return VaultTools(np_cmd=np_cmd, security_cmd=sec_cmd, secret_tool_cmd=st_cmd, platform=platform)

    def np_item(self, title="ADO_PAT_ORG", pw=None):
        return {"id": str(uuid.uuid4()), "title": title, "password": pw or self.pat}

    def read(self, tools, title="ADO_PAT_ORG", **ref):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                return vault.read_pat(ORG, {"nordpass_title": title, **ref}, tools), out.getvalue() + err.getvalue()
            except VaultError as e:
                self.captured = out.getvalue() + err.getvalue()
                raise e


class OrdemTest(VaultBase):
    def test_nordpass_primeiro(self):
        t = self.tools({"items": [self.np_item()]}, {f"{SVC}\t{ORG}": self.os_pat})
        r, printed = self.read(t)
        self.assertEqual((r.secret.reveal(), r.source), (self.pat, "nordpass"))
        self.assertEqual(self.h.log(self.kc), [])  # o Keychain nem foi consultado
        fakelib.assert_no_leak(self, printed + repr(r), [self.pat])

    def test_np_usa_json_sem_field_e_stdin_fechado(self):
        t = self.tools({"items": [self.np_item()]})
        self.read(t)
        log = self.h.log(self.np_state)
        self.assertEqual([e["argv"][:2] for e in log], [["item", "list"], ["item", "get"]])
        for e in log:
            self.assertNotIn("--field", e["argv"])
            self.assertNotIn("--copy", e["argv"])
            self.assertIn("--format", e["argv"])
            self.assertTrue(e["stdin_eof"])

    def test_pasta_do_nordpass(self):
        t = self.tools({"items": [self.np_item()]})
        self.read(t, nordpass_folder="minha-pasta")
        argv = self.h.log(self.np_state)[0]["argv"]
        self.assertEqual(argv[argv.index("--folder") + 1], "minha-pasta")

    def test_titulo_exato(self):
        t = self.tools({"items": [self.np_item("ADO_PAT_ORG_ANTIGO", "x-" + self.pat)]},
                       {f"{SVC}\t{ORG}": self.os_pat})
        r, _ = self.read(t)
        self.assertEqual((r.secret.reveal(), r.source), (self.os_pat, "keychain"))
        self.assertIn("nordpass:NP_NO_ITEM", r.notes)

    def test_titulo_ambiguo_nao_escolhe(self):
        t = self.tools({"items": [self.np_item(), self.np_item()]})
        with self.assertRaises(VaultError) as cm:
            self.read(t)
        self.assertEqual(cm.exception.reason_code, "PAT_NOT_FOUND")
        self.assertIn("nordpass:NP_AMBIGUOUS", cm.exception.tried)

    def test_travado_cai_no_keychain(self):
        t = self.tools({"mode": "locked", "items": [self.np_item()]}, {f"{SVC}\t{ORG}": self.os_pat})
        r, _ = self.read(t)
        self.assertEqual((r.secret.reveal(), r.source), (self.os_pat, "keychain"))
        self.assertIn("nordpass:NP_LOCKED", r.notes)

    def test_travado_sem_keychain_pede_unlock(self):
        t = self.tools({"mode": "locked", "items": [self.np_item()]})
        with self.assertRaises(VaultError) as cm:
            self.read(t)
        self.assertEqual(cm.exception.reason_code, "NP_LOCKED")
        self.assertIn("np unlock", str(cm.exception))
        fakelib.assert_no_leak(self, self.captured + str(cm.exception), [self.pat])

    def test_sessao_vencida(self):
        t = self.tools({"mode": "signin", "items": [self.np_item()]})
        with self.assertRaises(VaultError) as cm:
            self.read(t)
        self.assertEqual(cm.exception.reason_code, "NP_SIGNIN")
        self.assertIn("np signin", str(cm.exception))

    def test_sem_np_usa_keychain(self):
        t = self.tools(None, {f"{SVC}\t{ORG}": self.os_pat})
        r, _ = self.read(t)
        self.assertEqual(r.source, "keychain")
        self.assertIn("nordpass:absent", r.notes)

    def test_sem_titulo_nao_chama_np(self):
        t = self.tools({"items": [self.np_item()]}, {f"{SVC}\t{ORG}": self.os_pat})
        r = vault.read_pat(ORG, {}, t)
        self.assertEqual(r.source, "keychain")
        self.assertEqual(self.h.log(self.np_state), [])

    def test_referencia_propria_no_keychain(self):
        t = self.tools(None, {"MEU\tITEM": self.os_pat})
        r = vault.read_pat(ORG, {"os_service": "MEU", "os_account": "ITEM"}, t)
        self.assertEqual(r.secret.reveal(), self.os_pat)

    def test_nada_encontrado(self):
        t = self.tools(None)
        with self.assertRaises(VaultError) as cm:
            vault.read_pat(ORG, {}, t)
        self.assertEqual(cm.exception.reason_code, "PAT_NOT_FOUND")

    def test_variavel_de_ambiente_nunca_e_fonte(self):
        """I3: com AZURE_DEVOPS_EXT_PAT no ambiente e nada nos cofres, não há PAT."""
        t = self.tools(None)
        old = os.environ.get("AZURE_DEVOPS_EXT_PAT")
        os.environ["AZURE_DEVOPS_EXT_PAT"] = self.pat
        try:
            with self.assertRaises(VaultError):
                vault.read_pat(ORG, {}, t)
        finally:
            if old is None:
                os.environ.pop("AZURE_DEVOPS_EXT_PAT", None)
            else:
                os.environ["AZURE_DEVOPS_EXT_PAT"] = old

    def test_linux_secret_tool(self):
        t = self.tools(None, platform="linux", secret_tool={f"{SVC}\t{ORG}": self.os_pat})
        r = vault.read_pat(ORG, {}, t)
        self.assertEqual((r.secret.reveal(), r.source), (self.os_pat, "secret-tool"))


class GravacaoTest(VaultBase):
    def test_keychain_pelo_stdin(self):
        t = self.tools(None)
        value = self.pat + ' com "aspas" e \\barra'
        self.assertEqual(vault.write_os(SVC, ORG, value, t), "keychain")
        self.assertEqual(vault.read_os(SVC, ORG, t).reveal(), value)
        log = json.dumps(self.h.log(self.kc))
        self.assertNotIn(self.pat, log)  # o valor nunca passou pelo argv
        self.assertIn('"-i"', log)
        self.assertTrue(vault.delete_os(SVC, ORG, t))
        self.assertIsNone(vault.read_os(SVC, ORG, t))

    def test_keychain_recusa_controle(self):
        t = self.tools(None)
        for bad in ("a\nb", "a\rb", "a\x00b", ""):
            with self.subTest(bad=repr(bad)), self.assertRaises(VaultError):
                vault.write_os(SVC, ORG, bad, t)
        self.assertEqual(self.h.log(self.kc), [])

    def test_secret_tool_pelo_stdin(self):
        t = self.tools(None, platform="linux")
        self.assertEqual(vault.write_os(SVC, ORG, self.pat, t), "secret-tool")
        self.assertEqual(vault.read_os(SVC, ORG, t).reveal(), self.pat)
        self.assertNotIn(self.pat, json.dumps(self.h.log(self.st)))
        self.assertTrue(vault.delete_os(SVC, ORG, t))

    def test_nada_gravado_no_home(self):
        t = self.tools({"items": [self.np_item()]})
        vault.read_pat(ORG, {"nordpass_title": "ADO_PAT_ORG"}, t)
        fakelib.assert_no_leak(self, self.h.files_text(), [self.pat])


@unittest.skipUnless(sys.platform == "win32", "Credential Manager só no Windows")
class CredManRealTest(unittest.TestCase):
    """Windows real: alvo de teste criado e apagado."""

    def test_ida_e_volta(self):
        svc = "comit-azure-devops-test"
        acc = uuid.uuid4().hex
        v = fakelib.sentinel()
        t = VaultTools(platform="win32")
        try:
            self.assertEqual(vault.write_os(svc, acc, v, t), "credman")
            self.assertEqual(vault.read_os(svc, acc, t).reveal(), v)
        finally:
            vault.delete_os(svc, acc, t)
        self.assertIsNone(vault.read_os(svc, acc, t))


class FindNpTest(unittest.TestCase):
    def test_ordem(self):
        seen = []
        vault.find_np(which=lambda n: None, home="/h", platform="darwin", exists=lambda p: seen.append(p))
        self.assertEqual(seen, ["/opt/homebrew/bin/np", "/usr/local/bin/np", str(Path("/h/.local/bin/np"))])

    def test_windows(self):
        want = str(Path("C:/LA") / "Programs" / "NordPass CLI" / "np.exe")
        got = vault.find_np(which=lambda n: None, platform="win32", env={"LOCALAPPDATA": "C:/LA"},
                            exists=lambda p: p == want)
        # no macOS/Linux, "C:/LA/..." não é absoluto; só confere que nada fora da lista entra
        self.assertIn(got, (None, want))


if __name__ == "__main__":
    unittest.main()

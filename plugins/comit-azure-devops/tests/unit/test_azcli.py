import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402
from adoplug import azcli  # noqa: E402
from adoplug.azcli import AzCli, AzError  # noqa: E402


class FindAzTest(unittest.TestCase):
    """I4: ordem de busca e caminho absoluto."""

    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)

    def _touch(self, *parts):
        p = self.h.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return str(p)

    def test_config_primeiro(self):
        cfg = self._touch("cfg", "az")
        onpath = self._touch("bin", "az")
        got = azcli.find_az(cfg, which=lambda n: onpath, home=self.h.home, platform="linux")
        self.assertEqual(got, cfg)

    def test_path_depois(self):
        onpath = self._touch("bin", "az")
        got = azcli.find_az(None, which=lambda n: onpath, home=self.h.home, platform="linux")
        self.assertEqual(got, onpath)

    def test_config_relativo_ou_ausente_ignorado(self):
        onpath = self._touch("bin", "az")
        self.assertEqual(azcli.find_az("az", which=lambda n: onpath, home=self.h.home, platform="linux"), onpath)
        self.assertEqual(azcli.find_az(str(self.h.root / "nao-existe"), which=lambda n: onpath,
                                       home=self.h.home, platform="linux"), onpath)

    def test_caminhos_conhecidos_posix(self):
        seen = []

        def exists(p):
            seen.append(p)
            return False

        self.assertIsNone(azcli.find_az(None, which=lambda n: None, home=self.h.home, platform="darwin",
                                        exists=exists))
        self.assertEqual(seen, ["/opt/homebrew/bin/az", "/usr/local/bin/az",
                                str(self.h.home / ".local" / "bin" / "az"), "/usr/bin/az"])

    def test_uv_tool_no_home(self):
        p = self.h.home / ".local" / "bin" / "az"
        p.parent.mkdir(parents=True)
        p.write_text("")
        got = azcli.find_az(None, which=lambda n: None, home=self.h.home, platform="linux",
                            exists=lambda c: c == str(p))
        self.assertEqual(got, str(p))

    def test_windows_program_files(self):
        pf = str(self.h.root / "PF")
        want = str(Path(pf) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd")
        got = azcli.find_az(None, which=lambda n: None, home=self.h.home, platform="win32",
                            env={"ProgramFiles": pf}, exists=lambda c: c == want)
        self.assertEqual(got, os.path.abspath(want))

    def test_cmd_precisa_ser_absoluto(self):
        with self.assertRaises(AzError):
            AzCli(["az"])


class ChildEnvTest(unittest.TestCase):
    def test_remove_pat(self):
        env = azcli.child_env({"AZURE_DEVOPS_EXT_PAT": "x", "azure_devops_ext_pat": "y", "PATH": "/bin",
                               "SYSTEM_ACCESSTOKEN": "z"})
        self.assertEqual(env, {"PATH": "/bin"})


class RecordingRunner:
    def __init__(self, stdout="", stderr="", rc=0, exc=None):
        self.calls = []
        self.out = (stdout, stderr, rc)
        self.exc = exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.out[2], self.out[0], self.out[1])


class GetTokenTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.tok = fakelib.sentinel()
        self.tenant = str(uuid.uuid4())

    def test_token_e_expiracao(self):
        exp = int(time.time()) + 1234
        cmd, st = self.h.az(token=self.tok, expires_on=exp, tenant=self.tenant)
        t = AzCli(cmd).get_token(self.tenant)
        self.assertEqual(t.secret.reveal(), self.tok)
        self.assertEqual(t.expires_on, float(exp))
        self.assertNotIn(self.tok, repr(t))
        argv = self.h.log(st)[0]["argv"]
        self.assertEqual(argv[:4], ["account", "get-access-token", "--resource", azcli.ADO_RESOURCE])
        self.assertEqual(argv[argv.index("--tenant") + 1], self.tenant)

    def test_formato_antigo_expireson(self):
        cmd, _ = self.h.az(token=self.tok, old_format=True, expiresOn="2030-01-02 03:04:05.000000")
        t = AzCli(cmd).get_token()
        self.assertGreater(t.expires_on, time.time())

    def test_sem_shell_e_stdin_fechado(self):
        r = RecordingRunner(stdout='{"accessToken": "%s", "expires_on": 1}' % self.tok)
        AzCli(["/abs/az"], runner=r).get_token()
        argv, kw = r.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "/abs/az")
        self.assertIs(kw["shell"], False)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertNotIn("check", kw)
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", kw["env"])

    def test_filho_sem_pat_no_ambiente(self):
        """I3: o az falso registra se recebeu AZURE_DEVOPS_EXT_PAT."""
        cmd, st = self.h.az(token=self.tok)
        AzCli(cmd, env={**os.environ, "AZURE_DEVOPS_EXT_PAT": fakelib.sentinel("Env")}).get_token()
        self.assertFalse(self.h.log(st)[0]["pat_in_env"])

    def test_sem_login(self):
        cmd, _ = self.h.az(token=self.tok, mode="not_logged_in")
        with self.assertRaises(AzError) as cm:
            AzCli(cmd).get_token()
        self.assertEqual(cm.exception.reason_code, "AZ_NOT_LOGGED_IN")

    def test_erro_com_token_no_stderr_e_limpo(self):
        jwt = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20
        cmd, _ = self.h.az(token=jwt, mode="fail_echo")
        with self.assertRaises(AzError) as cm:
            AzCli(cmd).get_token()
        self.assertNotIn(jwt, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_tenant_invalido_nao_chama_o_az(self):
        r = RecordingRunner()
        for bad in ("x & calc", "--debug", "a b", "../x"):
            with self.subTest(bad=bad), self.assertRaises(AzError) as cm:
                AzCli(["/abs/az"], runner=r).get_token(bad)
            self.assertEqual(cm.exception.reason_code, "INVALID_TENANT")
        self.assertEqual(r.calls, [])

    def test_timeout(self):
        r = RecordingRunner(exc=subprocess.TimeoutExpired(["az"], 1, output=self.tok))
        with self.assertRaises(AzError) as cm:
            AzCli(["/abs/az"], runner=r).get_token()
        self.assertEqual(cm.exception.reason_code, "AZ_TIMEOUT")
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)  # o traceback não mostra a saída do az
        self.assertNotIn(self.tok, str(cm.exception))

    def test_saida_sem_token(self):
        r = RecordingRunner(stdout="{}")
        with self.assertRaises(AzError) as cm:
            AzCli(["/abs/az"], runner=r).get_token()
        self.assertEqual(cm.exception.reason_code, "AZ_FAILED")

    def test_versao_e_tenants(self):
        cmd, _ = self.h.az(token=self.tok, tenant=self.tenant)
        cli = AzCli(cmd)
        self.assertEqual(cli.version(), "9.9.9")
        self.assertEqual(cli.logged_tenants(), [self.tenant])


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.h = fakelib.TempHome()
        self.addCleanup(self.h.cleanup)
        self.tenant = str(uuid.uuid4())

    def test_device_code(self):
        cmd, st = self.h.az()
        h = AzCli(cmd).start_login(self.tenant, "device_code", wait_code=10)
        self.assertEqual(h.code, "FAKE12345")
        self.assertEqual(h.url, "https://microsoft.com/devicelogin")
        self.assertEqual(h.wait(10), 0)
        self.assertEqual(h.status()["state"], "ok")
        argv = self.h.log(st)[0]["argv"]
        self.assertIn("--use-device-code", argv)
        self.assertIn("--allow-no-subscriptions", argv)
        self.assertEqual(argv[argv.index("--tenant") + 1], self.tenant)

    def test_browser(self):
        cmd, _ = self.h.az()
        h = AzCli(cmd).start_login(self.tenant, "browser")
        self.assertEqual(h.wait(10), 0)
        self.assertIsNone(h.code)

    def test_modo_e_tenant_obrigatorios(self):
        cli = AzCli(["/abs/az"])
        with self.assertRaises(AzError):
            cli.start_login(self.tenant, "senha")
        with self.assertRaises(AzError):
            cli.start_login("", "browser")


if __name__ == "__main__":
    unittest.main()

"""I1, I2 e I6 com servidores HTTP locais (127.0.0.1); nenhum pedido sai da máquina."""
import contextlib
import io
import json
import logging
import socketserver
import sys
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
import fakelib  # noqa: E402
from adoplug import config  # noqa: E402
from adoplug import http as adohttp  # noqa: E402
from adoplug.auth import AuthManager, Credential  # noqa: E402
from adoplug.azcli import AzCli  # noqa: E402
from adoplug.vault import Secret  # noqa: E402


class LocalHTTPServer(ThreadingHTTPServer):
    """Sem `getfqdn` no bind: o DNS reverso de 127.0.0.1 trava ~30 s no runner macOS do CI."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Server:
    """Servidor local que grava cada pedido e responde conforme `routes`."""

    def __init__(self):
        self.requests = []
        self.routes = {}  # path -> (status, headers, body) ou lista para respostas em sequência
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers), "body": body})
                key = self.path.split("?")[0]
                r = outer.routes.get(key, (200, {}, b"{}"))
                if isinstance(r, list):
                    r = r.pop(0) if len(r) > 1 else r[0]
                status, hdrs, data = r
                if callable(data):
                    data = data(self)
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.httpd = LocalHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeAuth:
    def __init__(self, token):
        self.secret = Secret(token)
        self.invalidated = 0
        self.calls = 0

    def get_credential(self, org):
        self.calls += 1
        return Credential("az", "az", self.secret)

    def invalidate(self, org):
        self.invalidated += 1

    def known_secrets(self):
        return (self.secret,)


class HttpBase(unittest.TestCase):
    def setUp(self):
        self.srv = Server()
        self.addCleanup(self.srv.close)
        self.tok = fakelib.sentinel()
        self.auth = FakeAuth(self.tok)
        self.client = adohttp.AdoHttp(self.auth, ["OrgA"], scheme_host=self.srv.base)


class AllowlistTest(HttpBase):
    """I1."""

    def test_org_configurada_recebe_token(self):
        r = self.client.get("orga", "/_apis/projects", query={"api-version": "7.1"})
        self.assertTrue(r.ok)
        req = self.srv.requests[0]
        self.assertEqual(req["path"], "/orga/_apis/projects?api-version=7.1")
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + self.tok)
        hdrs = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(hdrs["x-tfs-fedauthredirect"], "Suppress")

    def test_org_nao_configurada_nao_envia_nada(self):
        with self.assertRaises(adohttp.HttpError) as cm:
            self.client.get("OutraOrg", "/_apis/projects")
        self.assertEqual(cm.exception.reason_code, "ORG_NOT_ALLOWED")
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.auth.calls, 0)

    def test_allowlist_dinamica(self):
        orgs = ["OrgA"]
        c = adohttp.AdoHttp(self.auth, lambda: orgs, scheme_host=self.srv.base)
        orgs.clear()
        with self.assertRaises(adohttp.HttpError):
            c.get("OrgA", "/_apis/projects")

    def test_redirect_recusado(self):
        other = Server()
        self.addCleanup(other.close)
        self.srv.routes["/OrgA/_apis/x"] = (302, {"Location": other.base + "/OrgA/_apis/y"}, b"")
        r = self.client.get("OrgA", "/_apis/x")
        self.assertEqual((r.status, r.reason_code, r.ok), (302, "REDIRECT_REFUSED", False))
        self.assertEqual(other.requests, [])  # o segundo host nunca recebeu o token

    def test_redirect_no_mesmo_host_tambem_recusado(self):
        self.srv.routes["/OrgA/_apis/x"] = (301, {"Location": self.srv.base + "/OrgA/_apis/y"}, b"")
        r = self.client.get("OrgA", "/_apis/x")
        self.assertEqual(r.reason_code, "REDIRECT_REFUSED")
        self.assertEqual(len(self.srv.requests), 1)

    def test_caminhos_proibidos(self):
        for p in ("_apis", "//evil/x", "/../x", "/%2e%2e/x", "/%252e%252e/x", "/a?b=1", "/a#b",
                  "/a\\b", "/a@b", "/a b", "/a\r\nX: y", "/x/https://evil"):
            with self.subTest(p=p), self.assertRaises(adohttp.HttpError):
                self.client.get("OrgA", p)
        self.assertEqual(self.srv.requests, [])

    def test_org_invalida(self):
        for o in ("evil.com", "a/b", "", "a@b"):
            with self.subTest(o=o), self.assertRaises(adohttp.HttpError):
                self.client.get(o, "/_apis/x")

    def test_host_fora_da_lista(self):
        with self.assertRaises(adohttp.HttpError):
            self.client.request("GET", "OrgA", "/_apis/x", host="evil.example")

    def test_cabecalho_authorization_do_chamador_recusado(self):
        for k in ("Authorization", "cookie", "Host"):
            with self.subTest(k=k), self.assertRaises(adohttp.HttpError):
                self.client.get("OrgA", "/_apis/x", headers={k: "x"})


class CheckUrlTest(unittest.TestCase):
    def setUp(self):
        self.c = adohttp.AdoHttp(FakeAuth(fakelib.sentinel()), ["OrgA"])

    def test_aceita(self):
        pid = str(uuid.uuid4())
        self.assertEqual(self.c.check_url(f"https://dev.azure.com/OrgA/{pid}/_apis/wit/x?y=1"), "OrgA")

    def test_recusa(self):
        for u in ("https://evil.example/OrgA/_apis", "https://orga.visualstudio.com/_apis",
                  "https://dev.azure.com:8443/OrgA/_apis", "https://u@dev.azure.com/OrgA/_apis",
                  "http://dev.azure.com/OrgA/_apis", "https://dev.azure.com.evil.example/OrgA",
                  "https://vssps.dev.azure.com/OrgA/_apis", "https://dev.azure.com/",
                  "https://dev.azure.com/OrgA/../x"):
            with self.subTest(u=u), self.assertRaises(adohttp.HttpError):
                self.c.check_url(u)

    def test_vssps_so_explicito(self):
        self.assertEqual(self.c.check_url("https://vssps.dev.azure.com/OrgA/_apis/x", allow_vssps=True), "OrgA")

    def test_url_padrao_e_dev_azure_com(self):
        self.assertEqual(self.c.url("OrgA", "/_apis/x"), "https://dev.azure.com/OrgA/_apis/x")
        self.assertEqual(self.c.url("OrgA", "/_apis/x", host=adohttp.VSSPS_HOST),
                         "https://vssps.dev.azure.com/OrgA/_apis/x")

    def test_segmento_com_acento(self):
        self.assertEqual(adohttp.segment("Excelência Comercial"), "Excel%C3%AAncia%20Comercial")


class BodyTest(HttpBase):
    """I6."""

    def test_json_patch_utf8(self):
        patch = [{"op": "add", "path": "/fields/System.Title", "value": "Ação de teste"}]
        self.client.request("POST", "OrgA", "/_apis/wit/workitems/$Task", query={"validateOnly": "true"},
                            json_body=patch, content_type=adohttp.JSON_PATCH)
        req = self.srv.requests[0]
        self.assertIn("Ação".encode("utf-8"), req["body"])
        self.assertEqual(json.loads(req["body"].decode("utf-8")), patch)
        self.assertTrue(req["headers"]["Content-Type"].startswith(adohttp.JSON_PATCH))
        self.assertIn("validateOnly=true", req["path"])

    def test_json_patch_precisa_ser_array(self):
        with self.assertRaises(adohttp.HttpError):
            self.client.request("PATCH", "OrgA", "/_apis/wit/workitems/1",
                                json_body={"op": "add"}, content_type=adohttp.JSON_PATCH)
        self.assertEqual(self.srv.requests, [])

    def test_bytes(self):
        self.client.request("POST", "OrgA", "/_apis/wit/attachments", data=b"\x00\x01")
        self.assertEqual(self.srv.requests[0]["body"], b"\x00\x01")
        self.assertEqual(self.srv.requests[0]["headers"]["Content-Type"], "application/octet-stream")


class AuthRejectTest(HttpBase):
    def test_401_invalida_e_repete_uma_vez(self):
        self.srv.routes["/OrgA/_apis/x"] = (401, {}, b'{"message": "no"}')
        r = self.client.get("OrgA", "/_apis/x")
        self.assertEqual((r.status, r.reason_code), (401, "AUTH_REJECTED"))
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.auth.invalidated, 1)

    def test_401_depois_200(self):
        self.srv.routes["/OrgA/_apis/x"] = [(401, {}, b""), (200, {}, b'{"ok": 1}')]
        r = self.client.get("OrgA", "/_apis/x")
        self.assertEqual(r.json(), {"ok": 1})

    def test_203_e_falha_de_autenticacao(self):
        self.srv.routes["/OrgA/_apis/x"] = (203, {"Content-Type": "text/html"}, b"<html>sign in</html>")
        r = self.client.get("OrgA", "/_apis/x")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "AUTH_REJECTED")
        self.assertEqual(r.diagnostic()["message"], "")

    def test_anonimo_sem_token_e_tenant(self):
        tenant = str(uuid.uuid4())
        self.srv.routes["/Qualquer/_apis/connectionData"] = (401, {"X-VSS-ResourceTenant": tenant}, b"")
        self.assertEqual(self.client.resource_tenant("Qualquer"), tenant)
        self.assertNotIn("Authorization", self.srv.requests[0]["headers"])
        self.assertEqual(self.auth.calls, 0)

    def test_set_cookie_descartado(self):
        self.srv.routes["/OrgA/_apis/x"] = (200, {"Set-Cookie": "a=b"}, b"{}")
        self.assertNotIn("set-cookie", self.client.get("OrgA", "/_apis/x").headers)


class DiagnosticTest(HttpBase):
    def test_diagnostico_campos(self):
        body = json.dumps({"typeKey": "RuleValidationException", "errorCode": 0,
                           "message": "TF401320: campo X" + "y" * 3000}).encode()
        self.srv.routes["/OrgA/_apis/x"] = (400, {}, body)
        d = self.client.get("OrgA", "/_apis/x").diagnostic()
        self.assertEqual((d["http_status"], d["typeKey"], d["reason_code"]),
                         (400, "RuleValidationException", "BAD_REQUEST"))
        self.assertLessEqual(len(d["message"]), 1001)

    def test_rede_fora(self):
        c = adohttp.AdoHttp(self.auth, ["OrgA"], scheme_host="http://127.0.0.1:9", timeout=2)
        with self.assertRaises(adohttp.HttpError) as cm:
            c.get("OrgA", "/_apis/x")
        self.assertIn(cm.exception.reason_code, ("NETWORK", "TIMEOUT"))


class LeakSweepTest(HttpBase):
    """I2: token injetado não aparece em nenhuma saída, retorno, erro, log ou arquivo."""

    def test_varredura(self):
        h = fakelib.TempHome()
        self.addCleanup(h.cleanup)
        tenant = str(uuid.uuid4())
        tok = fakelib.sentinel("Sweep")
        cfg = config.set_org(config.default_config(), "OrgA", tenant=tenant)
        cfg_path = h.home / "cfg" / "config.json"
        config.save_config(cfg, cfg_path)
        az_cmd, az_state = h.az(token=tok, tenant=tenant)
        auth = AuthManager(cfg, az_factory=lambda: AzCli(az_cmd))
        client = adohttp.AdoHttp(auth, lambda: config.configured_orgs(cfg), scheme_host=self.srv.base)

        echo = lambda req: json.dumps({"message": "eco " + req.headers.get("Authorization", "")}).encode()
        self.srv.routes["/OrgA/_apis/eco"] = (400, {}, echo)
        self.srv.routes["/OrgA/_apis/ok"] = (200, {}, b'{"count": 1}')
        self.srv.routes["/OrgA/_apis/redir"] = (302, {"Location": "http://127.0.0.1:9/x?t=" + tok}, b"")

        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old_level)))

        collected = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for path in ("/_apis/ok", "/_apis/eco", "/_apis/redir"):
                r = client.get("OrgA", path)
                collected += [repr(r), r.text(), json.dumps(r.diagnostic()), json.dumps(r.headers), r.url]
            collected.append(repr(auth.get_credential("OrgA")))
            for bad in (lambda: client.get("Outra", "/_apis/ok"), lambda: client.get("OrgA", "/../x")):
                try:
                    bad()
                except adohttp.HttpError as e:
                    collected += [str(e), repr(e)]
            cfail = adohttp.AdoHttp(auth, ["OrgA"], scheme_host="http://127.0.0.1:9", timeout=2)
            try:
                cfail.get("OrgA", "/_apis/ok")
            except adohttp.HttpError as e:
                collected += [str(e), repr(e)]
        # o eco chegou com o token (prova de que o teste exercita o caminho)
        self.assertIn(tok, self.srv.requests[1]["headers"]["Authorization"])
        haystack = "\n".join(collected) + out.getvalue() + err.getvalue() + logs.getvalue() + h.files_text()
        fakelib.assert_no_leak(self, haystack, [tok])
        self.assertNotIn(tok, cfg_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

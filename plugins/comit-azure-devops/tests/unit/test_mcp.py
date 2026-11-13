"""Protocolo MCP: initialize, tools/list, tools/call, erros, e o servidor real em subprocesso (--mock)."""
import io
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes", "tests/unit"):
    sys.path.insert(0, str(PLUGIN / sub))
import ado_mcp  # noqa: E402
import test_tools  # noqa: E402
from adoplug import tools  # noqa: E402

SERVER = PLUGIN / "server" / "ado_mcp.py"
INSTRUCTIONS = (
    "comit-azure-devops: operações de work items do Azure DevOps.\n"
    "Antes de qualquer operação, chame ado_status.\n"
    "Toda escrita passa por ado_prepare_* e mostra a prévia à pessoa.\n"
    "Chame ado_apply só depois do OK explícito da pessoa para aquela prévia.\n"
    "Nunca peça PAT, senha ou token no chat."
)


def req(mid, method, params=None):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call(mid, name, **args):
    return req(mid, "tools/call", {"name": name, "arguments": args})


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.e = test_tools.Env(self)
        self.srv = ado_mcp.Server(self.e.ctx, log=lambda m: None)

    def tearDown(self):
        self.e.assert_no_leak()

    def test_initialize(self):
        r = self.srv.handle(req(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "0"}}))
        self.assertEqual(r["id"], 1)
        res = r["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["instructions"], INSTRUCTIONS)
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "comit-azure-devops")
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(res["serverInfo"]["version"], manifest["version"])

    def test_versao_de_protocolo(self):
        for want, got in (("2024-11-05", "2024-11-05"), ("1999-01-01", "2025-06-18"), (None, "2025-06-18")):
            p = {} if want is None else {"protocolVersion": want}
            self.assertEqual(self.srv.handle(req(1, "initialize", p))["result"]["protocolVersion"], got)

    def test_instrucoes_do_codigo(self):
        self.assertEqual(ado_mcp.INSTRUCTIONS, INSTRUCTIONS)

    def test_notificacao_sem_resposta(self):
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "qualquer/coisa"}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "id": 5, "result": {}}))  # resposta de cliente

    def test_ping(self):
        self.assertEqual(self.srv.handle(req("a", "ping")), {"jsonrpc": "2.0", "id": "a", "result": {}})

    def test_tools_list(self):
        r = self.srv.handle(req(2, "tools/list"))["result"]["tools"]
        self.assertEqual({t["name"] for t in r}, test_tools.EXPECTED)
        self.assertNotIn("hello", {t["name"] for t in r})
        for t in r:
            self.assertEqual(t["inputSchema"]["type"], "object")
        json.dumps(r)

    def test_call_ok(self):
        r = self.srv.handle(call(3, "ado_whoami"))["result"]
        env = json.loads(r["content"][0]["text"])
        self.assertEqual(env, r["structuredContent"])
        self.assertEqual(env["status"], "ok")
        self.assertFalse(r["isError"])
        self.assertEqual(r["content"][0]["type"], "text")

    def test_call_com_erro_vai_no_result(self):
        r = self.srv.handle(call(4, "ado_get", id=987654))
        self.assertNotIn("error", r)
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["reason_code"], "NOT_FOUND")

    def test_call_needs_setup_nao_e_erro(self):
        r = self.srv.handle(call(4, "ado_whoami", org="Outra"))["result"]
        self.assertEqual(r["structuredContent"]["status"], "needs_setup")
        self.assertFalse(r["isError"])

    def test_argumentos_invalidos(self):
        r = self.srv.handle(call(5, "ado_get"))["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["reason_code"], "INVALID_ARGUMENTS")

    def test_ferramenta_desconhecida(self):
        r = self.srv.handle(call(6, "ado_nada"))
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(r["id"], 6)

    def test_params_invalidos(self):
        self.assertEqual(self.srv.handle(req(7, "tools/call", {"arguments": {}}))["error"]["code"], -32602)
        self.assertEqual(self.srv.handle(req(7, "tools/call", {"name": "ado_whoami", "arguments": []}))["error"]["code"], -32602)
        self.assertEqual(self.srv.handle(req(7, "tools/list", [1]))["error"]["code"], -32602)

    def test_metodo_desconhecido(self):
        r = self.srv.handle(req(8, "resources/list"))
        self.assertEqual((r["id"], r["error"]["code"]), (8, -32601))

    def test_requisicao_invalida(self):
        self.assertEqual(self.srv.handle([1])["error"]["code"], -32600)
        self.assertEqual(self.srv.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)  # sem jsonrpc
        self.assertEqual(self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": 5})["error"]["code"], -32600)

    def test_linha_invalida(self):
        out = json.loads(self.srv.handle_line(b"{ruim"))
        self.assertEqual((out["id"], out["error"]["code"]), (None, -32700))
        out = json.loads(self.srv.handle_line(b"\xff\xfe"))
        self.assertEqual(out["error"]["code"], -32700)
        self.assertIsNone(self.srv.handle_line(b"   \n"))

    def test_lote(self):
        raw = json.dumps([req(1, "ping"), {"jsonrpc": "2.0", "method": "notifications/initialized"}, req(2, "ping")])
        out = json.loads(self.srv.handle_line(raw.encode()))
        self.assertEqual([r["id"] for r in out], [1, 2])

    def test_acento_ida_e_volta(self):
        self.e.ado.add_item("Task", "Conferência de números — ação", **{"System.AssignedTo": {
            "displayName": "Eu Mesmo", "uniqueName": "eu@example.com", "id": test_tools.fake_ado.ME}})
        raw = json.dumps(call(9, "ado_query", project="Projeto Ágil", assigned_to_me=True), ensure_ascii=False).encode("utf-8")
        out = self.srv.handle_line(raw)
        self.assertIn("Conferência de números — ação".encode("utf-8"), out)
        self.assertNotIn(b"\\u", out)
        self.assertNotIn(b"\n", out)

    def test_erro_interno_do_servidor_nao_derruba(self):
        self.srv.ctx = None  # qualquer uso quebra
        out = json.loads(self.srv.handle_line(json.dumps(call(10, "ado_whoami")).encode()))
        self.assertEqual(out["error"]["code"], -32603)
        self.assertEqual(out["id"], 10)

    def test_serve_le_varias_linhas(self):
        inp = io.BytesIO(("\n".join(json.dumps(m) for m in (req(1, "ping"), {"jsonrpc": "2.0",
                          "method": "notifications/initialized"}, req(2, "tools/list"))) + "\n").encode())
        out = io.BytesIO()
        ado_mcp.serve(self.e.ctx, inp, out, log=lambda m: None)
        lines = out.getvalue().decode("utf-8").splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [1, 2])

    def test_render_nao_altera_titulo_com_palavra_de_autenticacao(self):
        # 'basic configuration' e 'Bearer authentication' não são credenciais: o apply_args tem de voltar intacto
        fields = {**test_tools.TASK, "System.Title": "Ajuste basic configuration e Bearer authentication"}
        prep = self.srv.handle(call(12, "ado_prepare_create", project="Projeto Ágil", type="Task", fields=fields))
        env = prep["result"]["structuredContent"]
        self.assertEqual(env["reason_code"], "PREVIEW_READY", env)
        self.assertEqual(env["data"]["apply_args"]["title"], "Ajuste basic configuration e Bearer authentication")
        done = self.srv.handle(call(13, "ado_apply", **env["data"]["apply_args"]))["result"]["structuredContent"]
        self.assertEqual(done["reason_code"], "APPLIED", done)

    def test_token_nunca_na_saida(self):
        out = self.srv.handle_line(json.dumps(call(11, "ado_status", check_remote=True)).encode())
        for form in test_tools.fakelib.leak_forms(self.e.tok):
            self.assertNotIn(form.encode(), out)


class SubprocessTest(unittest.TestCase):
    """O servidor de verdade, pelo stdio, no modo --mock: ADO falso, config temporária."""

    def start(self, *args):
        p = subprocess.Popen([sys.executable, str(SERVER), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        t = threading.Timer(90, p.kill)
        t.start()

        def stop():
            t.cancel()
            p.kill()
            p.wait()
            for s in (p.stdin, p.stdout, p.stderr):
                s.close()
        self.addCleanup(stop)
        self.lines = []
        return p

    def rpc(self, p, msg):
        p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()
        if "id" not in msg:
            return None
        line = p.stdout.readline()
        self.assertTrue(line, "o servidor fechou o stdout")
        self.lines.append(line)
        return json.loads(line.decode("utf-8"))

    def test_fluxo_completo_no_mock(self):
        p = self.start("--mock")
        init = self.rpc(p, req(1, "initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(init["result"]["instructions"], INSTRUCTIONS)
        self.rpc(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(len(self.rpc(p, req(2, "tools/list"))["result"]["tools"]), 22)
        st = self.rpc(p, call(3, "ado_status"))["result"]["structuredContent"]
        self.assertEqual((st["status"], st["data"]["mode"], st["data"]["mock"]["scenario"]), ("ok", "mock", "configured"))
        q = self.rpc(p, call(4, "ado_query", project="Projeto Ágil", assigned_to_me=True, top=3))["result"]
        items = q["structuredContent"]["data"]["items"]
        self.assertEqual(len(items), 3)
        self.assertIn("Revisar carga", {i["fields"]["System.Title"] for i in items})
        prep = self.rpc(p, call(5, "ado_prepare_create", project="Projeto Ágil", type="Task", parent_id=1001,
                                fields={"System.Title": "Conferência — acentuação", **{k: v for k, v in test_tools.TASK.items()
                                                                                  if k != "System.Title"}}))
        env = prep["result"]["structuredContent"]
        self.assertEqual(env["reason_code"], "PREVIEW_READY", env)
        done = self.rpc(p, call(6, "ado_apply", **env["data"]["apply_args"]))["result"]["structuredContent"]
        self.assertEqual(done["reason_code"], "APPLIED", done)
        again = self.rpc(p, call(7, "ado_get", id=done["data"]["id"]))["result"]["structuredContent"]
        self.assertEqual(again["data"]["item"]["title"], "Conferência — acentuação")
        for line in self.lines:  # o stdout só tem JSON-RPC, uma mensagem por linha
            self.assertEqual(json.loads(line.decode("utf-8"))["jsonrpc"], "2.0")

    def test_mock_sem_config(self):
        p = self.start("--mock", "unconfigured")
        self.rpc(p, req(1, "initialize", {}))
        st = self.rpc(p, call(2, "ado_status"))["result"]["structuredContent"]
        self.assertEqual(st["status"], "needs_setup")
        self.assertEqual([x["code"] for x in st["data"]["pending_setup"]], ["NO_ORG"])
        sc = self.rpc(p, call(3, "ado_setup_org", org="OrgA", confirm=True))["result"]["structuredContent"]
        self.assertEqual(sc["reason_code"], "ORG_SAVED", sc)
        who = self.rpc(p, call(4, "ado_whoami"))["result"]["structuredContent"]
        self.assertEqual(who["status"], "ok")
        self.assertEqual(self.rpc(p, call(5, "ado_status"))["result"]["structuredContent"]["data"]["mock"]["scenario"],
                         "unconfigured")

    def test_json_invalido_nao_derruba(self):
        p = self.start("--mock")
        p.stdin.write(b"isto nao e json\n")
        p.stdin.flush()
        self.assertEqual(json.loads(p.stdout.readline())["error"]["code"], -32700)
        self.assertEqual(self.rpc(p, req(1, "ping"))["result"], {})

    def test_stdout_fechado_encerra(self):
        p = self.start("--mock")
        p.stdin.close()
        self.assertEqual(p.wait(timeout=30), 0)

    def test_modo_so_pelo_argumento(self):
        # sem --mock o servidor sobe em modo real e lê a config local; aqui só o argumento inválido
        r = subprocess.run([sys.executable, str(SERVER), "--mock", "invalido"], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()

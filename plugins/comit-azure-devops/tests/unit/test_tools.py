"""Ferramentas MCP sobre o ADO falso local: contratos, escritas em duas fases, setup e vazamento."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes", "tests/unit"):
    sys.path.insert(0, str(PLUGIN / sub))
import fake_ado  # noqa: E402
import fakelib  # noqa: E402
import test_discovery  # noqa: E402
from adoplug import config as cfgmod  # noqa: E402
from adoplug import http as adohttp  # noqa: E402
from adoplug import profile_validate, tools  # noqa: E402
from adoplug.auth import AuthManager  # noqa: E402
from adoplug.azcli import AzCli, AzError  # noqa: E402
from adoplug.vault import Secret, VaultTools  # noqa: E402

PROJ = fake_ado.PROJ
TENANT = "0e0e0e0e-0000-4000-8000-00000000000e"
TASK = {"System.Title": "Revisar carga", "Custom.Equipe": "DADOS",
        "Microsoft.VSTS.Scheduling.OriginalEstimate": "2", "Microsoft.VSTS.Common.Priority": "3"}
EXPECTED = {
    "ado_status", "ado_setup_org", "ado_auth_login", "ado_whoami", "ado_list_projects", "ado_list_teams",
    "ado_iterations", "ado_get", "ado_query", "ado_children", "ado_comments", "ado_resolve_identity",
    "ado_type_schema", "ado_discover_org", "ado_profile_save", "ado_set_defaults",
    "ado_prepare_create", "ado_prepare_update", "ado_prepare_comment", "ado_prepare_attach", "ado_apply",
    "ado_reconcile",
}


class Env:
    """Contexto de teste: ADO falso, config temporária, `az` e cofre falsos."""

    def __init__(self, tc, org="OrgA", configured=True, az=False, np_items=None, discovery=False, profile=True,
                 az_state=None):
        self.tc = tc
        self.ado = fake_ado.FakeAdo(org)
        tc.addCleanup(self.ado.close)
        self.h = fakelib.TempHome()
        tc.addCleanup(self.h.cleanup)
        self.tmp = Path(tempfile.mkdtemp(prefix="adoplug-t-"))
        tc.addCleanup(shutil.rmtree, self.tmp, True)
        self.tok = fakelib.sentinel()
        self.org = org
        self.cdir = self.tmp / "cfg"
        (self.cdir / "profiles").mkdir(parents=True)
        self.files = self.tmp / "files"
        self.files.mkdir()
        (self.tmp / "home").mkdir()
        self.envs = []
        if configured:
            cfgmod.save_config(cfgmod.set_org(cfgmod.default_config(), org, tenant=TENANT),
                               cfgmod.paths.config_file(self.cdir))
        if profile:
            (self.cdir / "profiles" / f"{org}.json").write_text(
                json.dumps(fake_ado.profile(), ensure_ascii=False), encoding="utf-8")
        st = {"token": self.tok, "tenant": TENANT}
        st.update(az_state or {})
        self.az_cmd, self.az_state = self.h.az(**st)
        np_cmd, _ = self.h.np(items=np_items) if np_items is not None else ([], None)
        sec_cmd, self.keychain = self.h.security()
        self.vault = VaultTools(np_cmd=np_cmd, security_cmd=sec_cmd, secret_tool_cmd=[], platform="darwin")
        kw = {}
        if az:
            kw["auth_factory"] = lambda cfg: AuthManager(cfg, az_factory=lambda: AzCli(self.az_cmd),
                                                         vault_tools=self.vault)
        else:
            kw["auth_factory"] = lambda cfg: fake_ado.FakeAuth(self.tok)
        if discovery:
            kw["discovery_factory"] = lambda http: test_discovery.FakeClient()
        self.ctx = tools.Context(
            config_dir=self.cdir, embedded_dir=self.tmp / "emb", vault_tools=self.vault, home=self.tmp / "home",
            http_factory=lambda auth, cfg: adohttp.AdoHttp(auth, lambda: cfgmod.configured_orgs(cfg),
                                                           scheme_host=self.ado.base),
            tenant_lookup=lambda o: TENANT, scheme_host=self.ado.base, **kw)

    def call(self, name, **args):
        env = tools.call_tool(self.ctx, name, args)
        self.envs.append(env)
        return env

    def apply(self, env, **override):
        a = dict(env["data"]["apply_args"])
        a.update(override)
        return self.call("ado_apply", **a)

    def leak_text(self):
        text = json.dumps(self.envs, ensure_ascii=False, default=str)
        for p in self.cdir.rglob("*"):
            if p.is_file():
                text += p.read_text(encoding="utf-8", errors="replace")
        return text

    def assert_no_leak(self):
        text = self.leak_text()
        for form in fakelib.leak_forms(self.tok):
            self.tc.assertNotIn(form, text)


class Base(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.e = Env(self, **self.ENV)

    def tearDown(self):
        self.e.assert_no_leak()


class RegistryTest(unittest.TestCase):
    def test_nomes_exatos(self):
        self.assertEqual(set(tools.TOOLS), EXPECTED)
        self.assertEqual(len(tools.tool_list()), 22)

    def test_esquemas(self):
        for t in tools.tool_list():
            s = t["inputSchema"]
            self.assertEqual(s["type"], "object", t["name"])
            self.assertIs(s["additionalProperties"], False, t["name"])
            self.assertTrue(t["description"].strip(), t["name"])
            self.assertEqual(t["annotations"]["readOnlyHint"], tools.TOOLS[t["name"]].read_only)
            json.dumps(s)

    def test_apply_tem_as_chaves_de_apply_args(self):
        props = set(tools.TOOLS["ado_apply"].schema["properties"])
        self.assertEqual(props, {"preview_id", "hash", "operation", "org", "target_id", "type", "title", "file_name"})
        self.assertTrue(tools.tool_list()[[t["name"] for t in tools.tool_list()].index("ado_apply")]
                        ["annotations"]["destructiveHint"])

    def test_leituras_sao_somente_leitura(self):
        for n in ("ado_status", "ado_get", "ado_query", "ado_children", "ado_comments", "ado_whoami",
                  "ado_discover_org", "ado_type_schema"):
            self.assertTrue(tools.TOOLS[n].read_only, n)
        for n in ("ado_apply", "ado_prepare_create", "ado_set_defaults", "ado_profile_save"):
            self.assertFalse(tools.TOOLS[n].read_only, n)

    def test_validador(self):
        s = tools.TOOLS["ado_apply"].schema
        self.assertEqual(tools.validate_args(s, {"preview_id": "a", "hash": "b", "operation": "create", "org": "x"}), [])
        errs = tools.validate_args(s, {"preview_id": 1, "operation": "delete", "x": 1})
        text = " ".join(errs)
        for frag in ("preview_id: esperado string", "hash: obrigatório", "operation: use um destes", "x: argumento desconhecido"):
            self.assertIn(frag, text)
        h = tools.TOOLS["ado_prepare_create"].schema["properties"]["hyperlinks"]
        self.assertEqual(tools.validate_args(h, ["https://a.b", {"url": "https://c.d"}]), [])
        self.assertTrue(tools.validate_args(h, [{"comment": "sem url"}]))
        self.assertTrue(tools.validate_args(h, [3]))

    def test_id_aceita_inteiro_ou_texto(self):
        s = tools.TOOLS["ado_get"].schema
        self.assertEqual(tools.validate_args(s, {"id": 5}), [])
        self.assertEqual(tools.validate_args(s, {"id": "5"}), [])
        self.assertTrue(tools.validate_args(s, {"id": True}))


class DispatchTest(Base):
    def test_argumento_invalido(self):
        env = self.e.call("ado_get", id=1, extra=1)
        self.assertEqual((env["status"], env["reason_code"]), ("failed", "INVALID_ARGUMENTS"))
        self.assertEqual(self.e.ado.requests, [])

    def test_ferramenta_desconhecida(self):
        with self.assertRaises(tools.UnknownTool):
            tools.call_tool(self.e.ctx, "ado_nada", {})

    def test_envelope_completo(self):
        for env in (self.e.call("ado_whoami"), self.e.call("ado_get", id=999999)):
            self.assertEqual(set(env), {"status", "reason_code", "data", "next_action", "diagnostic"})

    def test_erro_do_ado_vira_envelope(self):
        env = self.e.call("ado_get", id=999999)
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["reason_code"], "NOT_FOUND")
        self.assertEqual(env["diagnostic"]["http_status"], 404)

    def test_org_nao_configurada(self):
        env = self.e.call("ado_whoami", org="Outra")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_setup", "ORG_NOT_CONFIGURED"))
        self.assertEqual([r for r in self.e.ado.requests if r["path"] != "/OrgA/_apis/connectionData"], [])

    def test_org_por_url_visualstudio_recusada(self):
        env = self.e.call("ado_whoami", org="https://x.visualstudio.com/p")
        self.assertEqual(env["reason_code"], "UNSUPPORTED_HOST")

    def test_org_sem_diferenciar_maiusculas(self):
        self.assertEqual(self.e.call("ado_whoami", org="orga")["status"], "ok")

    def test_erro_interno_generico(self):
        def boom(*a, **k):
            raise RuntimeError("falha com " + self.e.tok)
        self.e.ctx.api.whoami = boom
        env = self.e.call("ado_whoami")
        self.assertEqual((env["status"], env["reason_code"]), ("failed", "INTERNAL_ERROR"))
        text, _ = tools.render(self.e.ctx, env)
        self.assertNotIn(self.e.tok, text)

    def test_erro_interno_no_apply_manda_reconciliar(self):
        def boom(*a, **k):
            raise OSError("disco cheio")
        self.e.ctx.writes.apply = boom
        env = self.e.call("ado_apply", preview_id="a" * 32, hash="b" * 64, operation="create", org="OrgA")
        self.assertEqual(env["reason_code"], "INTERNAL_ERROR")
        self.assertIn("ado_reconcile", env["next_action"])
        self.assertIn("a" * 32, env["next_action"])

    def test_render_recusa_secret(self):
        env = tools.ok({"x": Secret("valor-secreto-123")})
        text, obj = tools.render(self.e.ctx, env)
        self.assertNotIn("valor-secreto-123", text)
        self.assertEqual(obj["reason_code"], "INTERNAL_ERROR")

    def test_render_limpa_token_ecoado(self):
        env = tools.ok({"msg": "Authorization: Bearer " + self.e.tok})
        text, _ = tools.render(self.e.ctx, env)
        self.assertNotIn(self.e.tok, text)

    def test_config_relida_a_cada_chamada(self):
        self.assertEqual(self.e.call("ado_whoami", org="OrgB")["reason_code"], "ORG_NOT_CONFIGURED")
        p = cfgmod.paths.config_file(self.e.cdir)
        cfgmod.save_config(cfgmod.set_org(cfgmod.load_config(p), "OrgB", tenant=TENANT), p)
        self.assertEqual(self.e.call("ado_status", org="OrgB")["data"]["orgs"][0]["org"], "OrgB")

    def test_config_ilegivel(self):
        def deny(path=None):
            raise PermissionError("sem acesso")
        orig = cfgmod.load_config
        cfgmod.load_config = deny
        self.addCleanup(setattr, cfgmod, "load_config", orig)
        env = self.e.call("ado_list_projects")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_setup", "CONFIG_INVALID"))
        self.assertIn("ler", env["next_action"])

    def test_config_invalida(self):
        cfgmod.paths.config_file(self.e.cdir).write_text("{ruim", encoding="utf-8")
        env = self.e.call("ado_list_projects")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_setup", "CONFIG_INVALID"))
        st = self.e.call("ado_status")
        self.assertEqual(st["status"], "needs_setup")
        self.assertIn("CONFIG_INVALID", [p["code"] for p in st["data"]["pending_setup"]])


class StatusTest(unittest.TestCase):
    def test_sem_org(self):
        e = Env(self, configured=False)
        env = e.call("ado_status")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_setup", "SETUP_PENDING"))
        self.assertEqual([p["code"] for p in env["data"]["pending_setup"]], ["NO_ORG"])
        self.assertEqual(e.ado.requests, [])

    def test_pronto_com_az(self):
        e = Env(self, az=True)
        env = e.call("ado_status")
        self.assertEqual((env["status"], env["reason_code"]), ("ok", "READY"), env)
        d = env["data"]
        self.assertEqual(d["az"]["version"], "9.9.9")
        self.assertEqual(d["orgs"][0]["login"], "ok")
        self.assertEqual(d["orgs"][0]["profile"], "learned")
        self.assertEqual(d["mode"], "real")
        self.assertEqual(e.ado.requests, [])  # sem rede por padrão
        e.assert_no_leak()

    def test_check_remote(self):
        e = Env(self, az=True)
        env = e.call("ado_status", check_remote=True)
        self.assertTrue(env["data"]["orgs"][0]["remote"]["ok"])
        self.assertEqual([r["path"] for r in e.ado.requests], ["/OrgA/_apis/connectionData"])
        e.assert_no_leak()

    def test_login_ausente(self):
        e = Env(self, az=True, az_state={"tenant": "0f0f0f0f-0000-4000-8000-00000000000f"})
        env = e.call("ado_status")
        self.assertEqual(env["data"]["orgs"][0]["login"], "missing")
        self.assertIn("LOGIN_MISSING", [p["code"] for p in env["data"]["pending_setup"]])

    def test_az_ausente(self):
        e = Env(self, az=True)

        def none():
            raise AzError("AZ_NOT_FOUND", "Azure CLI não encontrado.")
        e.ctx.auth._az_factory = none
        env = e.call("ado_status")
        self.assertIn("AZ_NOT_FOUND", [p["code"] for p in env["data"]["pending_setup"]])
        self.assertFalse(env["data"]["az"]["found"])

    def test_perfil_ausente(self):
        e = Env(self, az=True, profile=False)
        env = e.call("ado_status")
        self.assertIn("PROFILE_ABSENT", [p["code"] for p in env["data"]["pending_setup"]])

    def test_tenant_desconhecido(self):
        e = Env(self, az=True)
        p = cfgmod.paths.config_file(e.cdir)
        cfgmod.save_config(cfgmod.set_org(cfgmod.load_config(p), "OrgA", tenant=None), p)
        env = e.call("ado_status")
        self.assertIn("TENANT_UNKNOWN", [x["code"] for x in env["data"]["pending_setup"]])

    def test_modo_pat_sem_pat(self):
        e = Env(self)
        p = cfgmod.paths.config_file(e.cdir)
        cfgmod.save_config(cfgmod.set_org(cfgmod.load_config(p), "OrgA", auth="pat"), p)
        env = e.call("ado_status")
        row = env["data"]["orgs"][0]
        self.assertEqual(row["pat"], {"found": False, "reason_code": "PAT_NOT_FOUND"})
        self.assertIn("PAT_NOT_FOUND", [x["code"] for x in env["data"]["pending_setup"]])

    def test_modo_pat_com_pat_no_cofre(self):
        e = Env(self)
        pat = fakelib.sentinel("Pat")
        p = cfgmod.paths.config_file(e.cdir)
        cfgmod.save_config(cfgmod.set_org(cfgmod.load_config(p), "OrgA", auth="pat"), p)
        from adoplug import vault
        vault.write_os("comit-azure-devops", "OrgA", pat, e.vault)
        env = e.call("ado_status")
        self.assertEqual(env["data"]["orgs"][0]["pat"], {"found": True, "source": "keychain"})
        self.assertNotIn(pat, json.dumps(env))

    def test_filtro_por_org(self):
        e = Env(self, az=True)
        self.assertEqual(e.call("ado_status", org="Zzz")["data"]["pending_setup"][0]["code"], "ORG_NOT_CONFIGURED")


class SetupTest(unittest.TestCase):
    def test_sem_confirm_nao_grava(self):
        e = Env(self, configured=False)
        env = e.call("ado_setup_org", org="https://dev.azure.com/OrgA/Projeto%20%C3%81gil/_workitems/edit/42")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "CONFIRM_REQUIRED"))
        d = env["data"]
        self.assertEqual((d["org"], d["tenant"], d["project_hint"], d["item_id"]), ("OrgA", TENANT, "Projeto Ágil", 42))
        self.assertFalse(cfgmod.paths.config_file(e.cdir).exists())

    def test_confirm_grava_e_libera_a_org(self):
        e = Env(self, configured=False)
        self.assertEqual(e.call("ado_whoami", org="OrgA")["status"], "needs_setup")
        env = e.call("ado_setup_org", org="OrgA", confirm=True)
        self.assertEqual((env["status"], env["reason_code"]), ("ok", "ORG_SAVED"))
        saved = cfgmod.load_config(cfgmod.paths.config_file(e.cdir))
        self.assertEqual(saved["default_org"], "OrgA")
        self.assertEqual(saved["orgs"]["OrgA"]["tenant"], TENANT)
        # a allowlist do http enxerga a org nova sem reiniciar
        self.assertEqual(e.call("ado_whoami")["status"], "ok")

    def test_pat_ref_com_nordpass(self):
        e = Env(self, configured=False)
        e.call("ado_setup_org", org="OrgA", auth="pat", confirm=True,
               pat_ref={"nordpass_title": "ADO_PAT_ORGA", "nordpass_folder": "Trabalho"})
        entry = cfgmod.load_config(cfgmod.paths.config_file(e.cdir))["orgs"]["OrgA"]
        self.assertEqual(entry["auth"], "pat")
        self.assertEqual(entry["pat_ref"]["nordpass_title"], "ADO_PAT_ORGA")
        self.assertEqual(entry["pat_ref"]["nordpass_folder"], "Trabalho")
        self.assertEqual(entry["pat_ref"]["os_service"], "comit-azure-devops")

    def test_pat_ref_so_aceita_servico_fixo_e_conta_da_org(self):
        e = Env(self, configured=False)
        for ref in ({"os_service": "outro-app"}, {"os_account": "OutraOrg"},
                    {"os_service": "comit-azure-devops", "os_account": "x"}):
            for confirm in (False, True):
                env = e.call("ado_setup_org", org="OrgA", auth="pat", confirm=confirm, pat_ref=ref)
                self.assertEqual((env["status"], env["reason_code"]), ("needs_setup", "CONFIG_INVALID"), ref)
        self.assertFalse(cfgmod.paths.config_file(e.cdir).exists())
        env = e.call("ado_setup_org", org="OrgA", auth="pat", confirm=True,
                     pat_ref={"os_service": "comit-azure-devops", "os_account": "orga"})
        self.assertEqual(env["status"], "ok")

    def test_segredo_na_config_e_recusado(self):
        e = Env(self, configured=False)
        env = e.call("ado_setup_org", org="OrgA", confirm=True, pat_ref={"token": "x"})
        self.assertEqual(env["status"], "failed")  # esquema: chave desconhecida
        self.assertFalse(cfgmod.paths.config_file(e.cdir).exists())

    def test_tenant_desconhecido_avisa(self):
        e = Env(self, configured=False)
        e.ctx.tenant_lookup = lambda o: None
        env = e.call("ado_setup_org", org="OrgA")
        self.assertIn("tenant_warning", env["data"])

    def test_falha_de_rede_no_tenant(self):
        e = Env(self, configured=False)

        def boom(o):
            raise adohttp.HttpError("NETWORK", "Falha de rede")
        e.ctx.tenant_lookup = boom
        env = e.call("ado_setup_org", org="OrgA")
        self.assertIsNone(env["data"]["tenant"])
        self.assertIn("NETWORK", env["data"]["note"])


class LoginTest(unittest.TestCase):
    def wait_login(self, e, **kw):
        import time
        for _ in range(100):
            env = e.call("ado_auth_login", **kw)
            if env["reason_code"] != "LOGIN_RUNNING":
                return env
            time.sleep(0.1)
        self.fail("login não terminou")

    def test_browser(self):
        e = Env(self, az=True)
        env = self.wait_login(e)
        self.assertEqual((env["status"], env["reason_code"]), ("ok", "LOGIN_OK"))
        argv = [l["argv"] for l in e.h.log(e.az_state) if l["argv"][:1] == ["login"]][0]
        self.assertEqual(argv[argv.index("--tenant") + 1], TENANT)
        self.assertNotIn("--use-device-code", argv)
        self.assertEqual(e.ctx.logins, {})

    def test_device_code(self):
        e = Env(self, az=True)
        env = e.call("ado_auth_login", mode="device_code")
        self.assertEqual(env["reason_code"], "LOGIN_RUNNING")
        self.assertEqual(env["data"]["login"]["code"], "FAKE12345")
        self.assertIn("FAKE12345", env["next_action"])
        self.assertIn("https://microsoft.com/devicelogin", env["next_action"])
        self.assertEqual(self.wait_login(e, mode="device_code")["reason_code"], "LOGIN_OK")

    def test_nao_inicia_dois_logins(self):
        e = Env(self, az=True)
        e.call("ado_auth_login", mode="device_code")
        e.call("ado_auth_login", mode="device_code")
        self.wait_login(e, mode="device_code")
        logins = [l for l in e.h.log(e.az_state) if l["argv"][:1] == ["login"]]
        self.assertEqual(len(logins), 1)

    def test_falha(self):
        e = Env(self, az=True, az_state={"mode": "login_fail"})
        env = self.wait_login(e)
        self.assertEqual((env["status"], env["reason_code"]), ("failed", "AZ_LOGIN_FAILED"))

    def test_sem_tenant(self):
        e = Env(self, az=True)
        p = cfgmod.paths.config_file(e.cdir)
        cfgmod.save_config(cfgmod.set_org(cfgmod.load_config(p), "OrgA", tenant=None), p)
        e.ctx.tenant_lookup = lambda o: None
        env = e.call("ado_auth_login")
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "TENANT_UNKNOWN"))


class ReadTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        me = {"displayName": "Eu Mesmo", "uniqueName": fake_ado.ME_UNIQUE, "id": fake_ado.ME}
        self.us = a.add_item("User Story", "Painel")
        self.t1 = a.add_item("Task", "Primeira", state="Em Andamento", **{"System.AssignedTo": me})
        self.t2 = a.add_item("Task", "Segunda", **{"System.AssignedTo": me})
        a.link_parent(self.t1, self.us)
        a.link_parent(self.t2, self.us)

    def test_whoami(self):
        d = self.e.call("ado_whoami")["data"]
        self.assertEqual((d["displayName"], d["uniqueName"]), ("Eu Mesmo", fake_ado.ME_UNIQUE))

    def test_connection_data_usa_api_preview(self):
        # o ADO real recusa api-version=7.1 em connectionData (400); o fake não confere, então o teste confere
        self.e.call("ado_whoami")
        self.e.call("ado_status", check_remote=True)
        qs = [r["query"].get("api-version") for r in self.e.ado.requests if r["path"].endswith("/connectionData")]
        self.assertTrue(qs)
        self.assertEqual(set(qs), {"7.1-preview.1"})

    def test_projetos_teams_iteracoes(self):
        p = self.e.call("ado_list_projects")["data"]
        self.assertEqual(p["count"], 2)
        self.assertEqual({x["name"] for x in p["projects"]}, {"Projeto Ágil", "Projeto Beta"})
        t = self.e.call("ado_list_teams", project="Projeto Ágil")["data"]
        self.assertEqual(t["teams"][0]["name"], "Time Ágil")
        it = self.e.call("ado_iterations", project=PROJ, team="Time Ágil")["data"]
        self.assertEqual([i["name"] for i in it["iterations"]], ["Sprint 2"])
        fut = self.e.call("ado_iterations", project=PROJ, team="Time Ágil", timeframe="future")["data"]
        self.assertEqual([i["name"] for i in fut["iterations"]], ["Sprint 3"])

    def test_iteracoes_com_team_padrao(self):
        self.e.call("ado_set_defaults", project=PROJ, defaults={"team": "Time Ágil"}, confirm=True)
        it = self.e.call("ado_iterations", project=PROJ)
        self.assertEqual(it["status"], "ok", it)
        self.assertEqual(self.e.call("ado_iterations", project="Projeto Beta")["reason_code"], "TEAM_REQUIRED")

    def test_projeto_obrigatorio(self):
        self.assertEqual(self.e.call("ado_list_teams")["reason_code"], "PROJECT_REQUIRED")

    def test_get(self):
        item = self.e.call("ado_get", id=self.t1)["data"]["item"]
        self.assertEqual((item["type"], item["state"], item["state_category"]), ("Task", "Em Andamento", "InProgress"))
        self.assertEqual(item["parent_id"], self.us)
        self.assertIn("web_url", item)
        us = self.e.call("ado_get", id=str(self.us))["data"]["item"]
        self.assertEqual(sorted(us["children_ids"]), [self.t1, self.t2])
        none = self.e.call("ado_get", id=self.t1, expand="none")["data"]["item"]
        self.assertEqual(none["relations"], [])

    def test_query_atribuidos(self):
        d = self.e.call("ado_query", project="Projeto Ágil", assigned_to_me=True)["data"]
        self.assertEqual(sorted(i["id"] for i in d["items"]), [self.t1, self.t2])
        self.assertIn("@Me", d["wiql"])
        d2 = self.e.call("ado_query", project=PROJ, assigned_to_me=True, state_category="InProgress", type="Task")["data"]
        self.assertEqual([i["id"] for i in d2["items"]], [self.t1])
        d3 = self.e.call("ado_query", project=PROJ, types=["Task"], top=1, fields=["System.Title"])["data"]
        self.assertEqual(d3["count"], 1)
        self.assertEqual(list(d3["items"][0]["fields"]), ["System.Title"])

    def test_query_wiql_livre_e_erro(self):
        d = self.e.call("ado_query", project=PROJ, wiql="SELECT [System.Id] FROM WorkItems WHERE [System.Id] > 0")["data"]
        self.assertGreaterEqual(d["count"], 3)
        self.assertEqual(self.e.call("ado_query", state_category="Foo", project=PROJ)["reason_code"], "STATE_INVALID")

    def test_filhos(self):
        d = self.e.call("ado_children", id=self.us)["data"]
        self.assertEqual(sorted(i["id"] for i in d["items"]), [self.t1, self.t2])

    def test_comentarios(self):
        self.e.ado.comments[self.t1] = [{"id": 1, "text": "<b>oi</b><br>tudo", "createdDate": "2026-01-01T00:00:00Z",
                                         "createdBy": {"displayName": "Ana", "uniqueName": "a@example.com"}, "version": 1}]
        d = self.e.call("ado_comments", id=self.t1)["data"]
        self.assertEqual(d["count"], 1)
        self.assertIn("oi", d["comments"][0]["plain"])
        self.assertNotIn("<b>", d["comments"][0]["plain"])
        self.assertEqual(self.e.call("ado_comments", id=self.t1, project=PROJ)["data"]["count"], 1)

    def test_resolver_identidade(self):
        one = self.e.call("ado_resolve_identity", text="Ana Souza")["data"]
        self.assertEqual([c["displayName"] for c in one["candidates"]], ["Ana Souza"])
        many = self.e.call("ado_resolve_identity", text="Ana", project=PROJ, team="Time Ágil")
        self.assertEqual(many["data"]["count"], 2)
        self.assertIn("qual", many["next_action"])
        self.assertEqual(self.e.call("ado_resolve_identity", text="  ")["reason_code"], "IDENTITY_TEXT_REQUIRED")

    def test_nenhuma_escrita_nas_leituras(self):
        for n, a in (("ado_get", {"id": self.t1}), ("ado_children", {"id": self.us}), ("ado_whoami", {}),
                     ("ado_list_projects", {}), ("ado_comments", {"id": self.t1})):
            self.e.call(n, **a)
        self.assertEqual(self.e.ado.writes(), [])


class TypeSchemaTest(unittest.TestCase):
    def test_pelo_perfil(self):
        e = Env(self)
        d = e.call("ado_type_schema", project="Projeto Ágil", type="task")["data"]
        self.assertEqual(d["source"], "learned")
        self.assertIn("Custom.Equipe", d["required_on_create"])
        self.assertEqual(d["picklists"]["Custom.Equipe"], ["DADOS", "QA"])
        self.assertEqual(d["states_by_category"]["Proposed"], ["Aberto"])
        self.assertEqual(d["defaults"]["Microsoft.VSTS.Common.Priority"], 2)
        e.assert_no_leak()

    def test_sem_perfil_descobre_sem_gravar(self):
        e = Env(self, org="ORG-EXEMPLO", discovery=True, profile=False)
        env = e.call("ado_type_schema", project=PROJ, type="Task")
        self.assertEqual(env["status"], "ok", env)
        self.assertEqual(env["data"]["source"], "discovered")
        self.assertIn("Custom.Equipe", env["data"]["required_on_create"])
        self.assertEqual(list((e.cdir / "profiles").iterdir()), [])
        self.assertEqual(e.ado.writes(), [])
        self.assertEqual(e.call("ado_type_schema", project=PROJ, type="Nada")["reason_code"], "TYPE_NOT_FOUND")

    def test_descoberta_e_gravacao_do_perfil(self):
        e = Env(self, org="ORG-EXEMPLO", discovery=True, profile=False)
        env = e.call("ado_discover_org", project=PROJ)
        self.assertEqual(env["status"], "ok", env)
        d = env["data"]
        self.assertEqual(d["current_profile"], "absent")
        self.assertNotIn("diff", d)
        names = [p["name"] for p in d["summary"]["processes"]]
        self.assertEqual(names, ["PROC_EXEMPLO", "PROC_OUTRO"])
        self.assertEqual(list((e.cdir / "profiles").iterdir()), [])  # discover não grava
        pid = d["proposal_id"]
        ask = e.call("ado_profile_save", proposal_id=pid)
        self.assertEqual((ask["status"], ask["reason_code"]), ("needs_decision", "CONFIRM_REQUIRED"))
        self.assertEqual(list((e.cdir / "profiles").iterdir()), [])
        done = e.call("ado_profile_save", proposal_id=pid, confirm=True)
        self.assertEqual(done["reason_code"], "PROFILE_SAVED", done)
        saved = e.cdir / "profiles" / "ORG-EXEMPLO.json"
        self.assertTrue(saved.is_file())
        self.assertEqual(profile_validate.validate_file(saved), [])
        # agora o perfil aprendido vale e a segunda descoberta traz diff (vazio) contra ele
        again = e.call("ado_discover_org", project=PROJ)["data"]
        self.assertEqual(again["current_profile"], "learned")
        self.assertEqual(again["diff_total"], 0)
        self.assertEqual(e.ado.writes(), [])

    def test_proposta_desconhecida(self):
        e = Env(self)
        self.assertEqual(e.call("ado_profile_save", proposal_id="zz", confirm=True)["reason_code"], "PROPOSAL_NOT_FOUND")

    def test_discover_so_faz_get(self):
        e = Env(self, org="ORG-EXEMPLO", discovery=True, profile=False)
        e.call("ado_discover_org", project=PROJ)
        self.assertEqual(e.ado.requests, [])  # o cliente da descoberta é o falso: nenhum pedido sai

    def test_projeto_inexistente(self):
        e = Env(self, org="ORG-EXEMPLO", discovery=True, profile=False)
        self.assertEqual(e.call("ado_discover_org", project="Nao Existe")["reason_code"], "PROJECT_NOT_FOUND")


class DefaultsTest(Base):
    def test_fluxo(self):
        a = dict(project="Projeto Ágil", defaults={"team": "Time Ágil", "area_path": "Projeto Ágil\\Dados",
                                                  "fields": {"Custom.Equipe": "DADOS"}}, me_unique_name="eu@example.com")
        ask = self.e.call("ado_set_defaults", **a)
        self.assertEqual((ask["status"], ask["reason_code"]), ("needs_decision", "CONFIRM_REQUIRED"))
        self.assertEqual(cfgmod.load_config(cfgmod.paths.config_file(self.e.cdir))["orgs"]["OrgA"]["defaults"], {})
        self.assertEqual(self.e.call("ado_set_defaults", confirm=True, **a)["reason_code"], "DEFAULTS_SAVED")
        entry = cfgmod.load_config(cfgmod.paths.config_file(self.e.cdir))["orgs"]["OrgA"]
        self.assertEqual(entry["defaults"][PROJ]["team"], "Time Ágil")
        self.assertEqual(entry["defaults"][PROJ]["fields"], {"Custom.Equipe": "DADOS"})
        self.assertEqual(entry["me"], {"uniqueName": "eu@example.com"})
        self.e.call("ado_set_defaults", project=PROJ, defaults={"fields": {"Custom.Equipe": None}}, confirm=True)
        entry = cfgmod.load_config(cfgmod.paths.config_file(self.e.cdir))["orgs"]["OrgA"]
        self.assertEqual(entry["defaults"][PROJ]["fields"], {})

    def test_o_padrao_alimenta_a_criacao(self):
        self.e.call("ado_set_defaults", project=PROJ, defaults={"fields": {"Custom.Equipe": "QA"}}, confirm=True)
        f = dict(TASK)
        del f["Custom.Equipe"]
        env = self.e.call("ado_prepare_create", project=PROJ, type="Task", fields=f)
        self.assertEqual(env["status"], "ok", env)
        self.assertEqual(env["data"]["fields"]["Custom.Equipe"], "QA")

    def test_campo_invalido_e_vazio(self):
        bad = self.e.call("ado_set_defaults", project=PROJ, defaults={"fields": {"token": "x"}}, confirm=True)
        self.assertEqual(bad["reason_code"], "FIELD_INVALID")
        bad = self.e.call("ado_set_defaults", project=PROJ, defaults={"fields": {"Custom.X": {"a": 1}}}, confirm=True)
        self.assertEqual(bad["reason_code"], "FIELD_INVALID")
        self.assertEqual(self.e.call("ado_set_defaults", confirm=True)["reason_code"], "NOTHING_TO_SET")


class WriteFlowTest(Base):
    def setUp(self):
        super().setUp()
        self.us = self.e.ado.add_item("User Story", "Painel", **{"System.AreaPath": "Projeto Ágil\\Dados"})

    def test_criar_com_apply_args_intactos(self):
        env = self.e.call("ado_prepare_create", project="Projeto Ágil", type="Task", fields=TASK, parent_id=self.us,
                          hyperlinks=["https://example.com/wiki"])
        self.assertEqual((env["status"], env["reason_code"]), ("ok", "PREVIEW_READY"), env)
        self.assertEqual(self.e.ado.writes(), [])
        args = env["data"]["apply_args"]
        self.assertEqual(set(args), {"preview_id", "hash", "operation", "org", "type", "title"})
        self.assertEqual((args["operation"], args["org"], args["type"], args["title"]),
                         ("create", "OrgA", "Task", "Revisar carga"))
        res = self.e.call("ado_apply", **args)
        self.assertEqual((res["status"], res["reason_code"]), ("ok", "APPLIED"), res)
        new = res["data"]["id"]
        self.assertEqual(self.e.ado.items[new]["fields"]["System.Title"], "Revisar carga")
        again = self.e.call("ado_apply", **args)
        self.assertEqual((again["status"], again["reason_code"]), ("blocked", "ALREADY_APPLIED"))
        self.assertEqual(len(self.e.ado.writes()), 1)

    def test_faltam_campos(self):
        env = self.e.call("ado_prepare_create", project=PROJ, type="Task", fields={"System.Title": "x"})
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "MISSING_FIELDS"))
        self.assertEqual(self.e.ado.writes(), [])

    def test_apply_recusa_hash_e_campo_diferente(self):
        env = self.e.call("ado_prepare_create", project=PROJ, type="Task", fields=TASK)
        bad = self.e.apply(env, hash="0" * 64)
        self.assertEqual((bad["status"], bad["reason_code"]), ("blocked", "HASH_MISMATCH"))
        bad = self.e.apply(env, title="Outro título")
        self.assertEqual((bad["status"], bad["reason_code"]), ("blocked", "PREVIEW_MISMATCH"))
        bad = self.e.apply(env, org="OrgA", preview_id="f" * 32)
        self.assertEqual(bad["reason_code"], "PREVIEW_NOT_FOUND")
        self.assertEqual(self.e.ado.writes(), [])
        self.assertEqual(self.e.apply(env)["reason_code"], "APPLIED")

    def test_apply_de_org_nao_configurada(self):
        env = self.e.call("ado_prepare_create", project=PROJ, type="Task", fields=TASK)
        bad = self.e.apply(env, org="Outra")
        self.assertEqual(bad["status"], "needs_setup")
        self.assertEqual(self.e.ado.writes(), [])

    def test_atualizar_estado_por_intencao(self):
        t = self.e.ado.add_item("Task", "Fazer", **{"Custom.Equipe": "DADOS"})
        env = self.e.call("ado_prepare_update", id=t, state_intent="iniciar", fields={"System.Tags": "x"})
        self.assertEqual(env["status"], "ok", env)
        args = env["data"]["apply_args"]
        self.assertEqual((args["operation"], args["target_id"], args["title"]), ("update", t, "Fazer"))
        res = self.e.call("ado_apply", **args)
        self.assertEqual(res["reason_code"], "APPLIED", res)
        self.assertEqual(self.e.ado.items[t]["fields"]["System.State"], "Em Andamento")

    def test_atualizar_sem_mudanca(self):
        t = self.e.ado.add_item("Task", "Fazer")
        env = self.e.call("ado_prepare_update", id=t, state_intent="Aberto")
        self.assertEqual(env["reason_code"], "NO_CHANGES")

    def test_comentario_com_mencao(self):
        t = self.e.ado.add_item("Task", "Fazer")
        env = self.e.call("ado_prepare_comment", id=t, text="Revisado: <ok> & pronto", mentions=["Ana Souza"])
        self.assertEqual(env["status"], "ok", env)
        self.assertIn(fake_ado.ANA, env["data"]["html"])
        self.assertIn("&lt;ok&gt;", env["data"]["html"])
        res = self.e.call("ado_apply", **env["data"]["apply_args"])
        self.assertEqual(res["reason_code"], "APPLIED", res)
        self.assertEqual(len(self.e.ado.comments[t]), 1)

    def test_comentario_mencao_ambigua_e_corpo(self):
        t = self.e.ado.add_item("Task", "Fazer")
        amb = self.e.call("ado_prepare_comment", id=t, html="oi", mentions=["Ana"])
        self.assertEqual((amb["status"], amb["reason_code"]), ("needs_decision", "IDENTITY_AMBIGUOUS"))
        self.assertEqual(len(amb["data"]["options"]), 2)
        both = self.e.call("ado_prepare_comment", id=t, html="a", text="b")
        self.assertEqual(both["reason_code"], "COMMENT_BODY")
        none = self.e.call("ado_prepare_comment", id=t)
        self.assertEqual(none["reason_code"], "COMMENT_BODY")

    def test_anexo(self):
        t = self.e.ado.add_item("Task", "Fazer")
        f = self.e.files / "evidência.txt"
        f.write_text("conteúdo", encoding="utf-8")
        env = self.e.call("ado_prepare_attach", id=t, file_path=str(f), comment="print")
        self.assertEqual(env["status"], "ok", env)
        self.assertEqual(self.e.ado.attachments, {})  # a prévia não envia
        self.assertEqual(env["data"]["file"]["name"], "evidência.txt")
        self.assertEqual(len(env["data"]["file"]["sha256"]), 64)
        args = env["data"]["apply_args"]
        self.assertEqual((args["operation"], args["file_name"]), ("attach", "evidência.txt"))
        res = self.e.call("ado_apply", **args)
        self.assertEqual(res["reason_code"], "APPLIED", res)
        self.assertEqual(len(self.e.ado.attachments), 1)

    def test_anexo_em_pasta_de_credencial(self):
        d = self.e.tmp / "home" / ".ssh"
        d.mkdir()
        (d / "id_x").write_text("x", encoding="utf-8")
        t = self.e.ado.add_item("Task", "Fazer")
        env = self.e.call("ado_prepare_attach", id=t, file_path=str(d / "id_x"))
        self.assertEqual(env["reason_code"], "FILE_DENIED")
        self.assertEqual(self.e.ado.attachments, {})

    def test_reconcile(self):
        env = self.e.call("ado_prepare_create", project=PROJ, type="Task", fields=TASK)
        r = self.e.call("ado_reconcile", preview_id=env["data"]["preview_id"])
        self.assertEqual(r["reason_code"], "NOTHING_TO_RECONCILE")
        self.assertEqual(self.e.call("ado_reconcile", preview_id="1" * 32)["status"], "blocked")


class ConfigSchemaTest(unittest.TestCase):
    SCHEMA = json.loads((PLUGIN / "profiles" / "config.schema.json").read_text(encoding="utf-8"))

    def check(self, value):
        errs = []
        profile_validate._check(value, self.SCHEMA, self.SCHEMA, (), errs)
        return errs

    def test_sem_deriva_em_relacao_ao_codigo(self):
        top = set(self.SCHEMA["properties"])
        self.assertEqual(set(cfgmod.default_config()), top)
        org = self.SCHEMA["$defs"]["org"]["properties"]
        self.assertEqual(set(cfgmod.default_org_entry("X")), set(org))
        self.assertEqual(set(cfgmod.default_org_entry("X")["pat_ref"]), set(org["pat_ref"]["properties"]))
        self.assertIn("nordpass_folder", org["pat_ref"]["properties"])

    def test_config_real_valida(self):
        cfg = cfgmod.set_org(cfgmod.default_config(), "OrgA", tenant=TENANT, auth="pat",
                             pat_ref={"nordpass_title": "T", "nordpass_folder": "F"}, default_project=PROJ,
                             me={"uniqueName": "a@example.com"},
                             defaults={PROJ: {"team": "T", "area_path": "A", "fields": {"Custom.X": 1}}})
        self.assertEqual(self.check(cfg), [])
        self.assertEqual(self.check(cfgmod.default_config()), [])

    def test_recusa_o_invalido(self):
        self.assertTrue(self.check({"schema_version": 1, "orgs": {"a b": {}}}))
        self.assertTrue(self.check({"schema_version": 1, "orgs": {"A": {"pat": "x"}}}))
        self.assertTrue(self.check({"schema_version": 1, "orgs": {"A": {"auth": "senha"}}}))
        self.assertTrue(self.check({"schema_version": 2}))


if __name__ == "__main__":
    unittest.main()

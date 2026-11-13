"""Escritas em duas fases contra o ADO falso local: prévia, validateOnly, apply, releitura e reconcile."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "server"))
sys.path.insert(0, str(PLUGIN / "tests" / "fixtures" / "workitems"))
sys.path.insert(0, str(PLUGIN / "tests" / "fakes"))
import fake_ado  # noqa: E402
import fakelib  # noqa: E402
from adoplug import http as adohttp  # noqa: E402
from adoplug.preview import PreviewStore  # noqa: E402
from adoplug.workitems import PARENT_REL, WorkItemsApi  # noqa: E402
from adoplug.writes import Writes  # noqa: E402

PROJ, PROJ2 = fake_ado.PROJ, fake_ado.PROJ2
TASK = {"System.Title": "Revisar carga", "Custom.Equipe": "DADOS",
        "Microsoft.VSTS.Scheduling.OriginalEstimate": "2", "Microsoft.VSTS.Common.Priority": "3"}


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class TimeoutAfter:
    """Envia a escrita de verdade e depois levanta TIMEOUT, como uma resposta perdida."""

    def __init__(self, http, path_regex):
        self.http, self.rx, self.armed = http, re.compile(path_regex), True

    def request(self, method, org, path, **kw):
        r = self.http.request(method, org, path, **kw)
        q = kw.get("query") or {}
        if self.armed and method in ("POST", "PATCH") and q.get("validateOnly") != "true" and self.rx.search(path):
            self.armed = False
            raise adohttp.HttpError("TIMEOUT", "O Azure DevOps não respondeu a tempo.")
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.ado = fake_ado.FakeAdo()
        self.addCleanup(self.ado.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="adoplug-w-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfgdir = self.home / "cfg-plugin"
        self.tok = fakelib.sentinel()
        self.http = adohttp.AdoHttp(fake_ado.FakeAuth(self.tok), ["OrgA"], scheme_host=self.ado.base)
        self.prof = fake_ado.profile()
        self.cfg = {"orgs": {"OrgA": {"defaults": {}}}}
        self.clock = Clock()
        self.store = PreviewStore(self.tmp / "previews", clock=self.clock)
        self.envs = []
        self.build(self.http)

    def build(self, http, profiles=None):
        self.api = WorkItemsApi(http, profiles or (lambda org: (self.prof, "embedded")))
        self.w = Writes(self.api, self.store, self.cfg, clock=self.clock, home=self.home, config_dir=self.cfgdir)

    def keep(self, env):
        self.envs.append(env)
        return env

    def apply(self, env, **override):
        args = dict(env["data"]["apply_args"])
        args.update(override)
        return self.keep(self.w.apply(**args))

    def posts(self, rx):
        return [r for r in self.ado.requests if r["method"] in ("POST", "PATCH") and re.search(rx, r["path"])]

    def tearDown(self):
        # I2: o sentinela não aparece em retorno nem em arquivo de prévia.
        text = json.dumps(self.envs, ensure_ascii=False, default=str)
        for p in (self.tmp / "previews").glob("*.json") if (self.tmp / "previews").is_dir() else []:
            text += p.read_text(encoding="utf-8")
        for form in fakelib.leak_forms(self.tok):
            self.assertNotIn(form, text)


class CreateTest(Base):
    def test_fluxo_completo(self):
        us = self.ado.add_item("User Story", "US", **{"System.AreaPath": "Projeto Ágil\\Dados",
                                                      "System.IterationPath": "Projeto Ágil\\Sprint 2"})
        env = self.keep(self.w.prepare_create("OrgA", "Projeto Ágil", "Task", TASK, parent_id=us,
                                              hyperlinks=[{"url": "https://example.com/wiki", "comment": "wiki"}]))
        self.assertEqual(env["status"], "ok", env)
        d = env["data"]
        self.assertEqual(d["fields"]["System.AreaPath"], "Projeto Ágil\\Dados")
        self.assertEqual(d["fields"]["System.IterationPath"], "Projeto Ágil\\Sprint 2")
        self.assertEqual(d["fields"]["Microsoft.VSTS.Common.Priority"], 3)
        self.assertEqual(d["fields"]["Microsoft.VSTS.Scheduling.OriginalEstimate"], 2.0)
        self.assertEqual(d["parent"]["state"], "Aberto")
        self.assertEqual(self.ado.writes(), [])
        vo = self.posts(r"/workitems/\$Task$")
        self.assertEqual(len(vo), 1)
        self.assertIn(f"/OrgA/{PROJ}/_apis/wit/workitems/$Task?", vo[0]["raw_path"])
        self.assertEqual(vo[0]["query"]["validateOnly"], "true")
        self.assertTrue(vo[0]["headers"]["content-type"].startswith("application/json-patch+json"))
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["reason_code"], "APPLIED")
        real = self.ado.writes()
        self.assertEqual(len(real), 1)
        self.assertLess(self.ado.requests.index(vo[0]), self.ado.requests.index(real[0]))
        new = res["data"]["id"]
        self.assertEqual(self.ado.items[new]["fields"]["System.Parent"], us)
        self.assertEqual(res["data"]["parent_id"], us)
        self.assertEqual(res["data"]["readback"]["mismatches"], [])
        self.assertEqual(self.store.load(d["preview_id"])["state"], "applied")
        self.assertFalse(any("bypass" in r["raw_path"].lower() for r in self.ado.requests))

    def test_sem_parent_usa_raiz_do_projeto(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", TASK))
        self.assertEqual(env["status"], "ok", env)
        self.assertEqual(env["data"]["fields"]["System.AreaPath"], "Projeto Ágil")
        self.assertEqual(env["data"]["fields"]["System.IterationPath"], "Projeto Ágil")
        self.assertEqual(env["data"]["fields"]["Microsoft.VSTS.Common.Priority"], 3)

    def test_defaults_da_config(self):
        self.cfg["orgs"]["OrgA"]["defaults"] = {PROJ: {"area_path": "Projeto Ágil\\Time", "fields": {"Custom.Equipe": "QA"}}}
        f = {k: v for k, v in TASK.items() if k != "Custom.Equipe"}
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", f))
        self.assertEqual(env["data"]["fields"]["Custom.Equipe"], "QA")
        self.assertEqual(env["data"]["fields"]["System.AreaPath"], "Projeto Ágil\\Time")

    def test_faltam_campos_sem_post(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", {"System.Title": "X"}))
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "MISSING_FIELDS"))
        miss = {m["field"]: m for m in env["data"]["missing"]}
        self.assertEqual(set(miss), {"Custom.Equipe", "Microsoft.VSTS.Scheduling.OriginalEstimate"})
        self.assertEqual(miss["Custom.Equipe"]["options"], ["DADOS", "QA"])
        self.assertNotIn("preview_id", env["data"])
        self.assertEqual(self.posts(r"/workitems"), [])

    def test_picklist_invalida(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", dict(TASK, **{"Custom.Equipe": "XYZ"})))
        self.assertEqual(env["status"], "needs_decision")
        self.assertEqual(env["data"]["invalid"][0]["field"], "Custom.Equipe")

    def test_validate_only_recusa_sem_perfil(self):
        self.build(self.http, lambda org: (None, "absent"))
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", {"System.Title": "X"}))
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "VALIDATION_FAILED"))
        self.assertIn("TF401320", env["diagnostic"]["message"])
        self.assertNotIn("preview_id", env["data"])
        self.assertEqual(self.ado.writes(), [])

    def test_campo_somente_leitura_e_tipo(self):
        for f in ({"System.Id": 3}, {"System.Parent": 3}, {"nao campo": 1}):
            env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", dict(TASK, **f)))
            self.assertEqual(env["status"], "failed", f)
        self.assertEqual(self.ado.requests, [])

    def test_duplicata_vira_aviso(self):
        us = self.ado.add_item("User Story", "US")
        t = self.ado.add_item("Task", TASK["System.Title"])
        self.ado.link_parent(t, us)
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", TASK, parent_id=us))
        self.assertEqual(env["status"], "ok")
        self.assertEqual([x["id"] for x in env["data"]["duplicates"]], [t])
        self.assertIn("mesmo título", env["next_action"])

    def test_apply_recusado_nao_faz_pedido(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", TASK))
        n = len(self.ado.requests)
        for over, code in (({"hash": "0" * 64}, "HASH_MISMATCH"), ({"title": "Outro"}, "PREVIEW_MISMATCH"),
                           ({"type": "Bug"}, "PREVIEW_MISMATCH"), ({"org": "OrgB"}, "PREVIEW_MISMATCH"),
                           ({"operation": "update"}, "PREVIEW_MISMATCH")):
            res = self.apply(env, **over)
            self.assertEqual((res["status"], res["reason_code"]), ("blocked", code))
        self.clock.t += 31 * 60
        self.assertEqual(self.apply(env)["reason_code"], "PREVIEW_EXPIRED")
        self.assertEqual(len(self.ado.requests), n)

    def test_apply_duas_vezes(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", TASK))
        self.assertEqual(self.apply(env)["status"], "ok")
        self.assertEqual(self.apply(env)["reason_code"], "ALREADY_APPLIED")
        self.assertEqual(len(self.ado.writes()), 1)


class UpdateTest(Base):
    def test_estado_tags_campos_hyperlink(self):
        t = self.ado.add_item("Task", "T", **{"System.Tags": "a; b"})
        env = self.keep(self.w.prepare_update("OrgA", t, {"System.Tags": "B; c",
                                                          "Microsoft.VSTS.Scheduling.RemainingWork": "1,5"},
                                              state_intent="iniciar", add_hyperlinks=["https://example.com/x"]))
        self.assertEqual(env["status"], "ok", env)
        vo = self.posts(rf"/workitems/{t}$")[0]
        self.assertEqual(vo["path"], f"/OrgA/{PROJ}/_apis/wit/workitems/{t}")
        self.assertEqual(vo["query"]["validateOnly"], "true")
        body = json.loads(vo["body"])
        self.assertEqual(body[0], {"op": "test", "path": "/rev", "value": 1})
        ch = {c.get("field"): c.get("to") for c in env["data"]["changes"]}
        self.assertEqual(ch["System.State"], "Em Andamento")
        self.assertEqual(ch["System.Tags"], "a; b; c")
        self.assertEqual(ch["Microsoft.VSTS.Scheduling.RemainingWork"], 1.5)
        self.assertEqual(self.ado.writes(), [])
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        it = self.ado.items[t]
        self.assertEqual((it["rev"], it["fields"]["System.State"], it["fields"]["System.Tags"]), (2, "Em Andamento", "a; b; c"))
        self.assertEqual(res["data"]["state_category"], "InProgress")
        self.assertEqual(res["data"]["readback"]["mismatches"], [])

    def test_revisao_mudou(self):
        t = self.ado.add_item("Task", "T")
        env = self.keep(self.w.prepare_update("OrgA", t, state_intent="concluir"))
        self.ado.items[t]["rev"] += 1
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"]), ("failed", "REVISION_CHANGED"))
        self.assertEqual(self.ado.items[t]["fields"]["System.State"], "Aberto")
        self.assertEqual(self.apply(env)["reason_code"], "PREVIEW_CLOSED")

    def test_estado_ambiguo_ou_ausente(self):
        e = self.ado.add_item("Epic", "E")
        env = self.keep(self.w.prepare_update("OrgA", e, state_intent="iniciar"))
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "STATE_AMBIGUOUS"))
        self.assertEqual([o["name"] for o in env["data"]["options"]], ["Em Andamento", "Suspenso"])
        b = self.ado.add_item("Bug", "B")
        env = self.keep(self.w.prepare_update("OrgA", b, state_intent="cancelar"))
        self.assertEqual(env["reason_code"], "STATE_NOT_AVAILABLE")
        env = self.keep(self.w.prepare_update("OrgA", e, state_intent="Suspenso"))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(self.ado.writes(), [])

    def test_sem_mudanca(self):
        t = self.ado.add_item("Task", "T", **{"System.Tags": "a"})
        env = self.keep(self.w.prepare_update("OrgA", t, {"System.Tags": "A"}, state_intent="reabrir"))
        self.assertEqual((env["status"], env["reason_code"]), ("ok", "NO_CHANGES"))
        self.assertEqual(self.posts(r"/workitems/"), [])

    def test_trocar_parent(self):
        us1, us2 = self.ado.add_item("User Story", "U1"), self.ado.add_item("User Story", "U2",
                                                                            **{"Microsoft.VSTS.Scheduling.RemainingWork": 4.0})
        t = self.ado.add_item("Task", "T")
        self.ado.link_parent(t, us1)
        env = self.keep(self.w.prepare_update("OrgA", t, add_parent=us2))
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "PARENT_EXISTS"))
        env = self.keep(self.w.prepare_update("OrgA", t, add_parent=us2,
                                              remove_relations=[{"rel": PARENT_REL, "target_id": us1}]))
        self.assertEqual(env["status"], "ok", env)
        self.assertEqual(env["data"]["parent"]["remaining_work"], 4.0)
        body = json.loads(self.posts(rf"/workitems/{t}$")[-1]["body"])
        self.assertEqual([o["op"] for o in body], ["test", "remove", "add"])
        self.assertEqual(body[1]["path"], "/relations/0")
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(self.ado.items[t]["fields"]["System.Parent"], us2)
        self.assertEqual(res["data"]["readback"]["mismatches"], [])

    def test_remover_campo(self):
        t = self.ado.add_item("Task", "T", **{"Custom.Nota": "x"})
        env = self.keep(self.w.prepare_update("OrgA", t, {"Custom.Nota": None}))
        body = json.loads(self.posts(rf"/workitems/{t}$")[-1]["body"])
        self.assertEqual(body[1], {"op": "remove", "path": "/fields/Custom.Nota"})
        self.assertEqual(self.apply(env)["data"]["readback"]["mismatches"], [])
        self.assertNotIn("Custom.Nota", self.ado.items[t]["fields"])


class CommentTest(Base):
    def test_comentario_com_mencao(self):
        t = self.ado.add_item("Task", "T")
        env = self.keep(self.w.prepare_comment("OrgA", t, text='Feito "ok"\nveja https://example.com/a(b)',
                                               mentions=["ana souza"]))
        self.assertEqual(env["status"], "ok", env)
        html = env["data"]["html"]
        self.assertTrue(html.startswith(f'<a href="#" data-vss-mention="version:2.0,{fake_ado.ANA}">@Ana Souza</a><br>'))
        self.assertIn('<a href="https://example.com/a(b)" target="_blank">', html)
        self.assertIn("@Ana Souza", env["data"]["text"])
        self.assertEqual(self.ado.writes(), [])  # comentário não tem validateOnly: nada vai no prepare
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        post = self.ado.writes()[0]
        self.assertEqual(post["path"], f"/OrgA/{PROJ}/_apis/wit/workItems/{t}/comments")
        self.assertEqual(post["query"]["api-version"], "7.1-preview.4")
        self.assertEqual(json.loads(post["body"]), {"text": html})
        self.assertIn("target=_blank", self.ado.comments[t][0]["text"])
        self.assertEqual(res["data"]["readback"]["mismatches"], [])

    def test_mencao_ambigua_ou_ausente(self):
        t = self.ado.add_item("Task", "T")
        env = self.keep(self.w.prepare_comment("OrgA", t, text="x", mentions=["ana"]))
        self.assertEqual((env["status"], env["reason_code"]), ("needs_decision", "IDENTITY_AMBIGUOUS"))
        self.assertEqual(len(env["data"]["options"]), 2)
        env = self.keep(self.w.prepare_comment("OrgA", t, text="x", mentions=["zé ninguém"]))
        self.assertEqual(env["reason_code"], "IDENTITY_NOT_FOUND")
        env = self.keep(self.w.prepare_comment("OrgA", t, text="x", mentions=[{"id": fake_ado.ANA2, "displayName": "Ana Lima"}]))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(self.ado.writes(), [])

    def test_html_recusado_e_corpo(self):
        for kw in ({"html": "<script>x</script>"}, {"text": "a", "html": "<p>a</p>"}, {}, {"text": "  "}):
            env = self.keep(self.w.prepare_comment("OrgA", 1001, **kw))
            self.assertEqual(env["status"], "failed", kw)
        self.assertEqual(self.ado.requests, [])


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "evid"
        self.dir.mkdir()
        self.file = self.dir / "relatório final.pdf"
        self.file.write_bytes(b"%PDF-1.4 conteudo de teste")
        self.t = self.ado.add_item("Task", "T")

    def test_fluxo(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file), comment="evidência"))
        self.assertEqual(env["status"], "ok", env)
        f = env["data"]["file"]
        self.assertEqual(f["path"], str(self.file.resolve()))
        self.assertEqual(f["size"], self.file.stat().st_size)
        self.assertEqual(f["sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(env["data"]["apply_args"]["file_name"], "relatório final.pdf")
        self.assertEqual(self.ado.writes(), [])
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        up, rel = self.ado.writes()
        self.assertEqual(up["path"], f"/OrgA/{PROJ}/_apis/wit/attachments")
        self.assertEqual(up["query"]["fileName"], "relatório final.pdf")
        self.assertEqual(up["body"], self.file.read_bytes())
        self.assertEqual(up["headers"]["content-type"], "application/octet-stream")
        self.assertEqual(rel["method"], "PATCH")
        v = json.loads(rel["body"])[0]["value"]
        self.assertEqual((v["rel"], v["attributes"]["comment"]), ("AttachedFile", "evidência"))
        self.assertIn(f"/OrgA/{PROJ}/_apis/wit/attachments/", res["data"]["relation_url"])
        self.assertNotIn("fileName", res["data"]["relation_url"])
        self.assertEqual(res["data"]["readback"]["mismatches"], [])

    def test_caminhos_recusados(self):
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id").write_text("k")
        self.cfgdir.mkdir()
        (self.cfgdir / "config.json").write_text("{}")
        (self.dir / ".netrc").write_text("x")
        (self.dir / "proj" / ".aws").mkdir(parents=True)
        (self.dir / "proj" / ".aws" / "c").write_text("x")
        outside = self.tmp / "fora.txt"
        outside.write_text("segredo")
        link = self.dir / "link.txt"
        cases = [(str(self.home / ".ssh" / "id"), "FILE_DENIED"), (str(self.cfgdir / "config.json"), "FILE_DENIED"),
                 (str(self.dir / ".netrc"), "FILE_DENIED"), (str(self.dir / "proj" / ".aws" / "c"), "FILE_DENIED"),
                 ("evid/x.pdf", "FILE_PATH_RELATIVE"), (str(self.dir), "FILE_NOT_REGULAR"),
                 (str(self.dir / "nao.pdf"), "FILE_NOT_FOUND")]
        try:
            os.symlink(outside, link)
            cases.append((str(link), "FILE_SYMLINK_OUTSIDE"))
            os.symlink(self.home / ".ssh" / "id", self.dir / "chave")
            cases.append((str(self.dir / "chave"), "FILE_SYMLINK_OUTSIDE"))
        except (OSError, NotImplementedError):
            pass  # Windows sem privilégio de symlink
        for path, code in cases:
            env = self.keep(self.w.prepare_attach("OrgA", self.t, path))
            self.assertEqual((env["status"], env["reason_code"]), ("failed", code), path)
        self.assertEqual(self.ado.requests, [])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "permissão POSIX")
    def test_arquivo_sem_permissao_de_leitura(self):
        self.file.chmod(0)
        self.addCleanup(self.file.chmod, 0o600)
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.assertEqual((env["status"], env["reason_code"]), ("failed", "FILE_UNREADABLE"))

    def test_segredos_por_nome_extensao_e_perfil_de_navegador(self):
        browser = self.home / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
        browser.mkdir(parents=True)
        (browser / "Cookies").write_text("x")
        for d in (".snowflake", ".kube", ".docker", ".gnupg", ".mozilla"):
            (self.home / d).mkdir()
            (self.home / d / "c").write_text("x")
        (self.home / ".claude.json").write_text("x")
        names = [".env", ".env.local", "chave.pem", "a.KEY", "k.p8", "c.pfx", ".vault-token"]
        for n in names:
            (self.dir / n).write_text("x")
        targets = [browser / "Cookies", self.home / ".claude.json", *(self.home / d / "c" for d in
                   (".snowflake", ".kube", ".docker", ".gnupg", ".mozilla")), *(self.dir / n for n in names)]
        for p in targets:
            env = self.keep(self.w.prepare_attach("OrgA", self.t, str(p)))
            self.assertEqual((env["status"], env["reason_code"]), ("failed", "FILE_DENIED"), str(p))

    def test_arquivo_comum_nao_e_recusado(self):
        (self.dir / "ambiente.pdf").write_text("x")
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.dir / "ambiente.pdf")))
        self.assertEqual(env["status"], "ok")

    def test_appdata_do_ambiente(self):
        from adoplug import writes
        appdata = self.tmp / "roaming"
        cred = appdata / "Microsoft" / "Credentials"
        cred.mkdir(parents=True)
        (cred / "x").write_text("x")
        dirs = writes.denied_dirs(self.home, self.cfgdir, env={"APPDATA": str(appdata)})
        self.assertIn(cred, dirs)
        local = self.tmp / "local"
        dirs = writes.denied_dirs(self.home, self.cfgdir, env={"LOCALAPPDATA": str(local)})
        self.assertIn(local / "Google" / "Chrome", dirs)
        orig = writes.denied_dirs
        writes.denied_dirs = lambda h, c=None, env=None: orig(h, c, {"APPDATA": str(appdata)})
        self.addCleanup(setattr, writes, "denied_dirs", orig)
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(cred / "x")))
        self.assertEqual(env["reason_code"], "FILE_DENIED")

    def test_link_na_mesma_pasta_mostra_caminho_real(self):
        try:
            os.symlink(self.file, self.dir / "atalho.pdf")
        except (OSError, NotImplementedError):
            self.skipTest("symlink indisponível")
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.dir / "atalho.pdf")))
        self.assertEqual(env["data"]["file"]["path"], str(self.file.resolve()))

    def test_arquivo_mudou(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.file.write_bytes(b"outro conteudo")
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"]), ("blocked", "FILE_CHANGED"))
        self.assertEqual(self.ado.writes(), [])

    def test_item_mudou_antes_do_upload(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.ado.items[self.t]["rev"] += 1
        self.assertEqual(self.apply(env)["reason_code"], "REVISION_CHANGED")
        self.assertEqual(self.ado.writes(), [])

    def test_relacao_recusada_reaplica_sem_novo_upload(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.ado.fault("PATCH", r"/workitems/\d+$", "400_before")
        res = self.apply(env)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(res["data"]["uploaded"])
        self.assertEqual(self.store.load(env["data"]["preview_id"])["state"], "partial")
        res = self.apply(env)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(len(self.ado.attachments), 1)
        self.assertEqual(len(self.posts(r"/attachments$")), 1)

    def test_relacao_ambigua_reconcilia(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.ado.fault("PATCH", r"/workitems/\d+$", "500_after")
        res = self.apply(env)
        self.assertEqual((res["status"], res["data"]["outcome"]), ("ok", "applied"), res)
        self.assertEqual(res["data"]["after"], "ambiguous_write")

    def test_upload_ambiguo_sem_relacao(self):
        env = self.keep(self.w.prepare_attach("OrgA", self.t, str(self.file)))
        self.ado.fault("POST", r"/attachments$", "drop_after")
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"], res["data"]["outcome"]), ("failed", "NOT_APPLIED", "not_applied"))
        self.assertEqual(len(self.posts(r"/workitems/")), 0)


class AmbiguidadeTest(Base):
    def prep(self):
        env = self.keep(self.w.prepare_create("OrgA", PROJ, "Task", TASK))
        self.assertEqual(env["status"], "ok", env)
        return env

    def test_criacao_com_resposta_perdida_reconcilia_aplicada(self):
        env = self.prep()
        self.ado.fault("POST", r"/workitems/\$Task$", "drop_after")
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"], res["data"]["outcome"]), ("ok", "APPLIED", "applied"), res)
        self.assertEqual(len(self.ado.writes()), 1)  # sem reenvio
        self.assertIn(res["data"]["id"], self.ado.items)
        self.assertEqual(self.store.load(env["data"]["preview_id"])["state"], "applied")

    def test_criacao_sem_efeito(self):
        env = self.prep()
        self.ado.fault("POST", r"/workitems/\$Task$", "drop_before")
        res = self.apply(env)
        self.assertEqual((res["status"], res["data"]["outcome"]), ("failed", "not_applied"))
        self.assertEqual(self.apply(env)["reason_code"], "PREVIEW_CLOSED")

    def test_criacao_5xx_com_efeito(self):
        env = self.prep()
        self.ado.fault("POST", r"/workitems/\$Task$", "500_after")
        self.assertEqual(self.apply(env)["data"]["outcome"], "applied")

    def test_criacao_ambigua_com_dois_candidatos(self):
        env = self.prep()
        self.ado.add_item("Task", TASK["System.Title"])
        self.ado.fault("POST", r"/workitems/\$Task$", "drop_after")
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"]), ("blocked", "AMBIGUOUS"))
        self.assertEqual(len(res["data"]["candidates"]), 2)
        self.assertEqual(self.apply(env)["reason_code"], "NEEDS_RECONCILE")

    def test_timeout_do_http(self):
        self.build(TimeoutAfter(self.http, r"/workitems/\$Task$"))
        env = self.prep()
        res = self.apply(env)
        self.assertEqual(res["data"]["outcome"], "applied", res)
        self.assertEqual(res["diagnostic"]["reason_code"], "TIMEOUT")

    def test_update_e_comentario(self):
        t = self.ado.add_item("Task", "T")
        env = self.keep(self.w.prepare_update("OrgA", t, state_intent="iniciar"))
        self.ado.fault("PATCH", r"/workitems/\d+$", "drop_after")
        self.assertEqual(self.apply(env)["data"]["outcome"], "applied")
        env = self.keep(self.w.prepare_update("OrgA", t, state_intent="concluir"))
        self.ado.fault("PATCH", r"/workitems/\d+$", "drop_before")
        self.assertEqual(self.apply(env)["data"]["outcome"], "not_applied")
        env = self.keep(self.w.prepare_comment("OrgA", t, html='<p>ver <a href="https://e.com">x</a></p>'))
        self.ado.fault("POST", r"/comments$", "drop_after")
        res = self.apply(env)
        self.assertEqual(res["data"]["outcome"], "applied", res)
        self.assertEqual(res["data"]["comment_id"], self.ado.comments[t][0]["id"])

    def test_login_vencido_no_apply_pede_setup(self):
        from adoplug.auth import AuthError
        env = self.prep()
        auth = self.http.auth

        def boom(org):
            raise AuthError("AZ_NOT_LOGGED_IN", "Rode az login.")
        auth.get_credential = boom
        res = self.apply(env)
        self.assertEqual((res["status"], res["reason_code"]), ("needs_setup", "AZ_NOT_LOGGED_IN"))
        self.assertEqual(self.store.load(env["data"]["preview_id"])["state"], "prepared")
        self.assertEqual(self.ado.writes(), [])
        del auth.get_credential
        self.assertEqual(self.apply(env)["status"], "ok")

    def test_reconcile_direto(self):
        env = self.prep()
        self.assertEqual(self.w.reconcile(env["data"]["preview_id"])["reason_code"], "NOTHING_TO_RECONCILE")
        self.assertEqual(self.w.reconcile("x")["status"], "blocked")


class EstaticoTest(unittest.TestCase):
    def test_sem_bypass_rules(self):
        for p in (PLUGIN / "server" / "adoplug").glob("*.py"):
            self.assertIsNone(re.search(r"""["']bypassRules["']""", p.read_text(encoding="utf-8")), p.name)


if __name__ == "__main__":
    unittest.main()

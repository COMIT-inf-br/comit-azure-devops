"""Leituras de work item e adaptador discovery ↔ http, contra o ADO falso local."""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "server"))
sys.path.insert(0, str(PLUGIN / "tests" / "fixtures" / "workitems"))
sys.path.insert(0, str(PLUGIN / "tests" / "fakes"))
import fake_ado  # noqa: E402
import fakelib  # noqa: E402
from adoplug import discovery  # noqa: E402
from adoplug import http as adohttp  # noqa: E402
from adoplug.adoapi import AdoApiError, DiscoveryClient, canonical_url, split_url  # noqa: E402
from adoplug.workitems import (NeedsDecision, WorkItemsApi, merge_tags, state_for_intent,  # noqa: E402
                               wiql_literal)

PROJ, PROJ2 = fake_ado.PROJ, fake_ado.PROJ2


class Base(unittest.TestCase):
    def setUp(self):
        self.ado = fake_ado.FakeAdo()
        self.addCleanup(self.ado.close)
        self.tok = fakelib.sentinel()
        self.http = adohttp.AdoHttp(fake_ado.FakeAuth(self.tok), ["OrgA"], scheme_host=self.ado.base)
        self.prof = fake_ado.profile()
        self.api = WorkItemsApi(self.http, lambda org: (self.prof, "embedded"))


class AdaptadorTest(Base):
    def test_split_url(self):
        self.assertEqual(split_url("https://dev.azure.com/OrgA/_apis/x?api-version=7.1&$expand=y"),
                         ("OrgA", "/_apis/x", {"api-version": "7.1", "$expand": "y"}))
        self.assertEqual(split_url("https://orga.visualstudio.com/DefaultCollection/" + PROJ + "/_apis/wit/workItems/5")[:2],
                         ("orga", "/" + PROJ + "/_apis/wit/workItems/5"))
        self.assertEqual(canonical_url("https://orga.visualstudio.com/_apis/wit/workItems/5"),
                         "https://dev.azure.com/orga/_apis/wit/workItems/5")
        for bad in ("http://dev.azure.com/OrgA/x", "https://evil.example/OrgA/x", "https://u:p@dev.azure.com/OrgA/x",
                    "https://dev.azure.com:444/OrgA/x", "https://a.b.visualstudio.com/x"):
            with self.subTest(bad=bad), self.assertRaises(AdoApiError):
                split_url(bad)

    def test_discovery_por_get_json(self):
        c = DiscoveryClient(self.http)
        procs, names = discovery.list_processes(c, discovery.org_host("OrgA"))
        self.assertEqual(names, {fake_ado.PROC: "Proc"})
        r = self.ado.requests[-1]
        self.assertEqual(r["path"], "/OrgA/_apis/work/processes")
        self.assertEqual(r["query"], {"$expand": "projects", "api-version": "7.1"})
        self.assertEqual(r["headers"]["authorization"], "Bearer " + self.tok)

    def test_discovery_erro_e_sem_post(self):
        c = DiscoveryClient(self.http)
        with self.assertRaises(AdoApiError) as cm:
            c.get_json("https://dev.azure.com/OrgA/_apis/nada?api-version=7.1")
        self.assertEqual(cm.exception.reason_code, "NOT_FOUND")
        self.assertEqual(cm.exception.http_status, 404)
        with self.assertRaises(AdoApiError):
            c.post_json("https://dev.azure.com/OrgA/_apis/x", {})
        with self.assertRaises(AdoApiError) as cm:
            c.get_json("https://dev.azure.com/OutraOrg/_apis/x")
        self.assertEqual(cm.exception.reason_code, "ORG_NOT_ALLOWED")


class PurosTest(unittest.TestCase):
    STATES = [{"name": "Aberto", "category": "Proposed"}, {"name": "Em Andamento", "category": "InProgress"},
              {"name": "Suspenso", "category": "InProgress"}, {"name": "Concluído", "category": "Completed"}]

    def test_estado_por_categoria(self):
        self.assertEqual(state_for_intent(self.STATES, "concluir"), ("Concluído", "Completed"))
        self.assertEqual(state_for_intent(self.STATES, "REABRIR"), ("Aberto", "Proposed"))
        self.assertEqual(state_for_intent(self.STATES, "suspenso"), ("Suspenso", "InProgress"))
        self.assertEqual(state_for_intent(self.STATES, "concluido"), ("Concluído", "Completed"))
        with self.assertRaises(NeedsDecision) as cm:
            state_for_intent(self.STATES, "iniciar")
        self.assertEqual([o["name"] for o in cm.exception.options], ["Em Andamento", "Suspenso"])
        with self.assertRaises(AdoApiError) as cm:
            state_for_intent(self.STATES, "cancelar")
        self.assertEqual(cm.exception.reason_code, "STATE_NOT_AVAILABLE")
        with self.assertRaises(AdoApiError):
            state_for_intent(self.STATES, "Fechado")

    def test_tags(self):
        self.assertEqual(merge_tags("a; B", "b;c ; ;a"), "a; B; c")
        self.assertEqual(merge_tags(None, ["x", "y"]), "x; y")
        self.assertEqual(merge_tags("", ""), "")

    def test_literal_wiql(self):
        self.assertEqual(wiql_literal("d'água"), "'d''água'")
        with self.assertRaises(AdoApiError):
            wiql_literal("a\nb")


class LeituraTest(Base):
    def test_get_resumo(self):
        us = self.ado.add_item("User Story", "US")
        t = self.ado.add_item("Task", "T", state="Em Andamento")
        self.ado.link_parent(t, us)
        s = self.api.get("OrgA", t)
        self.assertEqual((s["id"], s["type"], s["state"], s["state_category"]), (t, "Task", "Em Andamento", "InProgress"))
        self.assertEqual(s["project_id"], PROJ)
        self.assertEqual(s["parent_id"], us)
        self.assertEqual(s["web_url"], f"https://dev.azure.com/OrgA/{PROJ}/_workitems/edit/{t}")
        self.assertEqual(s["fields"]["System.CreatedBy"], {"displayName": "Eu Mesmo", "uniqueName": fake_ado.ME_UNIQUE})
        r = self.ado.requests[0]
        self.assertEqual((r["path"], r["query"]), (f"/OrgA/_apis/wit/workitems/{t}", {"$expand": "relations", "api-version": "7.1"}))
        self.assertEqual(self.api.children("OrgA", us)[0]["id"], t)

    def test_id_invalido_nao_chama(self):
        for bad in ("1 OR 1", "-1", "0", "abc", True):
            with self.subTest(bad=bad), self.assertRaises(AdoApiError):
                self.api.get("OrgA", bad)
        self.assertEqual(self.ado.requests, [])

    def test_get_many_em_lotes(self):
        ids = [self.ado.add_item("Task", f"T{i}") for i in range(3)]
        out = self.api.get_many("OrgA", list(range(800, 1000)) + ids[::-1])
        self.assertEqual([o["id"] for o in out], ids[::-1])
        gets = [r for r in self.ado.requests if r["path"] == "/OrgA/_apis/wit/workitems"]
        self.assertEqual(len(gets), 2)
        self.assertEqual(len(gets[0]["query"]["ids"].split(",")), 200)

    def test_query_por_filtros(self):
        a = self.ado.add_item("Task", "Aberta", **{"System.AssignedTo": {"id": fake_ado.ME, "uniqueName": fake_ado.ME_UNIQUE}})
        self.ado.add_item("Task", "Concluida", state="Concluído", **{"System.AssignedTo": {"id": fake_ado.ME}})
        self.ado.add_item("Task", "De outro", **{"System.AssignedTo": {"id": fake_ado.ANA}})
        self.ado.add_item("Task", "Outro projeto", pid=PROJ2, **{"System.AssignedTo": {"id": fake_ado.ME}})
        r = self.api.query("OrgA", "Projeto Ágil", assigned_to_me=True, types=["Task"], state_category="open", top=5)
        self.assertEqual([i["id"] for i in r["items"]], [a])
        self.assertIn("[System.State] IN ('Aberto', 'Em Andamento')", r["wiql"])
        w = [x for x in self.ado.requests if x["path"].endswith("/wiql")][0]
        self.assertEqual(w["path"], f"/OrgA/{PROJ}/_apis/wit/wiql")
        self.assertEqual(w["query"], {"api-version": "7.1", "$top": "5"})

    def test_query_titulo_com_aspa_e_categoria_sem_perfil(self):
        self.prof = None
        self.api = WorkItemsApi(self.http, lambda org: (None, "absent"))
        i = self.ado.add_item("Bug", "d'água")
        q = self.api.build_wiql("OrgA", PROJ, state_category="Resolved", title="d'água")
        self.assertIn("[System.Title] = 'd''água'", q)
        self.assertIn("'Resolvido'", q)
        self.assertEqual(self.api.wiql_ids("OrgA", PROJ, self.api.build_wiql("OrgA", PROJ, title="d'água")), [i])
        with self.assertRaises(AdoApiError):
            self.api.build_wiql("OrgA", PROJ, state_category="Fechado")

    def test_comentarios_paginados_com_projeto(self):
        t = self.ado.add_item("Task", "T")
        for k in range(5):
            self.ado.comments.setdefault(t, []).append({"id": k, "text": f"c{k}", "createdDate": fake_ado.now_iso()})
        cs = self.api.comments("OrgA", t)
        self.assertEqual([c["text"] for c in cs], [f"c{k}" for k in range(5)])
        paths = {r["path"] for r in self.ado.requests if "comments" in r["path"]}
        self.assertEqual(paths, {f"/OrgA/{PROJ}/_apis/wit/workItems/{t}/comments"})
        self.assertTrue(all(r["query"]["api-version"] == "7.1-preview.4" for r in self.ado.requests if "comments" in r["path"]))

    def test_projeto_por_nome_com_acento(self):
        self.assertEqual(self.api.project("OrgA", "projeto ágil")["id"], PROJ)
        self.assertEqual(self.ado.requests, [])  # veio do perfil
        api = WorkItemsApi(self.http, lambda org: (None, "absent"))
        self.assertEqual(api.project("OrgA", "Projeto Ágil")["id"], PROJ)
        self.assertEqual(self.ado.requests[-1]["raw_path"].split("?")[0], "/OrgA/_apis/projects/Projeto%20%C3%81gil")

    def test_teams_iteracoes_whoami(self):
        self.assertEqual(self.api.list_teams("OrgA", PROJ), [{"id": fake_ado.TEAM, "name": "Time Ágil"}])
        cur = self.api.iterations("OrgA", PROJ, "time ágil")
        self.assertEqual([i["name"] for i in cur], ["Sprint 2"])
        fut = self.api.iterations("OrgA", PROJ, fake_ado.TEAM, "future")
        self.assertEqual([i["name"] for i in fut], ["Sprint 3"])
        self.assertEqual(self.api.whoami("OrgA"), {"displayName": "Eu Mesmo", "uniqueName": fake_ado.ME_UNIQUE, "id": fake_ado.ME})
        self.assertEqual(self.api.list_projects("OrgA")[0]["name"], "Projeto Beta")

    def test_identidade(self):
        both = self.api.resolve_identity("OrgA", "ana", PROJ, "Time Ágil")
        self.assertEqual({c["id"] for c in both}, {fake_ado.ANA, fake_ado.ANA2})
        one = self.api.resolve_identity("OrgA", "ana souza", PROJ, "Time Ágil")
        self.assertEqual([c["uniqueName"] for c in one], ["ana@example.com"])
        vs = self.api.resolve_identity("OrgA", "Ana Lima")
        self.assertEqual([c["id"] for c in vs], [fake_ado.ANA2])
        self.assertEqual(self.ado.requests[-1]["path"], "/OrgA/_apis/identities")


if __name__ == "__main__":
    unittest.main()

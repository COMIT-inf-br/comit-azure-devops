"""Backlog, sprint, mover, criar sprint, excluir, virada e lote de comentários sobre o ADO falso."""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes", "tests/unit"):
    sys.path.insert(0, str(PLUGIN / sub))
import fake_ado  # noqa: E402
from adoplug import planning  # noqa: E402
from test_tools import Base  # noqa: E402

PROJ = fake_ado.PROJ
ROOT = "Projeto Ágil"
S2, S3, S4 = ROOT + "\\Sprint 2", ROOT + "\\Sprint 3", ROOT + "\\Sprint 4"
T = {"project": PROJ, "team": "Time Ágil"}
ANA = {"displayName": "Ana Souza", "uniqueName": "ana@example.com", "id": fake_ado.ANA}
ME = {"displayName": "Eu Mesmo", "uniqueName": fake_ado.ME_UNIQUE, "id": fake_ado.ME}


class PathsTest(unittest.TestCase):
    def test_conversores(self):
        self.assertEqual(planning.path_segments("Proj\\Sprint 21"), ["Sprint 21"])
        self.assertEqual(planning.path_segments("Proj"), [])
        self.assertEqual(planning.node_to_item_path("\\Proj\\Iteration\\Sprint 21"), "Proj\\Sprint 21")
        self.assertEqual(planning.node_to_item_path("\\Proj\\Iteration"), "Proj")
        self.assertEqual(planning.node_route(PROJ, ["A b", "Sprint 1"]),
                         f"/{PROJ}/_apis/wit/classificationnodes/Iterations/A%20b/Sprint%201")
        self.assertEqual(planning.parent_path("Proj\\Sprint 2"), "Proj")

    def test_sugestao(self):
        its = [{"name": "Sprint 9", "path": "P\\Sprint 9", "start": "2026-10-06T00:00:00Z", "finish": "2026-10-19T00:00:00Z"},
               {"name": "Sprint 10", "path": "P\\Sprint 10", "start": "2026-10-20T00:00:00Z",
                "finish": "2026-11-02T00:00:00Z"}]
        s = planning.suggest_next(its)
        self.assertEqual((s["name"], s["start"], s["finish"], s["parent_path"]),
                         ("Sprint 11", "2026-11-03", "2026-11-16", "P"))
        self.assertIsNone(planning.suggest_next([{"name": "X", "path": "P\\X"}]))


class ReadsTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.us1 = a.add_item("User Story", "Backlog A", **{"Microsoft.VSTS.Common.StackRank": 2.0})
        self.us2 = a.add_item("User Story", "Backlog B", **{"Microsoft.VSTS.Common.StackRank": 1.0,
                                                           "System.AreaPath": ROOT + "\\Dados\\Sub"})
        a.add_item("User Story", "Outra área", **{"System.AreaPath": ROOT + "\\Outra"})
        a.add_item("User Story", "Concluída", state="Concluído")
        a.add_item("Task", "Task solta")
        a.add_item("User Story", "Na sprint", **{"System.IterationPath": S2})

    def test_backlog(self):
        d = self.e.call("ado_backlog", **T)["data"]
        self.assertEqual(d["backlog_iteration"], ROOT)
        self.assertEqual(d["types"], ["User Story"])
        self.assertEqual({i["title"] for i in d["items"]}, {"Backlog A", "Backlog B"})
        self.assertEqual(d["order_by"], "Microsoft.VSTS.Common.StackRank")
        wiql = [r for r in self.e.ado.requests if r["path"].endswith("/wiql")][-1]["body"].decode()
        self.assertIn("UNDER", wiql)
        self.assertIn("[System.IterationPath] = 'Projeto Ágil'", wiql)

    def test_backlog_sem_stackrank_cai_para_outro_campo(self):
        self.e.ado.no_stack_rank = True
        d = self.e.call("ado_backlog", types=["User Story", "Task"], **T)["data"]
        self.assertEqual(d["order_by"], "Microsoft.VSTS.Common.BacklogPriority")
        self.assertIn("Task solta", {i["title"] for i in d["items"]})

    def test_backlog_usa_team_padrao(self):
        self.assertEqual(self.e.call("ado_backlog", project=PROJ)["reason_code"], "TEAM_REQUIRED")

    def test_sprint_items_totais(self):
        a = self.e.ado
        us = a.add_item("User Story", "US", **{"System.IterationPath": S2})
        t1 = a.add_item("Task", "T1", **{"System.IterationPath": S2, "System.AssignedTo": ANA,
                                         "Microsoft.VSTS.Scheduling.RemainingWork": 3.0,
                                         "Microsoft.VSTS.Scheduling.OriginalEstimate": 5.0})
        t2 = a.add_item("Task", "T2", state="Concluído", **{"System.IterationPath": S2, "System.AssignedTo": ANA,
                                                            "Microsoft.VSTS.Scheduling.OriginalEstimate": 2.0})
        a.add_item("Task", "T3", **{"System.IterationPath": S2, "Microsoft.VSTS.Scheduling.RemainingWork": 1.5})
        a.link_parent(t1, us)
        a.link_parent(t2, us)
        d = self.e.call("ado_sprint_items", **T)["data"]
        self.assertEqual(d["iteration"]["name"], "Sprint 2")
        self.assertEqual(d["count"], 5)
        by = {p["assigned_to"].get("uniqueName") or p["assigned_to"]["displayName"]: p for p in d["totals_by_person"]}
        self.assertEqual((by["ana@example.com"]["remaining"], by["ana@example.com"]["original"],
                          by["ana@example.com"]["open"]), (3.0, 7.0, 1))
        self.assertEqual(by[planning.NO_OWNER]["remaining"], 1.5)
        self.assertEqual(d["totals"]["remaining"], 4.5)
        self.assertEqual({i["id"]: i["parent_id"] for i in d["items"]}[t1], us)
        other = self.e.call("ado_sprint_items", iteration="Sprint 3", **T)["data"]
        self.assertEqual(other["count"], 0)
        self.assertEqual(self.e.call("ado_sprint_items", iteration="Sprint 99", **T)["reason_code"],
                         "ITERATION_NOT_FOUND")


class MoveTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.us = a.add_item("User Story", "US backlog")
        self.t_open = a.add_item("Task", "Aberta")
        self.t_done = a.add_item("Task", "Feita", state="Concluído")
        a.link_parent(self.t_open, self.us)
        a.link_parent(self.t_done, self.us)

    def test_move_com_filhas(self):
        p = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 3", **T)
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        self.assertEqual([m["id"] for m in d["moving"]], [self.us, self.t_open])
        self.assertEqual(d["moving"][0]["from"], ROOT)
        self.assertEqual(d["moving"][0]["to"], S3)
        self.assertEqual([s["id"] for s in d["staying"]], [self.t_done])
        self.assertEqual(d["apply_args"]["iteration"], S3)
        self.assertEqual(d["apply_args"]["count"], 2)
        self.assertEqual(self.e.ado.writes(), [])
        vo = [r for r in self.e.ado.requests if r["query"].get("validateOnly") == "true"]
        self.assertEqual(len(vo), 2)
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(r["data"]["readback"]["mismatches"], [])
        f = self.e.ado.items
        self.assertEqual((f[self.us]["fields"]["System.IterationPath"], f[self.t_open]["fields"]["System.IterationPath"],
                          f[self.t_done]["fields"]["System.IterationPath"]), (S3, S3, ROOT))
        for w in self.e.ado.writes():
            self.assertEqual(w["method"], "PATCH")
            self.assertIn(b'"op": "test"', w["body"])
            self.assertNotIn("bypassRules", w["query"])
        self.assertEqual(self.e.apply(p)["reason_code"], "ALREADY_APPLIED")

    def test_sem_filhas(self):
        d = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration=S3, include_children=False, **T)["data"]
        self.assertEqual([m["id"] for m in d["moving"]], [self.us])

    def test_claims_errados_recusam(self):
        p = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 3", **T)
        self.assertEqual(self.e.apply(p, iteration=S2)["reason_code"], "PREVIEW_MISMATCH")
        self.assertEqual(self.e.apply(p, count=1)["reason_code"], "PREVIEW_MISMATCH")
        self.assertEqual(self.e.ado.writes(), [])

    def test_parcial_por_revisao_e_403(self):
        p = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 3", **T)
        self.e.ado.items[self.us]["rev"] += 1  # alguém mudou a US depois da prévia
        self.e.ado.fault("PATCH", rf"/workitems/{self.t_open}$", "403_before")
        r = self.e.apply(p)
        self.assertEqual(r["status"], "failed")
        codes = {s["id"]: s.get("code") for s in r["data"]["steps"]}
        self.assertEqual(codes, {self.us: "REVISION_CHANGED", self.t_open: "FORBIDDEN"})
        self.assertIn("permissão", next(s["message"] for s in r["data"]["steps"] if s["id"] == self.t_open))
        self.assertEqual(self.e.apply(p)["reason_code"], "PREVIEW_CLOSED")

    def test_ambiguo_reconcilia_sem_duplicar(self):
        p = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 3", **T)
        self.e.ado.fault("PATCH", rf"/workitems/{self.us}$", "500_after")
        r = self.e.apply(p)
        self.assertEqual(r["data"]["after"], "ambiguous_write")
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(len(self.e.ado.writes()), 2)

    def test_pendente_por_rede_e_retry(self):
        p = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 3", **T)
        self.e.ado.fault("PATCH", rf"/workitems/{self.t_open}$", "drop_before")
        r = self.e.apply(p)
        self.assertIn(r["reason_code"], ("APPLIED", "PARTIAL", "AMBIGUOUS"), r)
        if r["reason_code"] != "APPLIED":
            r = self.e.call("ado_reconcile", preview_id=p["data"]["preview_id"])
            if r["reason_code"] == "PARTIAL":
                r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        patched = [w for w in self.e.ado.writes() if w["path"].endswith(f"/{self.us}")]
        self.assertEqual(len(patched), 1)

    def test_para_backlog_e_nada_a_mover(self):
        self.e.ado.items[self.us]["fields"]["System.IterationPath"] = S2
        d = self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="backlog", include_children=False,
                        **T)["data"]
        self.assertEqual(d["to"]["path"], ROOT)
        r = self.e.call("ado_prepare_move_to_sprint", ids=[self.t_open], iteration="backlog", **T)
        self.assertEqual(r["reason_code"], "NO_CHANGES")
        self.assertEqual(self.e.call("ado_prepare_move_to_sprint", ids=[self.us], iteration="Sprint 9",
                                     **T)["reason_code"], "ITERATION_NOT_FOUND")


class CreateSprintTest(Base):
    def sprint(self, **a):
        from adoplug import tools
        env = tools.call_tool(self.e.ctx, "ado_prepare_create_sprint", {**T, **a})
        self.e.envs.append(env)
        return env

    def test_sugere_e_associa_no_existente(self):
        # Sprint 4 existe no projeto, fora do time: o plano só associa.
        p = self.e.call("ado_prepare_create_sprint", **T)
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        sp = p["data"]["sprint"]
        self.assertEqual((sp["name"], sp["action"], sp["path"], sp["start"]), ("Sprint 4", "associate", S4, "2026-11-30"))
        self.assertEqual(p["data"]["apply_args"]["title"], "Sprint 4")
        self.assertEqual(self.e.ado.writes(), [])
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertIn("5a5a5a5a-0000-4000-8000-000000000004", self.e.ado.team_iters)

    def test_cria_e_associa(self):
        self.e.ado.team_iters.append("5a5a5a5a-0000-4000-8000-000000000004")
        p = self.e.call("ado_prepare_create_sprint", **T)
        sp = p["data"]["sprint"]
        self.assertEqual((sp["name"], sp["action"], sp["start"], sp["finish"]),
                         ("Sprint 5", "create", "2026-12-12", "2026-12-23"))
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        node = next(n for n in self.e.ado.nodes if n["name"] == "Sprint 5")
        self.assertEqual(node["start"], "2026-12-12T00:00:00Z")
        self.assertIn(node["identifier"], self.e.ado.team_iters)

    def test_nome_e_datas_dados(self):
        p = self.sprint(name="Sprint Extra", start_date="2027-01-04",
                        finish_date="2027-01-15")
        self.assertEqual(p["data"]["sprint"]["action"], "create")
        self.assertEqual(self.sprint(name="X", start_date="2027-01-15",
                                     finish_date="2027-01-04")["reason_code"], "DATE_INVALID")
        self.assertEqual(self.sprint(name="Sprint 2")["reason_code"], "NO_CHANGES")

    def test_403_na_criacao_impede_associacao(self):
        p = self.sprint(name="Sprint Extra", start_date="2027-01-04",
                        finish_date="2027-01-15")
        self.e.ado.fault("POST", r"/classificationnodes/Iterations$", "403_before")
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "FORBIDDEN", r)
        self.assertEqual([s["state"] for s in r["data"]["steps"]], ["failed", "skipped"])
        self.assertIn("permissão", r["data"]["steps"][0]["message"])

    def test_criacao_ambigua_reconcilia(self):
        p = self.sprint(name="Sprint Extra", start_date="2027-01-04",
                        finish_date="2027-01-15")
        self.e.ado.fault("POST", r"/classificationnodes/Iterations$", "drop_after")
        r = self.e.apply(p)
        # a releitura acha o nó criado e o lote segue para a associação, sem criar de novo
        self.assertEqual((r["reason_code"], r["data"]["after"]), ("APPLIED", "ambiguous_write"), r)
        self.assertEqual(sum(1 for n in self.e.ado.nodes if n["name"] == "Sprint Extra"), 1)


class DeleteTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.us = a.add_item("User Story", "Apagar")
        self.t1 = a.add_item("Task", "Filha")
        a.link_parent(self.t1, self.us)
        self.solo = a.add_item("Task", "Sozinha")

    def test_recusa_sem_include_children(self):
        r = self.e.call("ado_prepare_delete", id=self.us)
        self.assertEqual((r["status"], r["reason_code"]), ("needs_decision", "CHILDREN_NOT_INCLUDED"))
        self.assertEqual([o["id"] for o in r["data"]["options"]], [self.t1])
        r = self.e.call("ado_prepare_delete", id=self.us, include_children=False)
        self.assertEqual(r["reason_code"], "CHILDREN_NOT_INCLUDED")

    def test_exclui_arvore_para_lixeira(self):
        p = self.e.call("ado_prepare_delete", id=self.us, include_children=True)
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        self.assertEqual((d["apply_args"]["operation"], d["apply_args"]["target_id"], d["apply_args"]["title"]),
                         ("delete", self.us, "Apagar"))
        self.assertTrue(d["recoverable"])
        self.assertEqual(self.e.ado.writes(), [])
        self.assertEqual(self.e.apply(p, title="Outro")["reason_code"], "PREVIEW_MISMATCH")
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(set(self.e.ado.recycle), {self.us, self.t1})
        dels = [w for w in self.e.ado.writes() if w["method"] == "DELETE"]
        self.assertEqual([w["path"].rsplit("/", 1)[1] for w in dels], [str(self.t1), str(self.us)])
        self.assertTrue(all("destroy" not in w["query"] for w in dels))

    def test_item_mudou_nao_exclui(self):
        p = self.e.call("ado_prepare_delete", id=self.solo)
        self.e.ado.items[self.solo]["rev"] += 1
        r = self.e.apply(p)
        self.assertEqual(r["data"]["steps"][0]["code"], "REVISION_CHANGED")
        self.assertIn(self.solo, self.e.ado.items)


class RolloverTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.us = a.add_item("User Story", "US", **{"System.IterationPath": S2})
        self.t_open = a.add_item("Task", "Aberta", **{"System.IterationPath": S2, "System.AssignedTo": ANA,
                                                     "Microsoft.VSTS.Scheduling.RemainingWork": 4.0})
        self.t_done = a.add_item("Task", "Feita", state="Concluído", **{"System.IterationPath": S2})
        a.link_parent(self.t_open, self.us)
        a.link_parent(self.t_done, self.us)

    def test_para_proxima_existente(self):
        p = self.e.call("ado_prepare_sprint_rollover", **T)
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        self.assertEqual((d["from"]["name"], d["to"]["name"], d["to"]["action"]), ("Sprint 2", "Sprint 3", "exists"))
        self.assertEqual({g["id"] for g in d["going"]}, {self.us, self.t_open})
        self.assertEqual([s["id"] for s in d["staying"]], [self.t_done])
        self.assertEqual(d["totals"], {"going": 2, "staying": 1, "remaining_going": 4.0})
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(self.e.ado.items[self.t_done]["fields"]["System.IterationPath"], S2)
        self.assertEqual(self.e.ado.items[self.t_open]["fields"]["System.IterationPath"], S3)

    def test_cria_a_proxima(self):
        p = self.e.call("ado_prepare_sprint_rollover", from_iteration="Sprint 3", **T)
        # Sprint 3 vazia: só abre a próxima (Sprint 4 existe fora do time)
        self.assertEqual((p["data"]["steps"], p["data"]["apply_args"]["count"]), (["associate"], 0))
        for i in (self.us, self.t_open, self.t_done):
            self.e.ado.items[i]["fields"]["System.IterationPath"] = S3
        p = self.e.call("ado_prepare_sprint_rollover", from_iteration="Sprint 3", **T)
        d = p["data"]
        self.assertEqual((d["to"]["name"], d["to"]["action"]), ("Sprint 4", "associate"))
        self.assertEqual(d["steps"], ["associate", "set_iteration", "set_iteration"])
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(self.e.ado.items[self.t_open]["fields"]["System.IterationPath"], S4)

    def test_sem_proxima_nem_criacao(self):
        for i in (self.us, self.t_open):
            self.e.ado.items[i]["fields"]["System.IterationPath"] = S3
        r = self.e.call("ado_prepare_sprint_rollover", from_iteration="Sprint 3", create_if_missing=False, **T)
        self.assertEqual(r["reason_code"], "NO_NEXT_SPRINT")


TRANSCRICAO = """[09:01] Carla Dias: Começando pela 1001, painel de vendas.
[09:01] Bruno Reis: Terminei a extração, falta publicar. Entrego quinta.
[09:02] Carla Dias: Próxima, revisar carga.
[09:02] Ana Souza: Travada esperando acesso ao banco. Ana, você libera?
[09:03] Carla Dias: Documentar regras.
[09:03] Davi Melo: Começo hoje."""


class CommentsBatchTest(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.ids = [a.add_item("User Story", "Painel de vendas"), a.add_item("Task", "Revisar carga"),
                    a.add_item("Task", "Documentar regras")]
        # Segmentação feita pela skill a partir de TRANSCRICAO (um comentário por item citado).
        self.items = [
            {"id": self.ids[0], "text": "Daily 13/11 — Bruno Reis: extração concluída; próximo passo: publicar; "
                                        "data prometida: quinta."},
            {"id": self.ids[1], "text": "Daily 13/11 — impedimento: aguardando acesso ao banco.",
             "mentions": ["Ana Souza"]},
            {"id": self.ids[2], "text": "Daily 13/11 — Davi Melo: começa hoje."},
        ]

    def test_lote_publica_um_por_um(self):
        self.assertIn("1001", TRANSCRICAO)
        p = self.e.call("ado_prepare_comments_batch", items=self.items, source_label="Daily 13/11", **T)
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        self.assertEqual(d["count"], 3)
        self.assertEqual([i["title"] for i in d["items"]], ["Painel de vendas", "Revisar carga", "Documentar regras"])
        self.assertIn("data-vss-mention", d["items"][1]["html"])
        self.assertEqual(self.e.ado.writes(), [])
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual([len(self.e.ado.comments.get(i, [])) for i in self.ids], [1, 1, 1])

    def test_retry_nao_duplica(self):
        p = self.e.call("ado_prepare_comments_batch", items=self.items, **T)
        self.e.ado.fault("POST", rf"/workItems/{self.ids[1]}/comments$", "drop_after")
        r = self.e.apply(p)
        self.assertEqual(r["data"]["after"], "ambiguous_write")
        self.assertEqual(r["reason_code"], "APPLIED", r)  # o 2º foi achado na releitura; o 3º seguiu
        self.assertEqual(self.e.apply(p)["reason_code"], "ALREADY_APPLIED")
        self.assertEqual([len(self.e.ado.comments.get(i, [])) for i in self.ids], [1, 1, 1])

    def test_pendente_reenvia_so_o_que_falta(self):
        p = self.e.call("ado_prepare_comments_batch", items=self.items, **T)
        self.e.ado.fault("POST", rf"/workItems/{self.ids[1]}/comments$", "drop_before")
        r = self.e.apply(p)
        if r["reason_code"] == "AMBIGUOUS":
            r = self.e.call("ado_reconcile", preview_id=p["data"]["preview_id"])
        self.assertEqual(r["reason_code"], "PARTIAL", r)
        self.assertEqual([len(self.e.ado.comments.get(i, [])) for i in self.ids], [1, 0, 0])
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual([len(self.e.ado.comments.get(i, [])) for i in self.ids], [1, 1, 1])

    def test_mencao_ambigua_diz_o_item(self):
        items = [dict(self.items[0]), {"id": self.ids[1], "text": "x", "mentions": ["Ana"]}]
        r = self.e.call("ado_prepare_comments_batch", items=items, **T)
        self.assertEqual(r["reason_code"], "IDENTITY_AMBIGUOUS")
        self.assertIn(f"Item {self.ids[1]}", r["next_action"])
        self.assertTrue(all(o.get("item_id") == self.ids[1] for o in r["data"]["options"]))

    def test_item_invalido_nao_gera_previa(self):
        r = self.e.call("ado_prepare_comments_batch", items=[self.items[0], {"id": 999999, "text": "x"}], **T)
        self.assertEqual(r["reason_code"], "ITEMS_INVALID")
        self.assertEqual(r["data"]["errors"][0]["position"], 2)


if __name__ == "__main__":
    unittest.main()

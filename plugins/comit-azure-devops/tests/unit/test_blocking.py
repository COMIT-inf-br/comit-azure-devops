"""Impedimento (ado_prepare_block e ado_prepare_unblock) sobre o ADO falso, pelo padrão do perfil."""
import datetime as dt
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes", "tests/unit"):
    sys.path.insert(0, str(PLUGIN / sub))
import fake_ado  # noqa: E402
from adoplug.blocking import Blocking  # noqa: E402
from test_tools import Base  # noqa: E402

ANA = {"displayName": "Ana Souza", "uniqueName": "ana@example.com", "id": fake_ado.ANA}
# 13/11/2026 12:00 no fuso da máquina: "hoje" fixo para os padrões de data.
NOW = dt.datetime(2026, 11, 13, 12, 0).timestamp()
REASON = "Acesso negado no início da task; chamado de infra aberto"
WAIT = "resolução do chamado de acesso"


class Setup(Base):
    def setUp(self):
        super().setUp()
        a = self.e.ado
        self.e.ctx.writes.clock = lambda: NOW
        self.us = a.add_item("User Story", "História", state="Em Andamento", **{"System.AssignedTo": ANA})
        self.task = a.add_item("Task", "Task", state="Em Andamento")
        a.link_parent(self.task, self.us)

    def block(self, **kw):
        args = {"id": self.task, "reason": REASON, "waiting_for": WAIT, "kind": "infra"}
        args.update(kw)
        args = {k: v for k, v in args.items() if v is not None}
        return self.e.call("ado_prepare_block", **args)


class BlockTest(Setup):
    def test_padroes_e_comentario(self):
        p = self.block()
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        v = d["values"]
        self.assertEqual(v["responsible"], {"displayName": "Ana Souza", "uniqueName": "ana@example.com",
                                            "source": "parent_assigned_to"})
        self.assertEqual((v["date"], v["forecast"], v["reason"]), ("13/11/2026", "15/11/2026", REASON))
        self.assertEqual(sorted(d["defaults_used"]), ["date", "forecast", "responsible"])
        self.assertEqual(d["comment"]["text"], f"Impedimento de infraestrutura: {REASON}. Aguardando: {WAIT} — "
                                               "responsável: @Ana Souza. Previsão: 15/11.")
        self.assertIn(f"data-vss-mention=\"version:2.0,{fake_ado.ANA}\"", d["comment"]["html"])
        self.assertEqual((d["validate_only"], d["state"], d["state_unchanged"]), ("accepted", "Em Andamento", True))
        self.assertEqual(self.e.ado.writes(), [])
        rec = self.e.ctx.store.load(d["preview_id"])
        kinds = [s["kind"] for s in rec["plan"]["steps"]]
        self.assertEqual(kinds, ["set_fields", "comment"])  # campos antes do comentário obrigatório
        patch = rec["plan"]["steps"][0]["patch"]
        self.assertEqual(patch[0], {"op": "test", "path": "/rev", "value": 1})
        self.assertNotIn("/fields/System.State", [o["path"] for o in patch])

    def test_apply_mantem_estado_e_grava_tudo(self):
        p = self.block()
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(r["data"]["readback"]["mismatches"], [])
        f = self.e.ado.items[self.task]["fields"]
        self.assertEqual(f["System.State"], "Em Andamento")
        self.assertEqual(f[fake_ado.BLOCKED], "Yes")
        self.assertEqual(f[fake_ado.BLOCK_WHO], "ana@example.com")
        self.assertEqual(f[fake_ado.BLOCK_WHY], REASON)
        self.assertEqual(dt.datetime.fromisoformat(f[fake_ado.BLOCK_DAY]).date(), dt.date(2026, 11, 13))
        self.assertEqual(dt.datetime.fromisoformat(f[fake_ado.BLOCK_ETA]).date(), dt.date(2026, 11, 15))
        comments = self.e.ado.comments[self.task]
        self.assertEqual(len(comments), 1)
        self.assertIn("data-vss-mention", comments[0]["text"])
        self.assertEqual(self.e.apply(p)["reason_code"], "ALREADY_APPLIED")

    def test_comentario_nao_sai_se_o_item_mudou(self):
        p = self.block()
        self.e.ado.items[self.task]["rev"] += 1
        r = self.e.apply(p)
        self.assertEqual(r["data"]["outcome"], "failed", r)
        self.assertEqual([s["state"] for s in r["data"]["steps"]], ["failed", "skipped"])
        self.assertEqual(self.e.ado.comments.get(self.task, []), [])
        self.assertNotEqual(self.e.ado.items[self.task]["fields"].get(fake_ado.BLOCKED), "Yes")

    def test_comentario_pendente_reenvia_so_ele(self):
        p = self.block()
        self.e.ado.fault("POST", rf"/workItems/{self.task}/comments$", "drop_before")
        r = self.e.apply(p)
        if r["reason_code"] == "AMBIGUOUS":
            r = self.e.call("ado_reconcile", preview_id=p["data"]["preview_id"])
        self.assertEqual(r["reason_code"], "PARTIAL", r)
        self.assertEqual(self.e.ado.items[self.task]["fields"][fake_ado.BLOCKED], "Yes")
        patches = len([w for w in self.e.ado.writes() if w["method"] == "PATCH"])
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(len(self.e.ado.comments[self.task]), 1)
        self.assertEqual(len([w for w in self.e.ado.writes() if w["method"] == "PATCH"]), patches)

    def test_patch_ambiguo_reconcilia_pelos_campos(self):
        p = self.block()
        self.e.ado.fault("PATCH", rf"/workitems/{self.task}$", "drop_after")
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        self.assertEqual(r["data"]["after"], "ambiguous_write")
        self.assertEqual(len(self.e.ado.comments[self.task]), 1)

    def test_valores_informados(self):
        p = self.block(responsible="Ana Souza", date="2026-11-12", forecast="2026-11-20")
        d = p["data"]
        self.assertEqual(d["values"]["responsible"]["source"], "informed")
        self.assertEqual((d["values"]["date"], d["values"]["forecast"]), ("12/11/2026", "20/11/2026"))
        self.assertEqual(d["defaults_used"], [])
        self.assertIn("Previsão: 20/11.", d["comment"]["text"])

    def test_responsavel_ambiguo(self):
        r = self.block(responsible="Ana")
        self.assertEqual(r["reason_code"], "IDENTITY_AMBIGUOUS")
        self.assertEqual(len(r["data"]["options"]), 2)

    def test_sem_pai_pede_o_nome(self):
        solo = self.e.ado.add_item("Task", "Solta", state="Em Andamento")
        r = self.block(id=solo)
        self.assertEqual((r["status"], r["reason_code"]), ("needs_decision", "RESPONSIBLE_REQUIRED"))

    def test_pai_sem_dono_pede_o_nome(self):
        self.e.ado.items[self.us]["fields"].pop("System.AssignedTo")
        r = self.block()
        self.assertEqual((r["status"], r["reason_code"]), ("needs_decision", "RESPONSIBLE_REQUIRED"))
        self.assertEqual(r["data"]["options"][0]["parent_id"], self.us)

    def test_funcional_e_sem_mencao(self):
        d = self.block(kind="funcional", waiting_for=None)["data"]
        self.assertEqual(d["comment"]["text"], "Impedimento funcional, aguardando resolver com @Ana Souza.")
        d = self.block(kind="funcional", mention_responsible=False)["data"]
        self.assertEqual(d["comment"]["text"], "Impedimento funcional, aguardando resolver com Ana Souza.")
        self.assertNotIn("data-vss-mention", d["comment"]["html"])

    def test_esperado_obrigatorio_quando_o_modelo_usa(self):
        r = self.block(waiting_for="  ")
        self.assertEqual(r["reason_code"], "ESPERADO_REQUIRED")

    def test_tipo_fora_da_politica(self):
        r = self.block(kind="cafe")
        self.assertEqual(r["reason_code"], "KIND_INVALID")
        self.assertEqual({o["kind"] for o in r["data"]["options"]}, {"infra", "funcional"})

    def test_previsao_antes_da_data(self):
        self.assertEqual(self.block(date="2026-11-13", forecast="2026-11-10")["reason_code"], "DATE_INVALID")

    def test_tipo_sem_padrao(self):
        r = self.block(id=self.us)
        self.assertEqual((r["status"], r["reason_code"]), ("needs_setup", "BLOCKING_NOT_MAPPED"))
        self.assertEqual(self.e.ado.writes(), [])

    def test_validate_only_recusado(self):
        self.e.ado.fault("PATCH", rf"/workitems/{self.task}$", "400_before")
        r = self.block()
        self.assertEqual(r["reason_code"], "VALIDATION_FAILED")
        self.assertEqual(self.e.ado.writes(), [])


class UnblockTest(Setup):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.e.apply(self.block())["reason_code"], "APPLIED")

    def test_desimpede_mantem_historico(self):
        p = self.e.call("ado_prepare_unblock", id=self.task, resolution="Acesso liberado pelo chamado")
        self.assertEqual(p["reason_code"], "PREVIEW_READY", p)
        d = p["data"]
        self.assertEqual([c["field"] for c in d["changes"]], [fake_ado.BLOCKED])
        self.assertEqual(len(d["kept_as_history"]), 4)
        self.assertEqual(d["comment"]["text"], "Impedimento resolvido: Acesso liberado pelo chamado.")
        r = self.e.apply(p)
        self.assertEqual(r["reason_code"], "APPLIED", r)
        f = self.e.ado.items[self.task]["fields"]
        self.assertEqual((f[fake_ado.BLOCKED], f["System.State"]), ("No", "Em Andamento"))
        self.assertEqual(f[fake_ado.BLOCK_WHY], REASON)
        self.assertEqual(len(self.e.ado.comments[self.task]), 2)
        again = self.e.call("ado_prepare_unblock", id=self.task, resolution="x")
        self.assertEqual(again["reason_code"], "NOT_BLOCKED")

    def test_limpa_o_que_a_politica_manda(self):
        tp = self.e.ctx.api.type_profile(self.e.org, fake_ado.PROJ, "Task")
        tp["blocking"]["policy"]["clear_on_unblock"] = ["forecast"]
        orig = self.e.ctx.api.type_profile
        self.e.ctx.api.type_profile = lambda *a: tp if a[2] == "Task" else orig(*a)
        p = self.e.call("ado_prepare_unblock", id=self.task, resolution="Liberado")
        self.assertEqual([c["field"] for c in p["data"]["changes"]], [fake_ado.BLOCKED, fake_ado.BLOCK_ETA])
        self.assertEqual(self.e.apply(p)["reason_code"], "APPLIED")
        self.assertNotIn(fake_ado.BLOCK_ETA, self.e.ado.items[self.task]["fields"])

    def test_resolucao_obrigatoria(self):
        self.assertEqual(self.e.call("ado_prepare_unblock", id=self.task, resolution=" ")["reason_code"],
                         "RESOLUTION_REQUIRED")


class RenderTest(unittest.TestCase):
    def test_escapa_texto_e_monta_mencao(self):
        html = Blocking.render("Com @{responsavel}: {motivo} <b>", {"motivo": "a<b>&c"},
                               {"id": fake_ado.ANA, "displayName": "Ana"}, True)
        self.assertEqual(html, f'Com <a href="#" data-vss-mention="version:2.0,{fake_ado.ANA}">@Ana</a>: '
                               "a&lt;b&gt;&amp;c &lt;b&gt;")


if __name__ == "__main__":
    unittest.main()

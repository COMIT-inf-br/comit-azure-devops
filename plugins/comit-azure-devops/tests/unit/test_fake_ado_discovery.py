"""Rotas de descoberta do ADO falso: servidas pela fixture, só em GET, com dados sintéticos."""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
for sub in ("server", "tests/fixtures/workitems", "tests/fakes"):
    sys.path.insert(0, str(PLUGIN / sub))
import fake_ado  # noqa: E402
import ado_mcp  # noqa: E402
from adoplug import discovery, tools, profile_validate  # noqa: E402
from adoplug import http as adohttp  # noqa: E402
from adoplug.adoapi import DiscoveryClient  # noqa: E402

PROC, PROJ = fake_ado.PROC, fake_ado.PROJ


class FakeAdoDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.ado = fake_ado.FakeAdo("OrgA")
        self.addCleanup(self.ado.close)
        self.http = adohttp.AdoHttp(fake_ado.FakeAuth("token-sentinela"), ["OrgA"], scheme_host=self.ado.base)
        self.client = DiscoveryClient(self.http)
        self.profile = discovery.discover_org(self.client, "OrgA", project=PROJ, measured_at="2026-11-13")
        self.types = self.profile["processes"][PROC]["work_item_types"]

    def test_perfil_valido_com_os_mesmos_tipos_da_fixture(self):
        self.assertEqual(profile_validate.validate_profile(self.profile), [])
        self.assertEqual(sorted(self.types), ["Task", "User Story"])
        task = self.types["Task"]
        self.assertEqual(task["required_on_create"], ["Custom.Equipe", "Microsoft.VSTS.Common.Activity",
                                                      "System.AreaPath", "System.IterationPath", "System.Title"])
        self.assertEqual([s["name"] for s in task["states"]], ["Aberto", "Em Andamento", "Concluído", "Removido"])
        self.assertEqual(task["picklists"]["Microsoft.VSTS.Common.Priority"], [1, 2, 3, 4])

    def test_so_get(self):
        self.assertEqual({r["method"] for r in self.ado.requests}, {"GET"})
        self.assertTrue(any(r["path"].endswith("/rules") for r in self.ado.requests))

    def test_rota_legada_de_processos_continua(self):
        # list_projects e outros testes dependem da lista própria do fake, com o processo "Proc"
        names = discovery.list_processes(self.client, discovery.org_host("OrgA"))[1]
        self.assertEqual(names, {PROC: "Proc"})

    def test_rota_sem_fixture_continua_404(self):
        r = self.http.get("OrgA", "/_apis/wit/rota-inexistente", query={"api-version": "7.1"})
        self.assertEqual(r.status, 404)


class MockOrgNovaTest(unittest.TestCase):
    """O caminho do eval "org nova": setup, descoberta e gravação no contexto --mock unconfigured."""

    def test_org_nova_ate_o_perfil(self):
        ctx = ado_mcp.build_mock_context("unconfigured")
        call = lambda name, **a: tools.call_tool(ctx, name, a)  # noqa: E731
        st = call("ado_status")
        self.assertEqual((st["status"], st["data"]["pending_setup"][0]["code"]), ("needs_setup", "NO_ORG"))
        self.assertEqual(call("ado_setup_org", org="OrgA")["reason_code"], "CONFIRM_REQUIRED")
        self.assertEqual(call("ado_setup_org", org="OrgA", confirm=True)["reason_code"], "ORG_SAVED")
        # o az falso do mock já tem login no tenant: resta o perfil
        self.assertEqual(call("ado_status")["data"]["pending_setup"][0]["code"], "PROFILE_ABSENT")
        found = call("ado_discover_org", org="OrgA", project="Projeto Ágil")
        self.assertEqual(found["status"], "ok")
        self.assertNotIn("diff", found["data"])  # sem perfil atual não há diferença
        pid = found["data"]["proposal_id"]
        self.assertEqual(call("ado_profile_save", proposal_id=pid)["reason_code"], "CONFIRM_REQUIRED")
        self.assertEqual(call("ado_profile_save", proposal_id=pid, confirm=True)["reason_code"], "PROFILE_SAVED")
        self.assertEqual(call("ado_status")["status"], "ok")


if __name__ == "__main__":
    unittest.main()

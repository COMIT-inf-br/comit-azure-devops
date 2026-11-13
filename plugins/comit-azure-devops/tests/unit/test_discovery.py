import copy
import json
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "server"))
from adoplug import discovery, profile_validate  # noqa: E402

FIXTURE = PLUGIN / "tests" / "fixtures" / "discovery" / "org_exemplo.json"
PROC = "0a0a0a0a-0000-4000-8000-00000000000a"
PROC2 = "0e0e0e0e-0000-4000-8000-00000000000e"
PROJ = "0c0c0c0c-0000-4000-8000-00000000000c"
IDF = "Custom.1a1a1a1a-0000-4000-8000-00000000001a"
GROUP = "3c3c3c3c-0000-4000-8000-00000000003c"


class FakeClient:
    """Responde GET pelas fixtures e grava as URLs. Qualquer POST falha o teste."""

    def __init__(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.host = data["host"]
        self.responses = data["responses"]
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        assert url.startswith(self.host + "/"), url
        path = url[len(self.host):]
        if path not in self.responses:
            raise KeyError(f"sem fixture: {path}")
        return copy.deepcopy(self.responses[path])

    def post_json(self, url, body):
        raise AssertionError("a descoberta não pode fazer POST")


class DiscoverOrgTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.profile = discovery.discover_org(self.client, "ORG-EXEMPLO", project=PROJ, measured_at="2026-11-13")
        self.types = self.profile["processes"][PROC]["work_item_types"]

    def test_valid_profile(self):
        self.assertEqual(profile_validate.validate_profile(self.profile), [])
        self.assertEqual(self.profile["source"], "discovered")
        self.assertEqual(self.profile["auth"], {"preferred": "az", "tenant": "discover"})

    def test_projects_and_processes(self):
        self.assertEqual(self.profile["projects"], [
            {"id": "0d0d0d0d-0000-4000-8000-00000000000d", "name": "Projeto Beta", "process": PROC},
            {"id": "0f0f0f0f-0000-4000-8000-00000000000f", "name": "Projeto Gama", "process": PROC2},
            {"id": PROJ, "name": "Projeto Ágil", "process": PROC},
        ])
        self.assertEqual(sorted(self.profile["processes"]), [PROC, PROC2])  # sem projeto = fora
        self.assertEqual(self.profile["processes"][PROC]["parent"], "Agile")
        self.assertEqual(self.profile["processes"][PROC2]["work_item_types"], "discover")

    def test_types_filter(self):
        # Desativado, oculto e fora das categorias de backlog ficam de fora.
        self.assertEqual(sorted(self.types), ["Task", "User Story"])

    def test_task(self):
        t = self.types["Task"]
        self.assertEqual(t["reference_name"], "Exemplo.Task")
        self.assertEqual(t["category"], "Microsoft.TaskCategory")
        self.assertEqual([s["name"] for s in t["states"]], ["Aberto", "Em Andamento", "Concluído", "Removido"])
        self.assertEqual(t["required_on_create"], ["Custom.Equipe", "Microsoft.VSTS.Common.Activity",
                                                   "System.AreaPath", "System.IterationPath", "System.Title"])
        self.assertEqual([r["when"] for r in t["required_by_rule"]], ["Microsoft.VSTS.CMMI.Blocked=Yes", "create"])
        self.assertEqual(t["required_by_rule"][1]["fields"], ["Microsoft.VSTS.Scheduling.OriginalEstimate"])
        self.assertEqual(t["defaults"], {"Microsoft.VSTS.CMMI.Blocked": "No", "Microsoft.VSTS.Common.Priority": 2})
        self.assertEqual(t["picklists"]["Microsoft.VSTS.Common.Priority"], [1, 2, 3, 4])
        self.assertEqual(t["picklists"]["Microsoft.VSTS.Common.Activity"],
                         ["1. Análise", "2. Desenvolvimento", "3. Testes"])
        self.assertTrue(t["fields"][IDF]["identity"])

    def test_blocking_inferred_from_rule(self):
        b = self.types["Task"]["blocking"]
        self.assertEqual(b["fields"], {
            "flag": {"field": "Microsoft.VSTS.CMMI.Blocked", "value_on": "Yes", "value_off": "No"},
            "responsible": IDF, "forecast": "Custom.2b2b2b2b-0000-4000-8000-00000000002b",
            "reason": "Custom.Justificativa"})
        self.assertNotIn("policy", b)  # a política nunca é inferida
        self.assertNotIn("blocking", self.types["User Story"])

    def test_blocking_only_flag_without_rule(self):
        from adoplug import rules
        part = {"required_by_rule": [], "fields": {}, "picklists": {}}
        tf = [{"referenceName": "Microsoft.VSTS.CMMI.Blocked", "allowedValues": ["No", "Yes"]}]
        b = rules.infer_blocking(part, tf, {"Microsoft.VSTS.CMMI.Blocked": {"name": "Blocked", "type": "string"}})
        self.assertEqual(b, {"fields": {"flag": {"field": "Microsoft.VSTS.CMMI.Blocked", "value_on": "Yes",
                                                  "value_off": "No"}}})
        self.assertIn("Microsoft.VSTS.CMMI.Blocked", part["fields"])
        self.assertIsNone(rules.infer_blocking({"required_by_rule": []}, [], {}))

    def test_no_people_or_groups(self):
        text = json.dumps(self.profile, ensure_ascii=False)
        for bad in ("Pessoa Exemplo", "example.com", GROUP, "displayName", "descrição", "System.AssignedTo"):
            self.assertNotIn(bad, text)

    def test_only_get_with_project_route(self):
        urls = self.client.urls
        self.assertTrue(all("api-version=7.1" in u for u in urls))
        type_urls = [u for u in urls if "/_apis/wit/workitemtypes/" in u]
        self.assertTrue(type_urls)
        self.assertTrue(all(f"/{PROJ}/_apis/" in u for u in type_urls))
        self.assertIn(f"{self.client.host}/{PROJ}/_apis/wit/workitemtypes/User%20Story/states?api-version=7.1", urls)
        self.assertFalse(any("Task%20Velha" in u or "Test%20Suite" in u for u in urls))

    def test_all_types(self):
        p = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project="projeto ágil", all_types=True)
        types = p["processes"][PROC]["work_item_types"]
        self.assertEqual(sorted(types), ["Issue", "Task", "User Story"])
        self.assertNotIn("category", types["Issue"])
        self.assertEqual(profile_validate.validate_profile(p), [])

    def test_without_project(self):
        c = FakeClient()
        p = discovery.discover_org(c, "ORG-EXEMPLO")
        self.assertEqual({v["work_item_types"] for v in p["processes"].values()}, {"discover"})
        self.assertEqual(len(c.urls), 1)

    def test_unknown_project(self):
        with self.assertRaises(LookupError):
            discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project="nao-existe")


class DiffProfilesTest(unittest.TestCase):
    def setUp(self):
        self.a = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project=PROJ, measured_at="2026-11-13")

    def test_equal_ignores_measure_date_and_source(self):
        b = copy.deepcopy(self.a)
        b["measured_at"] = "2030-01-01"
        b["source"] = "embedded"
        self.assertEqual(discovery.diff_profiles(self.a, b), [])

    def test_changes(self):
        b = copy.deepcopy(self.a)
        t = b["processes"][PROC]["work_item_types"]["Task"]
        t["picklists"]["Custom.Equipe"].append("EQUIPE C")
        t["suggested_fields"] = ["X.Y"]
        b["projects"] = b["projects"][1:]
        paths = {d["path"] for d in discovery.diff_profiles(self.a, b)}
        base = f"processes/{PROC}/work_item_types/Task"
        self.assertEqual(paths, {
            f"{base}/picklists/Custom.Equipe",
            f"{base}/suggested_fields",
            "projects[id=0d0d0d0d-0000-4000-8000-00000000000d]",
        })

    def test_state_order_matters(self):
        b = copy.deepcopy(self.a)
        states = b["processes"][PROC]["work_item_types"]["Task"]["states"]
        states.reverse()
        paths = [d["path"] for d in discovery.diff_profiles(self.a, b)]
        self.assertEqual(paths, [f"processes/{PROC}/work_item_types/Task/states#order"])


class MergeProfilesTest(unittest.TestCase):
    def setUp(self):
        self.current = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project=PROJ, measured_at="2026-11-13")
        self.current["source"] = "embedded"
        self.current["usage"] = {"work_items": True, "hint": "revisado à mão"}
        t = self.current["processes"][PROC]["work_item_types"]["Task"]
        t["suggested_fields"] = ["Custom.Sugerido"]
        t["fields"]["Custom.Sugerido"] = {"name": "Sugerido", "type": "string"}

    def test_discover_keeps_expanded_types(self):
        other = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", measured_at="2026-12-01")
        merged = discovery.merge_profiles(self.current, other)
        self.assertEqual(merged["processes"][PROC]["work_item_types"],
                         self.current["processes"][PROC]["work_item_types"])
        self.assertEqual(merged["measured_at"], "2026-12-01")
        self.assertEqual(merged["usage"]["hint"], "revisado à mão")
        self.assertEqual(discovery.diff_profiles(self.current, merged), [])
        self.assertEqual(profile_validate.validate_profile(merged), [])

    def test_expanded_replaces_types_and_keeps_suggested(self):
        fresh = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project=PROJ)
        fresh["processes"][PROC]["work_item_types"]["Task"]["picklists"]["Custom.Equipe"].append("EQUIPE C")
        merged = discovery.merge_profiles(self.current, fresh)
        t = merged["processes"][PROC]["work_item_types"]["Task"]
        self.assertEqual(t["suggested_fields"], ["Custom.Sugerido"])
        self.assertIn("Custom.Sugerido", t["fields"])
        paths = [d["path"] for d in discovery.diff_profiles(self.current, merged)]
        self.assertEqual(paths, [f"processes/{PROC}/work_item_types/Task/picklists/Custom.Equipe"])
        self.assertEqual(profile_validate.validate_profile(merged), [])

    def test_keeps_blocking_policy(self):
        t = self.current["processes"][PROC]["work_item_types"]["Task"]
        t["blocking"]["policy"] = {"responsible_default": "ask", "forecast_days": 3}
        fresh = discovery.discover_org(FakeClient(), "ORG-EXEMPLO", project=PROJ)
        merged = discovery.merge_profiles(self.current, fresh)
        mt = merged["processes"][PROC]["work_item_types"]["Task"]
        self.assertEqual(mt["blocking"], t["blocking"])
        del fresh["processes"][PROC]["work_item_types"]["Task"]["blocking"]
        merged = discovery.merge_profiles(self.current, fresh)
        self.assertEqual(merged["processes"][PROC]["work_item_types"]["Task"]["blocking"], t["blocking"])
        self.assertEqual(profile_validate.validate_profile(merged), [])

    def test_without_current(self):
        fresh = discovery.discover_org(FakeClient(), "ORG-EXEMPLO")
        self.assertEqual(discovery.merge_profiles(None, fresh), fresh)


class EmbeddedProfilesTest(unittest.TestCase):
    def test_embedded_profiles_validate(self):
        files = sorted(p for p in (PLUGIN / "profiles").glob("*.json") if not p.name.endswith(".schema.json"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(perfil=f.name):
                data = json.loads(f.read_text(encoding="utf-8"))
                self.assertEqual(profile_validate.validate_profile(data), [])
                self.assertEqual(data["source"], "embedded")
                self.assertEqual(f.stem, data["org"])


if __name__ == "__main__":
    unittest.main()

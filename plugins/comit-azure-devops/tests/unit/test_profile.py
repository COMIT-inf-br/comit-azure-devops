import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
REPO = PLUGIN.parents[1]
sys.path.insert(0, str(PLUGIN / "server"))
from adoplug import profile_validate as pv  # noqa: E402

PROC = "0a0a0a0a-0000-4000-8000-00000000000a"
PROJ = "0c0c0c0c-0000-4000-8000-00000000000c"
IDF = "Custom.1a1a1a1a-0000-4000-8000-00000000001a"
AT = "@"  # montado por partes, como no check_identity.py


def base():
    return {
        "schema_version": 1,
        "org": "ORG-EXEMPLO",
        "host": "https://dev.azure.com/ORG-EXEMPLO",
        "auth": {"preferred": "az", "tenant": "discover"},
        "usage": {"work_items": True, "hint": "texto curto"},
        "measured_at": "2026-11-13",
        "source": "embedded",
        "projects_policy": "discover_others",
        "projects": [{"id": PROJ, "name": "Projeto Ágil", "process": PROC}],
        "processes": {PROC: {"name": "PROC_EXEMPLO", "parent": "Agile", "work_item_types": {
            "Task": {
                "reference_name": "Exemplo.Task",
                "category": "Microsoft.TaskCategory",
                "states": [{"name": "Aberto", "category": "Proposed"}],
                "required_on_create": ["System.Title", IDF],
                "required_by_rule": [{"fields": [IDF], "conditions": [
                    {"type": "when", "field": IDF, "value": "Sim"}, {"type": "create"}],
                    "when": f"create & {IDF}=Sim", "source": "rule:x"}],
                "defaults": {"Microsoft.VSTS.Common.Priority": 2},
                "picklists": {"Microsoft.VSTS.Common.Priority": [1, 2]},
                "suggested_fields": ["Microsoft.VSTS.Common.Priority"],
                "fields": {
                    "System.Title": {"name": "Title", "type": "string"},
                    "Microsoft.VSTS.Common.Priority": {"name": "Priority", "type": "integer"},
                    IDF: {"name": "Responsável", "type": "string", "identity": True},
                },
            }}}},
        "link_types": ["Hyperlink"],
        "api": {"wit": "7.1", "comments": "7.1-preview.4"},
    }


def task(p):
    return p["processes"][PROC]["work_item_types"]["Task"]


class ValidProfileTest(unittest.TestCase):
    def test_base_is_valid(self):
        self.assertEqual(pv.validate_profile(base()), [])

    def test_discover_and_empty_processes(self):
        p = base()
        p["processes"][PROC]["work_item_types"] = "discover"
        self.assertEqual(pv.validate_profile(p), [])
        p["projects"] = []
        p["processes"] = {}
        p["usage"] = {"work_items": False}
        self.assertEqual(pv.validate_profile(p), [])

    def test_embedded_files(self):
        for name in ("MDB-CIT.json", "MDB-DATAPLATFORM.json"):
            with self.subTest(perfil=name):
                self.assertEqual(pv.validate_file(PLUGIN / "profiles" / name), [])


class RejectTest(unittest.TestCase):
    def assertRejects(self, profile, fragment):
        errors = pv.validate_profile(profile)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_secret_keys(self):
        for key in ("pat", "token", "Password", "secret"):
            with self.subTest(chave=key):
                p = base()
                p["auth"][key] = "x"
                self.assertRejects(p, "chave proibida")

    def test_person_keys_anywhere(self):
        for key in ("displayName", "uniqueName", "members", "people", "assignedTo", "descriptor", "_links"):
            with self.subTest(chave=key):
                p = base()
                task(p)["fields"]["System.Title"][key] = "x"
                self.assertRejects(p, "chave proibida")

    def test_unknown_key(self):
        p = base()
        p["extra"] = 1
        self.assertRejects(p, "chave não permitida")

    def test_tenant_must_be_discover(self):
        p = base()
        p["auth"]["tenant"] = str(uuid.uuid4())
        self.assertRejects(p, "auth/tenant")

    def test_email(self):
        p = base()
        task(p)["picklists"]["Microsoft.VSTS.Common.Priority"] = ["pessoa" + AT + "example.com"]
        self.assertRejects(p, "e-mail")

    def test_registration_number(self):
        p = base()
        p["usage"]["hint"] = "fale com " + "abc" + "12345"
        self.assertRejects(p, "matrícula")

    def test_secret_like(self):
        p = base()
        p["usage"]["hint"] = "A1b2" * 15
        self.assertRejects(p, "segredo")

    def test_person_guid_in_text(self):
        p = base()
        p["usage"]["hint"] = "responsável " + str(uuid.uuid4())
        self.assertRejects(p, "GUID fora")

    def test_person_guid_as_value(self):
        person = str(uuid.uuid4())
        cases = [
            lambda p: task(p)["picklists"].__setitem__("Microsoft.VSTS.Common.Priority", [person]),
            lambda p: task(p)["defaults"].__setitem__("Microsoft.VSTS.Common.Priority", person),
            lambda p: task(p)["required_by_rule"][0]["conditions"][0].__setitem__("value", person),
            lambda p: task(p)["states"][0].__setitem__("name", person),
            lambda p: p["projects"][0].__setitem__("name", person),
        ]
        for i, mutate in enumerate(cases):
            with self.subTest(caso=i):
                p = base()
                mutate(p)
                self.assertRejects(p, "GUID fora")

    def test_custom_guid_outside_field_position(self):
        p = base()
        task(p)["states"][0]["name"] = "Custom." + str(uuid.uuid4())
        self.assertRejects(p, "GUID fora")
        p = base()
        p["usage"]["hint"] = "Custom." + str(uuid.uuid4())
        self.assertRejects(p, "GUID fora")

    def test_guid_suffix_after_custom_field(self):
        p = base()
        task(p)["required_on_create"].append(IDF + "-" + str(uuid.uuid4()))
        self.assertTrue(pv.validate_profile(p))

    def test_bare_guid_as_process_value(self):
        p = base()
        p["processes"][PROC]["name"] = str(uuid.uuid4())
        self.assertRejects(p, "GUID fora")

    def test_identity_field_values(self):
        p = base()
        task(p)["defaults"][IDF] = "Pessoa"
        task(p)["picklists"][IDF] = ["Pessoa"]
        errors = pv.validate_profile(p)
        self.assertTrue(any("default em campo de identidade" in e for e in errors), errors)
        self.assertTrue(any("picklist em campo de identidade" in e for e in errors), errors)

    def test_broken_references(self):
        p = base()
        p["projects"][0]["process"] = str(uuid.uuid4())
        self.assertRejects(p, "processo não declarado")
        p = base()
        task(p)["required_on_create"].append("Custom.Faltando")
        self.assertRejects(p, "Custom.Faltando sem entrada em fields")
        p = base()
        p["projects"].append(dict(p["projects"][0]))
        self.assertRejects(p, "projeto repetido")

    def test_schema_shapes(self):
        p = base()
        task(p)["required_by_rule"][0]["conditions"] = [{"type": "whenCurrentUserIsMemberOfGroup", "value": "g"}]
        self.assertRejects(p, "required_by_rule[0]/conditions[0]")
        p = base()
        task(p)["states"][0]["category"] = "Outro"
        self.assertRejects(p, "valor fora")
        p = base()
        p["host"] = "https://exemplo.visualstudio.com"
        self.assertRejects(p, "host: formato")


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "validate_profiles.py"), *args],
                              capture_output=True, text=True)

    def test_embedded_ok(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("V2: ok", r.stdout)

    def test_bad_profile_fails(self):
        p = base()
        p["auth"]["token"] = "x"
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ruim.json"
            f.write_text(json.dumps(p), encoding="utf-8")
            r = self.run_cli(str(f))
        self.assertEqual(r.returncode, 1)
        self.assertIn("chave proibida", r.stdout)

    def test_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ruim.json"
            f.write_text("{", encoding="utf-8")
            r = self.run_cli(str(f))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()

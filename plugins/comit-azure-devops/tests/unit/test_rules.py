import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from adoplug import rules  # noqa: E402

IDF = "Custom.1a1a1a1a-0000-4000-8000-00000000001a"


def rule(actions, conditions=(), name=None, disabled=False):
    return {"name": name, "conditions": list(conditions), "isDisabled": disabled,
            "actions": [{"actionType": a, "targetField": f, "value": None} for a, f in actions]}


def cond(ctype, field, value=None):
    return {"conditionType": ctype, "field": field, "value": value}


class InterpretRulesTest(unittest.TestCase):
    def test_unconditional_and_alias(self):
        unc, con = rules.interpret_rules([rule([("makeRequired", "System.AreaId")]),
                                          rule([("makeRequired", "System.Title")])])
        self.assertEqual(unc, {"System.AreaPath", "System.Title"})
        self.assertEqual(con, [])

    def test_create_condition(self):
        _, con = rules.interpret_rules([rule([("makeRequired", "X.Estimate")],
                                             [cond("whenWas", "System.State", "")], name="Esforço")])
        self.assertEqual(con, [{"fields": ["X.Estimate"], "conditions": [{"type": "create"}],
                                "when": "create", "source": "rule:Esforço"}])

    def test_when_and_whennot_are_and(self):
        _, con = rules.interpret_rules([rule([("makeRequired", "B.Campo"), ("makeRequired", "A.Campo")],
                                             [cond("when", "X.Bloq", "Yes"), cond("whenNot", "Y.Tipo", None)])])
        self.assertEqual(con[0]["fields"], ["A.Campo", "B.Campo"])
        self.assertEqual(con[0]["conditions"], [{"type": "when", "field": "X.Bloq", "value": "Yes"},
                                                {"type": "whenNot", "field": "Y.Tipo", "value": ""}])
        self.assertEqual(con[0]["when"], "X.Bloq=Yes & Y.Tipo!=")
        self.assertEqual(con[0]["source"], "rule:system")

    def test_skipped_rules(self):
        skipped = [
            # Estados do processo base: a avaliação é do validateOnly.
            rule([("makeRequired", "System.Reason")],
                 [cond("whenWas", "System.State", "New"), cond("when", "System.State", "Closed")]),
            rule([("makeRequired", "A.B")], [cond("when", "System.State", "Concluído")]),
            rule([("makeRequired", "A.B")], [cond("whenChanged", "System.State")]),
            rule([("makeRequired", "A.B")], [cond("whenNotChanged", "A.C")]),
            # Grupo: nunca vira condição nem guarda o id do grupo.
            rule([("makeRequired", "A.B")], [cond("whenCurrentUserIsMemberOfGroup", None, "grupo")]),
            rule([("makeRequired", "A.B")], disabled=True),
            # Ações que não tornam o campo obrigatório.
            rule([("copyValue", "A.B"), ("makeReadOnly", "A.C"), ("setDefaultFromCurrentUser", "A.D")]),
        ]
        self.assertEqual(rules.interpret_rules(skipped), (set(), []))


class InterpretTypeTest(unittest.TestCase):
    META = {
        "System.Title": {"name": "Title", "type": "string"},
        "System.AreaPath": {"name": "Area Path", "type": "treePath"},
        "Microsoft.VSTS.Common.Priority": {"name": "Priority", "type": "integer"},
        "Custom.Equipe": {"name": "Equipe", "type": "string"},
        "Custom.Texto": {"name": "Texto", "type": "html"},
        IDF: {"name": "Responsável", "type": "string", "isIdentity": True},
    }

    def fields(self):
        return [
            {"referenceName": "System.Title", "alwaysRequired": True},
            {"referenceName": "System.State", "alwaysRequired": True, "defaultValue": "Aberto",
             "allowedValues": ["Aberto"]},
            {"referenceName": "System.AreaId", "alwaysRequired": True},
            {"referenceName": "System.Description", "defaultValue": "modelo"},
            {"referenceName": "Microsoft.VSTS.Common.Priority", "defaultValue": "2",
             "allowedValues": ["1", "2", "3"]},
            {"referenceName": "Custom.Equipe", "alwaysRequired": True, "allowedValues": ["A", "B"]},
            {"referenceName": "Custom.Texto", "defaultValue": "Preencha"},
            {"referenceName": IDF, "defaultValue": {"displayName": "P"}, "allowedValues": ["Pessoa"]},
        ]

    def test_shape(self):
        out = rules.interpret_type(self.fields(), [rule([("makeRequired", IDF)],
                                                        [cond("when", "Custom.Equipe", "A")])], self.META)
        self.assertEqual(out["required_on_create"], ["Custom.Equipe", "System.AreaPath", "System.Title"])
        self.assertEqual(out["defaults"], {"Microsoft.VSTS.Common.Priority": 2})
        self.assertEqual(out["picklists"], {"Custom.Equipe": ["A", "B"],
                                            "Microsoft.VSTS.Common.Priority": [1, 2, 3]})
        self.assertEqual(out["fields"][IDF], {"name": "Responsável", "type": "string", "identity": True})
        self.assertNotIn("System.State", out["fields"])
        self.assertEqual(out["required_by_rule"][0]["fields"], [IDF])

    def test_identity_never_has_values(self):
        out = rules.interpret_type(self.fields(), [rule([("makeRequired", IDF)])], self.META)
        self.assertIn(IDF, out["required_on_create"])
        self.assertNotIn(IDF, out["defaults"])
        self.assertNotIn(IDF, out["picklists"])

    def test_convert_value(self):
        self.assertEqual(rules.convert_value("2", "integer"), 2)
        self.assertEqual(rules.convert_value("x", "integer"), "x")
        self.assertEqual(rules.convert_value("2", "string"), "2")
        self.assertEqual(rules.convert_value("1.5", "double"), "1.5")


class EvaluateTest(unittest.TestCase):
    TYPE = {
        "required_on_create": ["System.Title", "Custom.Equipe"],
        "required_by_rule": [
            {"fields": ["X.Estimate"], "conditions": [{"type": "create"}], "when": "create", "source": "rule:a"},
            {"fields": ["X.Motivo", IDF], "conditions": [{"type": "when", "field": "X.Bloq", "value": "Yes"}],
             "when": "X.Bloq=Yes", "source": "rule:b"},
            {"fields": ["X.Extra"], "conditions": [{"type": "whenNot", "field": "X.Tipo", "value": "Simples"},
                                                   {"type": "create"}],
             "when": "create & X.Tipo!=Simples", "source": "rule:c"},
        ],
        "defaults": {"X.Bloq": "No", "X.Tipo": "Simples", "Microsoft.VSTS.Common.Priority": 2},
        "picklists": {"Custom.Equipe": ["A", "B"], "Microsoft.VSTS.Common.Priority": [1, 2, 3, 4]},
    }

    def test_create_with_defaults(self):
        self.assertEqual(rules.required_fields(self.TYPE, {}),
                         ["Custom.Equipe", "System.Title", "X.Estimate"])
        self.assertEqual(rules.missing_required(self.TYPE, {"System.Title": "t", "Custom.Equipe": ""}),
                         ["Custom.Equipe", "X.Estimate"])

    def test_conditional(self):
        req = rules.required_fields(self.TYPE, {"X.Bloq": "Yes", "X.Tipo": "Completo"})
        self.assertEqual(req, sorted(["Custom.Equipe", "System.Title", "X.Estimate", "X.Motivo", IDF, "X.Extra"]))

    def test_update_ignores_create_rules(self):
        self.assertEqual(rules.required_fields(self.TYPE, {"X.Bloq": "Yes"}, creating=False),
                         sorted(["X.Motivo", IDF]))

    def test_missing_field_counts_as_empty(self):
        t = {"required_by_rule": [{"fields": ["A.B"], "conditions": [{"type": "when", "field": "A.C", "value": ""}],
                                   "when": "A.C=", "source": "rule:x"}]}
        self.assertEqual(rules.required_fields(t, {}), ["A.B"])
        self.assertEqual(rules.required_fields(t, {"A.C": "v"}), [])

    def test_unknown_condition(self):
        t = {"required_by_rule": [{"fields": ["A.B"], "conditions": [{"type": "grupo"}]}]}
        with self.assertRaises(ValueError):
            rules.required_fields(t, {})

    def test_picklist_values(self):
        self.assertEqual(rules.invalid_picklist_values(self.TYPE, {"Custom.Equipe": "C",
                                                                   "Microsoft.VSTS.Common.Priority": "2"}),
                         {"Custom.Equipe": "C"})
        self.assertEqual(rules.invalid_picklist_values(self.TYPE, {"Custom.Equipe": ""}), {})


if __name__ == "__main__":
    unittest.main()

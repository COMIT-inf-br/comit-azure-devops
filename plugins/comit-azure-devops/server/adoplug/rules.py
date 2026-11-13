"""Obrigatórios por tipo de work item: interpretação das regras do processo e avaliação local.

Duas partes:

- `interpret_type` converte as respostas cruas do ADO (campos do tipo no projeto e regras
  do tipo no processo) na parte de obrigatórios, defaults e picklists do perfil.
- `required_fields` e `missing_required` avaliam o perfil contra os campos de uma criação.

A avaliação local só cobre a criação. Regras de transição de estado ficam com o
`validateOnly` do ADO, porque os nomes de estado das regras de sistema são os do
processo base (New, Active, Closed) e não os estados exibidos.
"""

# O `alwaysRequired` do ADO usa os ids de área e iteração; o payload usa os caminhos.
FIELD_ALIASES = {
    "System.AreaId": "System.AreaPath",
    "System.IterationId": "System.IterationPath",
}
# Obrigatórios que o ADO preenche sozinho na criação.
IMPLICIT_REQUIRED = frozenset({"System.State", "System.WorkItemType", "System.Reason"})
# Tipos de campo cujo default é texto de modelo, não valor de picklist.
TEXT_TYPES = frozenset({"html", "plainText", "history"})
STATE_FIELD = "System.State"


def canonical_field(ref):
    return FIELD_ALIASES.get(ref, ref)


def convert_value(value, field_type):
    """Converte o texto da API pelo tipo do campo: `integer` vira int; o resto fica como veio."""
    if field_type == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return value


def _condition(cond):
    """Traduz uma condição do ADO. Devolve dict, ou None quando a regra não vale na criação.

    Fica de fora: condição sobre `System.State` (exceto `whenWas ""`, que é a criação),
    `whenChanged`/`whenNotChanged` e qualquer tipo não conhecido (grupo, usuário atual).
    """
    ctype = cond.get("conditionType")
    field = cond.get("field")
    value = cond.get("value")
    if field == STATE_FIELD:
        if ctype == "whenWas" and (value or "") == "":
            return {"type": "create"}
        return None
    if ctype in ("when", "whenNot") and isinstance(field, str):
        return {"type": ctype, "field": canonical_field(field), "value": "" if value is None else str(value)}
    return None


def _label(conditions):
    parts = []
    for c in conditions:
        if c["type"] == "create":
            parts.append("create")
        elif c["type"] == "when":
            parts.append(f"{c['field']}={c['value']}")
        else:
            parts.append(f"{c['field']}!={c['value']}")
    return " & ".join(parts)


def _cond_order(c):
    return (c["type"] != "create", c.get("field", ""), c.get("value", ""))


def interpret_rules(rules):
    """Separa as regras `makeRequired` em incondicionais e condicionais.

    Devolve `(unconditional, conditional)`: um set de campos e uma lista de
    `{"fields", "conditions", "when", "source"}`. `copyValue`, `makeReadOnly` e as ações
    de usuário atual não tornam campo obrigatório e ficam de fora.
    """
    unconditional = set()
    conditional = []
    for rule in rules:
        if rule.get("isDisabled"):
            continue
        targets = sorted({canonical_field(a["targetField"]) for a in rule.get("actions") or []
                          if a.get("actionType") == "makeRequired" and a.get("targetField")})
        if not targets:
            continue
        conds = []
        valid = True
        for raw in rule.get("conditions") or []:
            c = _condition(raw)
            if c is None:
                valid = False
                break
            if c not in conds:
                conds.append(c)
        if not valid:
            continue
        if not conds:
            unconditional.update(targets)
            continue
        conds.sort(key=_cond_order)
        name = rule.get("name")
        conditional.append({
            "fields": targets,
            "conditions": conds,
            "when": _label(conds),
            "source": f"rule:{name}" if name else "rule:system",
        })
    conditional.sort(key=lambda r: (r["when"], r["source"], r["fields"]))
    return unconditional, conditional


def interpret_type(type_fields, rules, field_meta):
    """Monta obrigatórios, defaults, picklists e o mapa de campos de um tipo.

    - `type_fields`: `value` de `.../wit/workitemtypes/{tipo}/fields?$expand=allowedValues`.
    - `rules`: `value` de `.../work/processes/{id}/workItemTypes/{ref}/rules`.
    - `field_meta`: `{ref: {"name", "type", "isIdentity"}}` de `_apis/wit/fields` da org.

    Campo de identidade nunca leva default nem picklist: os valores seriam pessoas.
    """
    unconditional, conditional = interpret_rules(rules)
    by_ref = {f["referenceName"]: f for f in type_fields}

    required = set(unconditional)
    required.update(canonical_field(f["referenceName"]) for f in type_fields if f.get("alwaysRequired"))
    required -= IMPLICIT_REQUIRED

    def meta(ref):
        m = field_meta.get(ref) or {}
        return m.get("type") or "string", bool(m.get("isIdentity"))

    defaults = {}
    for f in type_fields:
        ref = f["referenceName"]
        dv = f.get("defaultValue")
        ftype, ident = meta(ref)
        if ref.startswith("System.") or ident or ftype in TEXT_TYPES:
            continue
        if dv is None or dv == "" or isinstance(dv, (dict, list)):
            continue
        defaults[ref] = convert_value(dv, ftype)

    rule_fields = {ref for r in conditional for ref in r["fields"]}
    cond_fields = {c["field"] for r in conditional for c in r["conditions"] if "field" in c}
    wanted = required | rule_fields | cond_fields | set(defaults)

    picklists = {}
    for ref in sorted(wanted):
        f = by_ref.get(ref)
        ftype, ident = meta(ref)
        if f is None or ref.startswith("System.") or ident:
            continue
        values = [v for v in f.get("allowedValues") or [] if isinstance(v, (str, int, float))]
        if values:
            picklists[ref] = [convert_value(v, ftype) for v in values]

    fields = {}
    for ref in sorted(wanted):
        ftype, ident = meta(ref)
        name = (field_meta.get(ref) or {}).get("name") or (by_ref.get(ref) or {}).get("name") or ref
        entry = {"name": name, "type": ftype}
        if ident:
            entry["identity"] = True
        fields[ref] = entry

    return {
        "required_on_create": sorted(required),
        "required_by_rule": conditional,
        "defaults": dict(sorted(defaults.items())),
        "picklists": picklists,
        "fields": fields,
    }


def _value(fields, ref):
    v = fields.get(ref)
    return "" if v is None else str(v)


def condition_holds(cond, fields, creating=True):
    """Avalia uma condição do perfil. Campo ausente conta como vazio."""
    if cond["type"] == "create":
        return creating
    if cond["type"] == "when":
        return _value(fields, cond["field"]) == cond["value"]
    if cond["type"] == "whenNot":
        return _value(fields, cond["field"]) != cond["value"]
    raise ValueError(f"condição desconhecida: {cond['type']}")


def effective_fields(type_profile, fields):
    """Campos da criação completados pelos defaults do perfil (o valor informado prevalece)."""
    out = dict(type_profile.get("defaults") or {})
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def required_fields(type_profile, fields, creating=True):
    """Lista ordenada dos campos obrigatórios para estes valores, com os defaults aplicados."""
    eff = effective_fields(type_profile, fields)
    req = set(type_profile.get("required_on_create") or []) if creating else set()
    for rule in type_profile.get("required_by_rule") or []:
        if all(condition_holds(c, eff, creating) for c in rule["conditions"]):
            req.update(rule["fields"])
    return sorted(req)


def missing_required(type_profile, fields, creating=True):
    """Obrigatórios sem valor depois dos defaults. Vazio não conta como valor."""
    eff = effective_fields(type_profile, fields)
    return [ref for ref in required_fields(type_profile, fields, creating)
            if eff.get(ref) in (None, "") and ref not in IMPLICIT_REQUIRED]


def invalid_picklist_values(type_profile, fields):
    """Campos cujo valor informado não está na picklist do perfil: `{ref: valor}`."""
    bad = {}
    for ref, values in (type_profile.get("picklists") or {}).items():
        if ref in fields and fields[ref] not in (None, ""):
            allowed = {str(v) for v in values}
            if str(fields[ref]) not in allowed:
                bad[ref] = fields[ref]
    return bad


# --- padrão de impedimento (bloco `blocking` do perfil) ------------------------------------

BLOCKED_FIELD = "Microsoft.VSTS.CMMI.Blocked"
_FLAG_HINTS = ("block", "imped")
_FORECAST_HINTS = ("previs", "forecast", "expected", "estimat", "desimped", "unblock", "resolu")
_TEXT_ROLE_TYPES = ("string", "plainText", "html")


def _looks_like_flag(ref, name):
    text = (ref + " " + (name or "")).casefold()
    return ref == BLOCKED_FIELD or any(h in text for h in _FLAG_HINTS)


def _flag_off(value_on, values):
    others = [v for v in values or [] if str(v) != str(value_on)]
    if len(others) == 1:
        return others[0]
    return "No" if str(value_on) == "Yes" else None


def infer_blocking(part, type_fields, field_meta):
    """Infere o bloco `blocking` de um tipo já interpretado. Devolve o bloco ou None.

    Usa a regra que exige campos quando a flag tem um valor (`when <flag>=<valor>`): o campo
    de identidade vira o responsável, a data com nome de previsão vira a previsão, a outra
    data vira a data e o texto vira o motivo. Papel com mais de um candidato fica de fora.
    Sem essa regra, mapeia só a flag `Blocked`, se o tipo tiver o campo. Nunca infere política.
    Acrescenta a `part["fields"]` (e à picklist) a flag que ainda não estiver lá.
    """
    fmap = part.setdefault("fields", {})
    by_ref = {f["referenceName"]: f for f in type_fields}

    def name_of(ref):
        return (fmap.get(ref) or {}).get("name") or (field_meta.get(ref) or {}).get("name") or ref

    def values_of(ref):
        if ref in (part.get("picklists") or {}):
            return part["picklists"][ref]
        return [v for v in (by_ref.get(ref) or {}).get("allowedValues") or [] if isinstance(v, (str, int, float))]

    block = None
    for rule in part.get("required_by_rule") or []:
        conds = rule.get("conditions") or []
        if len(conds) != 1 or conds[0].get("type") != "when":
            continue
        flag, on = conds[0]["field"], conds[0]["value"]
        if not _looks_like_flag(flag, name_of(flag)):
            continue
        off = _flag_off(on, values_of(flag))
        if off is None:
            continue
        roles = {"responsible": [], "date": [], "forecast": [], "reason": []}
        for ref in rule["fields"]:
            meta = fmap.get(ref) or {}
            ftype = meta.get("type") or (field_meta.get(ref) or {}).get("type")
            if meta.get("identity") or (field_meta.get(ref) or {}).get("isIdentity"):
                roles["responsible"].append(ref)
            elif ftype == "dateTime":
                hint = any(h in (ref + " " + name_of(ref)).casefold() for h in _FORECAST_HINTS)
                roles["forecast" if hint else "date"].append(ref)
            elif ftype in _TEXT_ROLE_TYPES:
                roles["reason"].append(ref)
        block = {"fields": {"flag": {"field": flag, "value_on": on, "value_off": off}}}
        for role, refs in roles.items():
            if len(refs) == 1:
                block["fields"][role] = refs[0]
        break
    if block is None and BLOCKED_FIELD in by_ref:
        values = values_of(BLOCKED_FIELD)
        if not values or {"Yes", "No"} <= {str(v) for v in values}:
            block = {"fields": {"flag": {"field": BLOCKED_FIELD, "value_on": "Yes", "value_off": "No"}}}
    if block is None:
        return None
    flag = block["fields"]["flag"]["field"]
    if flag not in fmap:
        meta = field_meta.get(flag) or {}
        fmap[flag] = {"name": name_of(flag), "type": meta.get("type") or "string"}
        part["fields"] = dict(sorted(fmap.items()))
    values = values_of(flag)
    if values and flag not in (part.get("picklists") or {}):
        part.setdefault("picklists", {})[flag] = list(values)
    return block

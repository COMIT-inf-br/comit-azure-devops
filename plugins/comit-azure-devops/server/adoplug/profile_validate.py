"""Validador de perfil de organização (V2), só com a biblioteca padrão.

Aplica `profiles/profile.schema.json` (um subconjunto do JSON Schema 2020-12) e as regras
de conteúdo proibido:

- chave de segredo ou de pessoa (`pat`, `token`, `displayName`, `members`...);
- e-mail, matrícula ou texto com cara de segredo em qualquer valor;
- GUID fora das posições permitidas. Posições permitidas: `projects[].id`,
  `projects[].process`, chave de `processes` e o sufixo de `Custom.<guid>` em posição de
  campo (também nos campos do bloco `blocking`). Qualquer outro GUID pode ser de pessoa ou
  identidade e reprova o perfil;
- default ou picklist em campo de identidade;
- referência quebrada (processo ou campo que o perfil não declara);
- bloco `blocking` incoerente (responsável sem identidade, flag fora da lista, prazo fora de 0 a 60).

Uso: `validate_profile(perfil) -> [erros]`; lista vazia = válido.
"""
import json
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "profiles" / "profile.schema.json"

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
FULL_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CUSTOM_GUID = re.compile(r"(?<![\w.])Custom\.(" + GUID.pattern + r")(?![\w-])")
EMAIL = re.compile(r"@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MATRICULA = re.compile(r"\b[a-z]{3}\d{5}\b", re.I)
SECRET_LIKE = re.compile(r"[A-Za-z0-9+/=_]{40,}")
FORBIDDEN_KEYS = frozenset({
    "pat", "token", "accesstoken", "password", "secret", "apikey",
    "members", "people", "assignedto", "displayname", "uniquename", "descriptor",
    "imageurl", "_links", "email", "mail", "tenantid", "identity_id", "subjectdescriptor",
})
# Contextos (último segmento estrutural) em que um valor é referência de campo.
FIELD_LIST_KEYS = ("required_on_create", "suggested_fields", "fields")
FIELD_MAP_KEYS = ("defaults", "picklists", "fields")
# Papéis de campo do bloco `blocking`, além da flag.
BLOCKING_ROLES = ("responsible", "reason", "date", "forecast")
FORECAST_DAYS_MAX = 60
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def load_schema(path=SCHEMA_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fmt(path):
    out = ""
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else (f"/{seg}" if out else str(seg))
    return out or "<raiz>"


# ---------- subconjunto do JSON Schema ----------

def _resolve(root, ref):
    if not ref.startswith("#/"):
        raise ValueError(f"$ref não suportado: {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _same_shape(value, schema):
    if "const" in schema:
        return type(value) is type(schema["const"])
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        return any(_TYPES[t](value) for t in types)
    return True


def _check(value, schema, root, path, errors):
    if "$ref" in schema:
        _check(value, _resolve(root, schema["$ref"]), root, path, errors)
    if "oneOf" in schema:
        trials = []
        for sub in schema["oneOf"]:
            trial = []
            _check(value, sub, root, path, trial)
            trials.append(trial)
        ok = sum(not t for t in trials)
        if ok == 0:
            # Mostra os erros da forma do mesmo tipo do valor, para o diagnóstico apontar o campo.
            fit = [t for sub, t in zip(schema["oneOf"], trials) if _same_shape(value, sub)]
            errors.extend(min(fit or trials, key=len))
        elif ok > 1:
            errors.append(f"{_fmt(path)}: casa com mais de uma forma permitida")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{_fmt(path)}: valor deve ser {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{_fmt(path)}: valor fora de {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            errors.append(f"{_fmt(path)}: tipo deve ser {'/'.join(types)}")
            return
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{_fmt(path)}: formato inválido")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{_fmt(path)}: texto curto demais")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{_fmt(path)}: texto longo demais")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{_fmt(path)}: itens de menos")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], root, path + (i,), errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{_fmt(path + (key,))}: chave obrigatória ausente")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        names = schema.get("propertyNames")
        for key, sub in value.items():
            if names is not None:
                _check(key, names, root, path + (key,), errors)
            if key in props:
                _check(sub, props[key], root, path + (key,), errors)
            elif extra is False:
                errors.append(f"{_fmt(path + (key,))}: chave não permitida")
            elif isinstance(extra, dict):
                _check(sub, extra, root, path + (key,), errors)


# ---------- conteúdo proibido ----------

def _structural(path):
    """Segmentos de chave (sem índices de lista)."""
    return [s for s in path if not isinstance(s, int)]


def _is_field_position(path, is_key):
    """True quando o texto em `path` é uma referência de campo dentro de um tipo."""
    keys = _structural(path)
    if len(keys) < 5 or keys[0] != "processes" or keys[2] != "work_item_types":
        return False
    rest = keys[4:]  # depois do nome do tipo
    if is_key:
        return len(rest) == 2 and rest[0] in FIELD_MAP_KEYS
    if not rest:
        return False
    if len(rest) == 1 and rest[0] in FIELD_LIST_KEYS and rest[0] != "fields":
        return isinstance(path[-1], int)
    if rest[0] == "required_by_rule":
        return rest[1:] in (["fields"], ["when"], ["conditions", "field"])
    if rest[0] == "blocking":
        return rest[1:] in (["fields", "flag", "field"],) or (
            len(rest) == 3 and rest[1] == "fields" and rest[2] in BLOCKING_ROLES)
    return False


def _guid_allowed(text, path, is_key):
    keys = _structural(path)
    if not is_key and len(path) == 3 and path[0] == "projects" and path[2] in ("id", "process"):
        return FULL_GUID.match(text) is not None
    if is_key and len(keys) == 2 and keys[0] == "processes":
        return FULL_GUID.match(text) is not None
    if _is_field_position(path, is_key):
        return not GUID.search(CUSTOM_GUID.sub("Custom.x", text))
    return False


def _scan_text(text, path, is_key, errors):
    where = _fmt(path) + (" (chave)" if is_key else "")
    if EMAIL.search(text):
        errors.append(f"{where}: e-mail ou domínio de conta")
    if MATRICULA.search(text):
        errors.append(f"{where}: possível matrícula de pessoa")
    if SECRET_LIKE.search(text):
        errors.append(f"{where}: texto com cara de segredo")
    if GUID.search(text) and not _guid_allowed(text, path, is_key):
        errors.append(f"{where}: GUID fora das posições permitidas (pode ser de pessoa)")


def _scan(value, path, errors):
    if isinstance(value, dict):
        for key, sub in value.items():
            kpath = path + (key,)
            if isinstance(key, str):
                if key.casefold() in FORBIDDEN_KEYS:
                    errors.append(f"{_fmt(kpath)}: chave proibida")
                _scan_text(key, kpath, True, errors)
            _scan(sub, kpath, errors)
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            _scan(sub, path + (i,), errors)
    elif isinstance(value, str):
        _scan_text(value, path, False, errors)


def _semantic(profile, errors):
    procs = profile.get("processes") if isinstance(profile.get("processes"), dict) else {}
    seen = set()
    for i, p in enumerate(profile.get("projects") or []):
        if not isinstance(p, dict):
            continue
        if p.get("id") in seen:
            errors.append(f"projects[{i}]/id: projeto repetido")
        seen.add(p.get("id"))
        if p.get("process") not in procs:
            errors.append(f"projects[{i}]/process: processo não declarado em processes")
    for pid, proc in procs.items():
        types = proc.get("work_item_types") if isinstance(proc, dict) else None
        if not isinstance(types, dict):
            continue
        for tname, t in types.items():
            if not isinstance(t, dict):
                continue
            base = f"processes/{pid}/work_item_types/{tname}"
            fmap = t.get("fields") if isinstance(t.get("fields"), dict) else {}
            refs = list(t.get("required_on_create") or []) + list(t.get("suggested_fields") or [])
            for r in t.get("required_by_rule") or []:
                refs += list(r.get("fields") or [])
                refs += [c.get("field") for c in r.get("conditions") or [] if c.get("field")]
            refs += list((t.get("defaults") or {}).keys()) + list((t.get("picklists") or {}).keys())
            for ref in sorted(set(refs)):
                if ref not in fmap:
                    errors.append(f"{base}: campo {ref} sem entrada em fields")
            _semantic_blocking(t.get("blocking"), fmap, t.get("picklists") or {}, base, errors)
            for ref, meta in fmap.items():
                if isinstance(meta, dict) and meta.get("identity"):
                    if ref in (t.get("defaults") or {}):
                        errors.append(f"{base}/defaults/{ref}: default em campo de identidade")
                    if ref in (t.get("picklists") or {}):
                        errors.append(f"{base}/picklists/{ref}: picklist em campo de identidade")


def _semantic_blocking(block, fmap, picks, base, errors):
    """Campos do impedimento declarados em `fields`, responsável de identidade e valores da flag na lista."""
    if not isinstance(block, dict):
        return
    where = base + "/blocking"
    bf = block.get("fields") if isinstance(block.get("fields"), dict) else {}
    flag = bf.get("flag") if isinstance(bf.get("flag"), dict) else {}
    refs = {"flag": flag.get("field")}
    refs.update({r: bf.get(r) for r in BLOCKING_ROLES})
    for role, ref in refs.items():
        if isinstance(ref, str) and ref not in fmap:
            errors.append(f"{where}/fields/{role}: campo {ref} sem entrada em fields")
    resp = bf.get("responsible")
    if isinstance(resp, str) and isinstance(fmap.get(resp), dict) and not fmap[resp].get("identity"):
        errors.append(f"{where}/fields/responsible: campo {resp} não é de identidade")
    values = picks.get(flag.get("field")) if isinstance(flag.get("field"), str) else None
    for k in ("value_on", "value_off"):
        if values and k in flag and str(flag[k]) not in {str(v) for v in values}:
            errors.append(f"{where}/fields/flag/{k}: valor fora da lista do campo")
    if "value_on" in flag and str(flag.get("value_on")) == str(flag.get("value_off")):
        errors.append(f"{where}/fields/flag: value_on igual a value_off")
    pol = block.get("policy") if isinstance(block.get("policy"), dict) else {}
    days = pol.get("forecast_days")
    if isinstance(days, int) and not isinstance(days, bool) and not 0 <= days <= FORECAST_DAYS_MAX:
        errors.append(f"{where}/policy/forecast_days: use de 0 a {FORECAST_DAYS_MAX} dias")
    for role in pol.get("clear_on_unblock") or []:
        if role in BLOCKING_ROLES and not bf.get(role):
            errors.append(f"{where}/policy/clear_on_unblock: papel {role} sem campo em fields")
    if pol.get("comment_templates") and "default" not in pol["comment_templates"]:
        errors.append(f"{where}/policy/comment_templates: falta o modelo default")


def validate_profile(profile, schema=None):
    """Lista de erros do perfil. Vazia = válido."""
    schema = schema or load_schema()
    errors = []
    _check(profile, schema, schema, (), errors)
    _scan(profile, (), errors)
    if isinstance(profile, dict):
        _semantic(profile, errors)
    return errors


def validate_file(path, schema=None):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: não é JSON legível ({type(exc).__name__})"]
    return validate_profile(data, schema)

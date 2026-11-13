"""Descoberta do perfil de uma organização do Azure DevOps, só por GET.

O cliente HTTP é injetado. Ele precisa de um método `get_json(url) -> dict` que devolve o
JSON decodificado e levanta exceção em resposta fora de 2xx. A descoberta nunca chama
`post_json`. O `http` do S2 cumpre esse contrato; os testes usam um cliente falso.

O tenant não entra no perfil (`auth.tenant = "discover"`). Ele vem do header
`x-vss-resourcetenant`, e `get_json` não devolve headers.
"""
import datetime
from urllib.parse import quote

from . import rules

API = "api-version=7.1"
API_VERSIONS = {"wit": "7.1", "comments": "7.1-preview.4"}
LINK_TYPES = [
    "System.LinkTypes.Hierarchy-Reverse",
    "System.LinkTypes.Hierarchy-Forward",
    "System.LinkTypes.Related",
    "System.LinkTypes.Dependency-Forward",
    "System.LinkTypes.Dependency-Reverse",
    "Hyperlink",
    "AttachedFile",
]
# Tipos de backlog. Os demais (Test Case, Issue, tipos ocultos ou sem categoria) só entram
# com all_types=True.
BACKLOG_CATEGORIES = (
    "Microsoft.EpicCategory",
    "Microsoft.FeatureCategory",
    "Microsoft.RequirementCategory",
    "Microsoft.TaskCategory",
    "Microsoft.BugCategory",
)
HIDDEN_CATEGORY = "Microsoft.HiddenCategory"
NIL_GUID = "-".join(["0" * 8, "0" * 4, "0" * 4, "0" * 4, "0" * 12])
# Chaves ignoradas por `diff_profiles`: mudam a cada medição.
DIFF_IGNORED = ("measured_at", "source")


def org_host(org):
    return "https://dev.azure.com/" + quote(org, safe="")


def _values(resp):
    return (resp or {}).get("value") or []


def list_processes(client, host):
    """Processos com os projetos de cada um. Devolve (processos, nome do processo por id)."""
    procs = _values(client.get_json(f"{host}/_apis/work/processes?$expand=projects&{API}"))
    names = {p["typeId"]: p["name"] for p in procs}
    return procs, names


def field_metadata(client, host):
    """`{ref: {"name", "type", "isIdentity"}}` da org. É a única fonte confiável de identidade:
    o endpoint de campos do processo devolve `type: string` para campo de pessoa."""
    out = {}
    for f in _values(client.get_json(f"{host}/_apis/wit/fields?{API}")):
        out[f["referenceName"]] = {
            "name": f.get("name"),
            "type": f.get("type"),
            "isIdentity": bool(f.get("isIdentity")),
        }
    return out


def _type_category(categories):
    """`{nome do tipo: categoria}`, sem a categoria oculta; e o set de tipos ocultos."""
    cat_of, hidden = {}, set()
    for c in categories:
        names = {t["name"] for t in c.get("workItemTypes") or []}
        if c.get("referenceName") == HIDDEN_CATEGORY:
            hidden |= names
            continue
        for n in names:
            cat_of.setdefault(n, c.get("referenceName"))
    return cat_of, hidden


def discover_types(client, host, process_id, project_id, field_meta, all_types=False):
    """Tipos de work item de um processo, lidos num projeto desse processo.

    Fica de fora: tipo desativado, tipo oculto e, sem `all_types`, tipo fora das
    categorias de backlog.
    """
    proj = quote(project_id, safe="")
    wits = _values(client.get_json(f"{host}/_apis/work/processes/{process_id}/workitemtypes?{API}"))
    cats = _values(client.get_json(f"{host}/{proj}/_apis/wit/workitemtypecategories?{API}"))
    cat_of, hidden = _type_category(cats)
    out = {}
    for wit in sorted(wits, key=lambda w: w["name"]):
        name = wit["name"]
        if wit.get("isDisabled") or name in hidden:
            continue
        category = cat_of.get(name)
        if not all_types and category not in BACKLOG_CATEGORIES:
            continue
        tname = quote(name, safe="")
        tfields = _values(client.get_json(
            f"{host}/{proj}/_apis/wit/workitemtypes/{tname}/fields?$expand=allowedValues&{API}"))
        states = _values(client.get_json(f"{host}/{proj}/_apis/wit/workitemtypes/{tname}/states?{API}"))
        trules = _values(client.get_json(
            f"{host}/_apis/work/processes/{process_id}/workItemTypes/"
            f"{quote(wit['referenceName'], safe='')}/rules?{API}"))
        part = rules.interpret_type(tfields, trules, field_meta)
        entry = {"reference_name": wit["referenceName"]}
        if category:
            entry["category"] = category
        entry["states"] = [{"name": s["name"], "category": s["category"]} for s in states]
        entry.update(part)
        out[name] = entry
    return out


def discover_org(client, org, project=None, all_types=False, measured_at=None, usage=None):
    """Proposta de perfil da org (`source = "discovered"`).

    Todos os projetos entram, com id, nome e processo. Os tipos de work item só são lidos
    no processo do `project` (GUID ou nome); os outros processos ficam com
    `work_item_types = "discover"`, para descoberta no primeiro uso. Sem `project`, nenhum
    processo é expandido.
    """
    host = org_host(org)
    procs, names = list_processes(client, host)
    projects = []
    processes = {}
    target = None
    for p in procs:
        plist = p.get("projects") or []
        if not plist:
            continue
        parent = names.get(p.get("parentProcessTypeId"))
        if p.get("parentProcessTypeId") in (None, NIL_GUID):
            parent = p["name"]
        processes[p["typeId"]] = {"name": p["name"], "parent": parent, "work_item_types": "discover"}
        for pr in plist:
            projects.append({"id": pr["id"], "name": pr["name"], "process": p["typeId"]})
            if project and project.lower() in (pr["id"].lower(), pr["name"].lower()):
                target = (p["typeId"], pr["id"])
    if project and target is None:
        raise LookupError(f"projeto não encontrado na org {org}: {project}")
    if target:
        meta = field_metadata(client, host)
        processes[target[0]]["work_item_types"] = discover_types(
            client, host, target[0], target[1], meta, all_types=all_types)
    projects.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    profile = {
        "schema_version": 1,
        "org": org,
        "host": host,
        "auth": {"preferred": "az", "tenant": "discover"},
        "usage": usage or {"work_items": True},
        "measured_at": measured_at or datetime.date.today().isoformat(),
        "source": "discovered",
        "projects_policy": "discover_others",
        "projects": projects,
        "processes": dict(sorted(processes.items())),
        "link_types": list(LINK_TYPES),
        "api": dict(API_VERSIONS),
    }
    return profile


def merge_profiles(current, discovered):
    """Junta uma descoberta ao perfil atual sem perder o que a descoberta não leu.

    - Projetos, processos presentes e datas vêm da descoberta.
    - Processo com `work_item_types = "discover"` na descoberta mantém os tipos do atual.
    - Processo expandido na descoberta substitui os tipos; cada tipo mantém os
      `suggested_fields` do atual quando a descoberta não traz esse campo.
    - `usage` do atual prevalece (o `hint` é texto revisado à mão).

    Use antes de `diff_profiles` e de gravar uma proposta.
    """
    if not current:
        return dict(discovered)
    merged = dict(discovered)
    if current.get("usage"):
        merged["usage"] = dict(current["usage"])
    cur_procs = current.get("processes") or {}
    procs = {}
    for pid, proc in (discovered.get("processes") or {}).items():
        proc = dict(proc)
        old = cur_procs.get(pid) or {}
        old_types = old.get("work_item_types")
        new_types = proc.get("work_item_types")
        if new_types == "discover" and isinstance(old_types, dict):
            proc["work_item_types"] = old_types
        elif isinstance(new_types, dict) and isinstance(old_types, dict):
            types = {}
            for tname, t in new_types.items():
                t = dict(t)
                prev = old_types.get(tname) or {}
                if "suggested_fields" not in t and prev.get("suggested_fields"):
                    t["suggested_fields"] = list(prev["suggested_fields"])
                    fmap = dict(t.get("fields") or {})
                    for ref in t["suggested_fields"]:
                        if ref not in fmap and ref in (prev.get("fields") or {}):
                            fmap[ref] = prev["fields"][ref]
                    t["fields"] = dict(sorted(fmap.items()))
                types[tname] = t
            proc["work_item_types"] = types
        procs[pid] = proc
    merged["processes"] = procs
    return merged


def diff_profiles(current, discovered, ignore=DIFF_IGNORED):
    """Diferenças entre dois perfis: lista de `{"path", "current", "discovered"}`.

    `path` usa `/` entre chaves e `[i]` em listas. Chave ausente aparece como `None`.
    Listas de dicts com `id` ou `name` são comparadas por essa chave, não pela posição.
    """
    out = []
    _diff(current, discovered, "", out, set(ignore))
    return out


def _keyed(items):
    for key in ("id", "name"):
        if items and all(isinstance(i, dict) and key in i for i in items):
            return key
    return None


def _diff(a, b, path, out, ignore):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if not path and k in ignore:
                continue
            p = f"{path}/{k}" if path else str(k)
            if k not in a:
                out.append({"path": p, "current": None, "discovered": b[k]})
            elif k not in b:
                out.append({"path": p, "current": a[k], "discovered": None})
            else:
                _diff(a[k], b[k], p, out, ignore)
        return
    if isinstance(a, list) and isinstance(b, list):
        key = _keyed(a) if _keyed(a) == _keyed(b) else None
        if key:
            ma = {i[key]: i for i in a}
            mb = {i[key]: i for i in b}
            if list(ma) == list(mb) or set(ma) != set(mb):
                for k in sorted(set(ma) | set(mb), key=str):
                    _diff(ma.get(k), mb.get(k), f"{path}[{key}={k}]", out, ignore)
                return
            # Mesmos itens em outra ordem: a ordem importa (estados, picklists).
            out.append({"path": path + "#order", "current": list(ma), "discovered": list(mb)})
            for k in ma:
                _diff(ma[k], mb[k], f"{path}[{key}={k}]", out, ignore)
            return
        if a != b:
            out.append({"path": path, "current": a, "discovered": b})
        return
    if a != b:
        out.append({"path": path, "current": a, "discovered": b})

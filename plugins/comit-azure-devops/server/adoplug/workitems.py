"""Leituras de work item: GET, WIQL, filhos, comentários, projetos, teams, iterações, identidade.

Nada aqui escreve no ADO. A WIQL usa POST, mas só consulta. Rotas de projeto usam o GUID
(I5). URLs devolvidas pelo ADO nunca são seguidas: o id e o GUID saem por regex e a rota é
remontada pelo `http`.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import config as cfgmod
from .adoapi import AdoApiError, call, split_url
from .http import GUID_RE, VSSPS_HOST, AdoHttp, segment

API = "7.1"
COMMENTS_API = "7.1-preview.4"
# connectionData ainda é preview: com "7.1" o ADO responde 400 (medido em 13/11/2026).
CONNECTION_API = "7.1-preview.1"
BATCH = 200
MAX_TOP = 200
DEFAULT_FIELDS = (
    "System.Id", "System.WorkItemType", "System.Title", "System.State", "System.AssignedTo",
    "Microsoft.VSTS.Scheduling.TargetDate", "System.TeamProject", "System.Parent",
)
STATE_CATEGORIES = ("Proposed", "InProgress", "Resolved", "Completed", "Removed")
OPEN_CATEGORIES = ("Proposed", "InProgress", "Resolved")
INTENT_CATEGORY = {"iniciar": "InProgress", "concluir": "Completed", "cancelar": "Removed", "reabrir": "Proposed"}
PARENT_REL = "System.LinkTypes.Hierarchy-Reverse"
CHILD_REL = "System.LinkTypes.Hierarchy-Forward"
ITEM_URL_RE = re.compile(r"/_apis/wit/workItems/(\d+)/?$", re.I)
PROJECT_URL_RE = re.compile(r"^/([0-9a-fA-F-]{36})/_apis/wit/", re.I)
ATTACHMENT_RE = re.compile(r"/_apis/wit/attachments/([0-9a-fA-F-]{36})", re.I)


class NeedsDecision(Exception):
    """Falta uma escolha da pessoa (estado ambíguo, identidade com vários candidatos)."""

    def __init__(self, reason_code: str, message: str, options: Optional[list] = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.options = options or []


# --- utilitários puros ----------------------------------------------------------------

def check_id(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise AdoApiError("INVALID_ID", "O id do work item precisa ser numérico.") from None
    if n <= 0 or str(value).strip().lstrip("+") != str(n):
        raise AdoApiError("INVALID_ID", "O id do work item precisa ser numérico.")
    return n


def check_guid(value: Any, what: str = "projeto") -> str:
    if not isinstance(value, str) or not GUID_RE.match(value):
        raise AdoApiError("INVALID_GUID", f"O id do {what} precisa ser um GUID.")
    return value.lower()


def wiql_literal(value: Any) -> str:
    """Literal de texto da WIQL, com a aspa simples dobrada."""
    s = str(value)
    if any(c in s for c in "\r\n\x00"):
        raise AdoApiError("INVALID_WIQL", "Texto com quebra de linha não entra na WIQL.")
    return "'" + s.replace("'", "''") + "'"


def item_id_from_url(url: str) -> Optional[int]:
    try:
        _, path, _ = split_url(url)
    except AdoApiError:
        return None
    m = ITEM_URL_RE.search(path)
    return int(m.group(1)) if m else None


def project_id_from_url(url: str) -> Optional[str]:
    """GUID do projeto na URL canônica do item (`/<org>/<guid>/_apis/wit/...`)."""
    try:
        _, path, _ = split_url(url)
    except AdoApiError:
        return None
    m = PROJECT_URL_RE.match(path)
    return m.group(1).lower() if m and GUID_RE.match(m.group(1)) else None


def attachment_id(url: str) -> Optional[str]:
    m = ATTACHMENT_RE.search(url or "")
    return m.group(1).lower() if m and GUID_RE.match(m.group(1)) else None


def item_api_url(org: str, item_id: int) -> str:
    """URL de relação para outro work item (dado do corpo, não destino de pedido)."""
    return f"https://dev.azure.com/{segment(org)}/_apis/wit/workItems/{int(item_id)}"


def split_tags(value: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for t in str(value or "").split(";"):
        t = t.strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out


def merge_tags(current: Any, new: Any) -> str:
    """Tags atuais + novas, sem repetir (sem diferenciar maiúsculas), juntadas com `; `."""
    extra = new if isinstance(new, (list, tuple)) else split_tags(new)
    return "; ".join(split_tags("; ".join(split_tags(current) + [str(t) for t in extra])))


def _fold(s: Any) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).casefold()


def state_for_intent(states: Sequence[Mapping[str, str]], intent: str) -> Tuple[str, str]:
    """Nome do estado pela intenção (categoria) ou pelo nome exato. Devolve (nome, categoria).

    Um estado na categoria: esse. Vários: `NeedsDecision`. Nenhum: `AdoApiError` STATE_NOT_AVAILABLE.
    """
    if not intent or not isinstance(intent, str):
        raise AdoApiError("STATE_INVALID", "Informe a intenção ou o nome do estado.")
    key = intent.strip()
    cat = INTENT_CATEGORY.get(_fold(key))
    if cat is None:
        for s in states:
            if _fold(s.get("name")) == _fold(key):
                return s["name"], s.get("category", "")
        raise AdoApiError("STATE_NOT_AVAILABLE", f"O estado '{key}' não existe neste tipo. Estados: "
                          + ", ".join(s["name"] for s in states))
    match = [s for s in states if s.get("category") == cat]
    if len(match) == 1:
        return match[0]["name"], cat
    if not match:
        raise AdoApiError("STATE_NOT_AVAILABLE", f"Este tipo não tem estado na categoria {cat}. Estados: "
                          + ", ".join(s["name"] for s in states))
    raise NeedsDecision("STATE_AMBIGUOUS", f"Há mais de um estado na categoria {cat}.",
                        [{"name": s["name"], "category": cat} for s in match])


def identity_view(value: Any) -> Any:
    """Campo de identidade do ADO reduzido a nome e uniqueName."""
    if isinstance(value, dict) and ("uniqueName" in value or "displayName" in value):
        return {"displayName": value.get("displayName"), "uniqueName": value.get("uniqueName")}
    return value


def _chunks(seq: Sequence[int], n: int) -> Iterable[Sequence[int]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


ProfileLoader = Callable[[str], Tuple[Optional[Dict[str, Any]], str]]


class WorkItemsApi:
    """Leituras de work item sobre `AdoHttp`. `profiles(org)` devolve (perfil, fonte)."""

    def __init__(self, http: AdoHttp, profiles: Optional[ProfileLoader] = None):
        self.http = http
        self.profiles: ProfileLoader = profiles or (lambda org: cfgmod.load_profile(org))

    def _get(self, org: str, path: str, query: Optional[Dict[str, Any]] = None, **kw) -> Any:
        q = {"api-version": API}
        q.update(query or {})
        return call(self.http, "GET", org, path, query=q, what="GET", **kw)

    # --- perfil e projeto -----------------------------------------------------------

    def profile(self, org: str) -> Tuple[Optional[Dict[str, Any]], str]:
        try:
            return self.profiles(org)
        except Exception:
            return None, "absent"

    def project(self, org: str, project: str) -> Dict[str, Any]:
        """{id, name, process} do projeto por GUID ou nome. Perfil primeiro; senão, GET."""
        if not project or not isinstance(project, str):
            raise AdoApiError("PROJECT_REQUIRED", "Informe o projeto.")
        prof, _ = self.profile(org)
        for p in (prof or {}).get("projects") or []:
            if project.casefold() in (p["id"].casefold(), p["name"].casefold()):
                return {"id": p["id"].lower(), "name": p["name"], "process": p.get("process")}
        j = self._get(org, "/_apis/projects/" + segment(project))
        pid = check_guid(j.get("id"))
        return {"id": pid, "name": j.get("name"), "process": None}

    def type_profile(self, org: str, project_id: str, wtype: str) -> Optional[Dict[str, Any]]:
        """Entrada do tipo no perfil, pelo processo do projeto. None quando não há."""
        prof, _ = self.profile(org)
        if not prof:
            return None
        proc = None
        for p in prof.get("projects") or []:
            if p["id"].casefold() == project_id.casefold():
                proc = p.get("process")
        types = ((prof.get("processes") or {}).get(proc) or {}).get("work_item_types")
        if not isinstance(types, dict):
            return None
        for name, t in types.items():
            if name.casefold() == str(wtype).casefold():
                return t
        return None

    def type_states(self, org: str, project_id: str, wtype: str) -> List[Dict[str, str]]:
        tp = self.type_profile(org, project_id, wtype)
        if tp and tp.get("states"):
            return [{"name": s["name"], "category": s.get("category", "")} for s in tp["states"]]
        j = self._get(org, f"/{check_guid(project_id)}/_apis/wit/workitemtypes/{segment(wtype)}/states")
        return [{"name": s["name"], "category": s.get("category", "")} for s in (j or {}).get("value") or []]

    def states_by_category(self, org: str, project_id: Optional[str], types: Sequence[str],
                           categories: Sequence[str]) -> List[str]:
        """Nomes de estado das categorias, nos tipos dados ou em todos os tipos do projeto."""
        names: List[str] = []
        if project_id and types:
            pools = [self.type_states(org, project_id, t) for t in types]
        elif project_id:
            j = self._get(org, f"/{check_guid(project_id)}/_apis/wit/workitemtypes")
            pools = [t.get("states") or [] for t in (j or {}).get("value") or []]
        else:
            raise AdoApiError("PROJECT_REQUIRED", "Filtro por categoria de estado precisa do projeto.")
        for pool in pools:
            for s in pool:
                if s.get("category") in categories and s["name"] not in names:
                    names.append(s["name"])
        return names

    # --- work item ------------------------------------------------------------------

    def get_raw(self, org: str, item_id: Any, expand: str = "relations",
                fields: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        n = check_id(item_id)
        q: Dict[str, Any] = {}
        if fields:
            q["fields"] = ",".join(fields)
        elif expand and expand != "none":
            if expand not in ("relations", "all", "fields", "links"):
                raise AdoApiError("INVALID_EXPAND", "expand precisa ser none, relations ou all.")
            q["$expand"] = expand
        return self._get(org, f"/_apis/wit/workitems/{n}", q)

    def summarize(self, org: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
        f = dict(raw.get("fields") or {})
        rels = []
        parent = None
        children = []
        for r in raw.get("relations") or []:
            rel, url = r.get("rel"), r.get("url", "")
            entry = {"rel": rel, "url": url, "attributes": dict(r.get("attributes") or {})}
            tid = item_id_from_url(url) if rel and rel.startswith("System.LinkTypes") else None
            if tid:
                entry["target_id"] = tid
                if rel == PARENT_REL:
                    parent = tid
                elif rel == CHILD_REL:
                    children.append(tid)
            rels.append(entry)
        pid = project_id_from_url(raw.get("url", ""))
        out = {
            "id": raw.get("id"),
            "rev": raw.get("rev"),
            "type": f.get("System.WorkItemType"),
            "title": f.get("System.Title"),
            "state": f.get("System.State"),
            "project_id": pid,
            "project_name": f.get("System.TeamProject"),
            "parent_id": parent if parent is not None else f.get("System.Parent"),
            "children_ids": children,
            "fields": {k: identity_view(v) for k, v in f.items()},
            "relations": rels,
        }
        if pid and raw.get("id"):
            out["web_url"] = f"https://dev.azure.com/{segment(org)}/{pid}/_workitems/edit/{int(raw['id'])}"
        if out["type"] and pid and out["state"]:
            try:
                for s in self.type_states(org, pid, out["type"]):
                    if s["name"] == out["state"]:
                        out["state_category"] = s["category"]
            except AdoApiError:
                pass
        return out

    def get(self, org: str, item_id: Any, expand: str = "relations") -> Dict[str, Any]:
        return self.summarize(org, self.get_raw(org, item_id, expand))

    def item_project(self, org: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
        """{id, name} do projeto do item: GUID da URL canônica; senão, pelo nome."""
        pid = project_id_from_url(raw.get("url", ""))
        name = (raw.get("fields") or {}).get("System.TeamProject")
        if pid:
            return {"id": pid, "name": name}
        if name:
            p = self.project(org, name)
            return {"id": p["id"], "name": p["name"]}
        raise AdoApiError("PROJECT_UNRESOLVED", "A resposta do work item não traz o projeto.")

    def get_many(self, org: str, ids: Sequence[Any], fields: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Itens em lotes de 200, na ordem dos ids. Itens apagados ficam de fora."""
        nums = [check_id(i) for i in ids]
        fl = list(fields or DEFAULT_FIELDS)
        by_id: Dict[int, Dict[str, Any]] = {}
        for chunk in _chunks(nums, BATCH):
            j = self._get(org, "/_apis/wit/workitems", {"ids": ",".join(str(i) for i in chunk),
                                                         "fields": ",".join(fl), "errorPolicy": "omit"})
            for it in (j or {}).get("value") or []:
                if it and it.get("id") is not None:
                    by_id[int(it["id"])] = it
        out = []
        for i in nums:
            it = by_id.get(i)
            if it:
                f = it.get("fields") or {}
                out.append({"id": i, "rev": it.get("rev"),
                            "fields": {k: identity_view(v) for k, v in f.items()}})
        return out

    # --- WIQL -----------------------------------------------------------------------

    def wiql_ids(self, org: str, project_id: Optional[str], wiql: str, top: int = 50) -> List[int]:
        if not isinstance(wiql, str) or not wiql.strip():
            raise AdoApiError("INVALID_WIQL", "A consulta WIQL está vazia.")
        top = max(1, min(int(top or 50), MAX_TOP))
        path = (f"/{check_guid(project_id)}" if project_id else "") + "/_apis/wit/wiql"
        j = call(self.http, "POST", org, path, query={"api-version": API, "$top": top},
                 json_body={"query": wiql}, what="WIQL")
        ids = []
        for w in (j or {}).get("workItems") or []:
            ids.append(int(w["id"]))
        for r in (j or {}).get("workItemRelations") or []:
            t = (r.get("target") or {}).get("id")
            if t is not None and int(t) not in ids:
                ids.append(int(t))
        return ids[:top]

    def build_wiql(self, org: str, project_id: Optional[str] = None, assigned_to_me: bool = False,
                   types: Optional[Sequence[str]] = None, state_category: Optional[str] = None,
                   parent: Optional[Any] = None, title: Optional[str] = None) -> str:
        cond = []
        if project_id:
            cond.append("[System.TeamProject] = @project")
        if assigned_to_me:
            cond.append("[System.AssignedTo] = @Me")
        tlist = [types] if isinstance(types, str) else list(types or [])
        if tlist:
            cond.append("[System.WorkItemType] IN (" + ", ".join(wiql_literal(t) for t in tlist) + ")")
        if state_category:
            cats = OPEN_CATEGORIES if state_category.casefold() == "open" else (state_category,)
            bad = [c for c in cats if c not in STATE_CATEGORIES]
            if bad:
                raise AdoApiError("STATE_INVALID", "Categoria de estado desconhecida. Use: "
                                  + ", ".join(STATE_CATEGORIES + ("open",)))
            names = self.states_by_category(org, project_id, tlist, cats)
            if not names:
                raise AdoApiError("STATE_NOT_AVAILABLE", "Nenhum estado nessa categoria.")
            cond.append("[System.State] IN (" + ", ".join(wiql_literal(n) for n in names) + ")")
        if parent is not None:
            cond.append(f"[System.Parent] = {check_id(parent)}")
        if title is not None:
            cond.append("[System.Title] = " + wiql_literal(title))
        where = " AND ".join(cond) if cond else "[System.Id] > 0"
        return ("SELECT [System.Id] FROM WorkItems WHERE " + where
                + " ORDER BY [Microsoft.VSTS.Scheduling.TargetDate] ASC, [System.ChangedDate] DESC")

    def query(self, org: str, project: Optional[str] = None, wiql: Optional[str] = None,
              assigned_to_me: bool = False, types: Optional[Sequence[str]] = None,
              state_category: Optional[str] = None, parent: Optional[Any] = None, top: int = 50,
              fields: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """WIQL livre ou montada a partir dos filtros; devolve ids e campos pedidos."""
        pid = self.project(org, project)["id"] if project else None
        q = wiql if wiql else self.build_wiql(org, pid, assigned_to_me, types, state_category, parent)
        ids = self.wiql_ids(org, pid, q, top)
        return {"wiql": q, "count": len(ids), "items": self.get_many(org, ids, fields) if ids else []}

    def children(self, org: str, item_id: Any, fields: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        raw = self.get_raw(org, item_id, "relations")
        ids = [item_id_from_url(r.get("url", "")) for r in raw.get("relations") or [] if r.get("rel") == CHILD_REL]
        ids = [i for i in ids if i]
        return self.get_many(org, ids, fields) if ids else []

    # --- comentários ----------------------------------------------------------------

    def comment_route(self, project_id: str, item_id: Any) -> str:
        return f"/{check_guid(project_id)}/_apis/wit/workItems/{check_id(item_id)}/comments"

    def comments(self, org: str, item_id: Any, project_id: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        """Todos os comentários (até `limit`), paginados por `continuationToken`."""
        if not project_id:
            project_id = self.item_project(org, self.get_raw(org, item_id, "none", ["System.TeamProject"]))["id"]
        route = self.comment_route(project_id, item_id)
        out: List[Dict[str, Any]] = []
        token, seen = None, set()
        while True:
            q: Dict[str, Any] = {"api-version": COMMENTS_API, "$top": MAX_TOP}
            if token:
                q["continuationToken"] = token
            j = call(self.http, "GET", org, route, query=q, what="GET de comentários") or {}
            for c in j.get("comments") or []:
                out.append({"id": c.get("id"), "text": c.get("text"), "createdDate": c.get("createdDate"),
                            "createdBy": identity_view(c.get("createdBy")), "version": c.get("version")})
            token = j.get("continuationToken")
            if not token or token in seen or len(out) >= limit:
                break
            seen.add(token)
        return out[:limit]

    def comment(self, org: str, project_id: str, item_id: Any, comment_id: Any) -> Dict[str, Any]:
        return call(self.http, "GET", org, f"{self.comment_route(project_id, item_id)}/{check_id(comment_id)}",
                    query={"api-version": COMMENTS_API}, what="GET de comentário")

    # --- org, projetos, teams, iterações --------------------------------------------

    def list_projects(self, org: str) -> List[Dict[str, Any]]:
        j = self._get(org, "/_apis/work/processes", {"$expand": "projects"})
        out = []
        for p in (j or {}).get("value") or []:
            for pr in p.get("projects") or []:
                out.append({"id": pr["id"], "name": pr["name"], "process": p.get("name"), "process_id": p.get("typeId")})
        return sorted(out, key=lambda x: x["name"].casefold())

    def list_teams(self, org: str, project: str) -> List[Dict[str, Any]]:
        pid = self.project(org, project)["id"]
        j = self._get(org, f"/_apis/projects/{pid}/teams", {"$top": 500})
        return [{"id": t["id"], "name": t["name"]} for t in (j or {}).get("value") or []]

    def team_id(self, org: str, project_id: str, team: str) -> str:
        if isinstance(team, str) and GUID_RE.match(team):
            return team.lower()
        for t in self.list_teams(org, project_id):
            if t["name"].casefold() == str(team).casefold():
                return t["id"].lower()
        raise AdoApiError("TEAM_NOT_FOUND", f"Team não encontrado: {team}.")

    def iterations(self, org: str, project: str, team: str, timeframe: str = "current") -> List[Dict[str, Any]]:
        """Iterações do team. `current` filtra no ADO; `future`, `past` e `all` filtram aqui."""
        tf = (timeframe or "current").casefold()
        if tf not in ("current", "future", "past", "all"):
            raise AdoApiError("INVALID_TIMEFRAME", "timeframe precisa ser current, future, past ou all.")
        pid = self.project(org, project)["id"]
        tid = self.team_id(org, pid, team)
        q = {"$timeframe": "current"} if tf == "current" else {}
        j = self._get(org, f"/{pid}/{tid}/_apis/work/teamsettings/iterations", q)
        out = []
        for it in (j or {}).get("value") or []:
            a = it.get("attributes") or {}
            if tf in ("current", "all") or (a.get("timeFrame") or "").casefold() == tf:
                out.append({"id": it.get("id"), "name": it.get("name"), "path": it.get("path"),
                            "start": a.get("startDate"), "finish": a.get("finishDate"), "timeframe": a.get("timeFrame")})
        return out

    # --- identidade -----------------------------------------------------------------

    def whoami(self, org: str) -> Dict[str, Any]:
        j = self._get(org, "/_apis/connectionData", {"api-version": CONNECTION_API})
        u = (j or {}).get("authenticatedUser") or {}
        acc = ((u.get("properties") or {}).get("Account") or {}).get("$value")
        return {"displayName": u.get("providerDisplayName") or u.get("customDisplayName"),
                "uniqueName": acc, "id": u.get("id")}

    def resolve_identity(self, org: str, text: str, project: Optional[str] = None,
                         team: Optional[str] = None) -> List[Dict[str, Any]]:
        """Candidatos {displayName, uniqueName, id}. Com team, busca nos membros; senão, no vssps.

        Só em memória: nada vai para o perfil nem para a config.
        """
        words = [w for w in _fold(text).split() if w]
        if not words:
            raise AdoApiError("IDENTITY_TEXT_REQUIRED", "Informe o nome ou o e-mail da pessoa.")
        cands: List[Dict[str, Any]] = []
        if team:
            if not project:
                raise AdoApiError("PROJECT_REQUIRED", "A busca no team precisa do projeto.")
            pid = self.project(org, project)["id"]
            tid = self.team_id(org, pid, team)
            j = self._get(org, f"/_apis/projects/{pid}/teams/{tid}/members", {"$top": 500})
            for m in (j or {}).get("value") or []:
                i = m.get("identity") or {}
                cands.append({"displayName": i.get("displayName"), "uniqueName": i.get("uniqueName"), "id": i.get("id")})
        else:
            j = self._get(org, "/_apis/identities", {"searchFilter": "General", "filterValue": text,
                                                       "queryMembership": "None"}, host=VSSPS_HOST)
            for i in (j or {}).get("value") or []:
                props = i.get("properties") or {}
                cands.append({"displayName": i.get("providerDisplayName") or i.get("customDisplayName"),
                              "uniqueName": (props.get("Account") or {}).get("$value")
                              or (props.get("Mail") or {}).get("$value"), "id": i.get("id")})
        out, seen = [], set()
        for c in cands:
            hay = _fold(c.get("displayName")) + " " + _fold(c.get("uniqueName"))
            if c.get("id") and all(w in hay for w in words) and c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
        return out

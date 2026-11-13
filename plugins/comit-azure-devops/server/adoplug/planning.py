"""Leituras de planejamento do time: backlog, itens da sprint, iterações e nós de iteração.

Nada aqui escreve no ADO. A WIQL usa POST, mas só consulta.

Dois formatos de caminho de iteração (medidos em 13/11/2026):
- no work item, na WIQL e na lista do time: `Projeto\\Sprint 21`;
- no nó de classificação: `\\Projeto\\Iteration\\Sprint 21`.
A rota do nó leva os segmentos depois do projeto, cada um codificado e separados por `/`.
O `backlogIteration.path` das configurações do time é relativo à raiz: vazio é a própria raiz.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .adoapi import AdoApiError, call
from .http import GUID_RE, segment
from .workitems import API, MAX_TOP, OPEN_CATEGORIES, STATE_CATEGORIES, WorkItemsApi, check_guid, wiql_literal

STATE = "System.State"
ITER = "System.IterationPath"
REMAINING = "Microsoft.VSTS.Scheduling.RemainingWork"
ORIGINAL = "Microsoft.VSTS.Scheduling.OriginalEstimate"
COMPLETED = "Microsoft.VSTS.Scheduling.CompletedWork"
ORDER_FIELDS = ("Microsoft.VSTS.Common.StackRank", "Microsoft.VSTS.Common.BacklogPriority")
ITEM_FIELDS = ("System.Id", "System.WorkItemType", "System.Title", STATE, "System.AssignedTo", ITER,
               "System.AreaPath", "System.Parent")
WORK_FIELDS = (REMAINING, ORIGINAL, COMPLETED)
DONE_CATEGORIES = ("Completed", "Removed")
NO_OWNER = "(sem responsável)"


# --- caminhos ------------------------------------------------------------------------------

def path_segments(item_path: str) -> List[str]:
    """Segmentos de `Projeto\\A\\B` depois do projeto: ["A", "B"]."""
    parts = [p for p in str(item_path or "").split("\\") if p]
    if not parts:
        raise AdoApiError("ITERATION_INVALID", "Caminho de iteração vazio.")
    return parts[1:]


def node_to_item_path(node_path: str) -> str:
    """`\\Projeto\\Iteration\\A` vira `Projeto\\A`."""
    parts = [p for p in str(node_path or "").split("\\") if p]
    if len(parts) >= 2 and parts[1].casefold() == "iteration":
        parts = [parts[0]] + parts[2:]
    return "\\".join(parts)


def node_route(project_id: str, segments: Sequence[str]) -> str:
    base = f"/{check_guid(project_id)}/_apis/wit/classificationnodes/Iterations"
    return base + "".join("/" + segment(s) for s in segments)


def parent_path(item_path: str) -> str:
    parts = [p for p in str(item_path).split("\\") if p]
    return "\\".join(parts[:-1])


def _day(v: Any) -> Optional[dt.date]:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        return None


def iso_day(d: dt.date) -> str:
    return d.isoformat() + "T00:00:00Z"


def parse_day(v: Any, what: str) -> dt.date:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
        raise AdoApiError("DATE_INVALID", f"{what} precisa estar no formato AAAA-MM-DD.")
    try:
        return dt.date.fromisoformat(v.strip())
    except ValueError:
        raise AdoApiError("DATE_INVALID", f"{what} não é uma data válida.") from None


NUM_RE = re.compile(r"^(.*?)(\d+)\s*$")


def suggest_next(iterations: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Próxima sprint pelo padrão do time: nome com o maior número + 1; datas logo depois da última.

    Duração = a da última sprint (as sprints reais variam de 12 a 14 dias: é só sugestão).
    """
    dated = [i for i in iterations if _day(i.get("start")) and _day(i.get("finish"))]
    if not dated:
        return None
    last = max(dated, key=lambda i: _day(i["finish"]))
    m = NUM_RE.match(str(last.get("name") or ""))
    if m:
        prefix = m.group(1)
        nums = [int(x.group(2)) for x in (NUM_RE.match(str(i.get("name") or "")) for i in iterations)
                if x and x.group(1) == prefix]
        name = f"{prefix}{max(nums) + 1}"
    else:
        name = f"{last.get('name')} 2"
    start = _day(last["finish"]) + dt.timedelta(days=1)
    finish = start + (_day(last["finish"]) - _day(last["start"]))
    return {"name": name, "start": start.isoformat(), "finish": finish.isoformat(),
            "parent_path": parent_path(last["path"]), "based_on": last.get("name")}


def owner_of(fields: Mapping[str, Any]) -> Dict[str, Any]:
    a = fields.get("System.AssignedTo")
    if isinstance(a, Mapping):
        return {"displayName": a.get("displayName"), "uniqueName": a.get("uniqueName")}
    return {"displayName": None, "uniqueName": None}


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# --- serviço -------------------------------------------------------------------------------

class Planning:
    def __init__(self, api: WorkItemsApi):
        self.api = api
        self.http = api.http
        self._states: Dict[Tuple[str, str], Dict[str, str]] = {}

    def _get(self, org: str, path: str, query: Optional[Dict[str, Any]] = None) -> Any:
        q = {"api-version": API}
        q.update(query or {})
        return call(self.http, "GET", org, path, query=q, what="GET")

    # time e iterações

    def team(self, org: str, project: str, team: str) -> Dict[str, Any]:
        proj = self.api.project(org, project)
        tid = self.api.team_id(org, proj["id"], team)
        return {"project": {"id": proj["id"], "name": proj["name"]}, "team_id": tid, "team": team}

    def settings(self, org: str, pid: str, tid: str) -> Dict[str, Any]:
        return self._get(org, f"/{check_guid(pid)}/{check_guid(tid, 'team')}/_apis/work/teamsettings") or {}

    def backlog_path(self, project_name: str, settings: Mapping[str, Any]) -> str:
        rel = str(((settings.get("backlogIteration") or {}).get("path")) or "")
        return project_name + ("\\" + rel.strip("\\") if rel.strip("\\") else "")

    def team_iterations(self, org: str, pid: str, tid: str) -> List[Dict[str, Any]]:
        j = self._get(org, f"/{check_guid(pid)}/{check_guid(tid, 'team')}/_apis/work/teamsettings/iterations")
        out = []
        for it in (j or {}).get("value") or []:
            a = it.get("attributes") or {}
            out.append({"id": str(it.get("id") or "").lower(), "name": it.get("name"), "path": it.get("path"),
                        "start": a.get("startDate"), "finish": a.get("finishDate"), "timeframe": a.get("timeFrame")})
        return sorted(out, key=lambda i: (_day(i["start"]) or dt.date.max, str(i["name"])))

    @staticmethod
    def find_iteration(iterations: Sequence[Mapping[str, Any]], spec: Optional[str]) -> Optional[Dict[str, Any]]:
        """Por GUID, nome ou caminho; sem `spec`, a atual."""
        if not spec:
            return next((dict(i) for i in iterations if (i.get("timeframe") or "").casefold() == "current"), None)
        s = str(spec).strip().casefold()
        for key in ("id", "path", "name"):
            hit = [i for i in iterations if str(i.get(key) or "").casefold() == s]
            if len(hit) == 1:
                return dict(hit[0])
        return None

    def node(self, org: str, pid: str, segments: Sequence[str]) -> Optional[Dict[str, Any]]:
        """Nó de iteração pelo caminho, ou None se não existe."""
        try:
            return self._get(org, node_route(pid, segments))
        except AdoApiError as e:
            if e.http_status == 404:
                return None
            raise

    def node_children(self, org: str, pid: str, segments: Sequence[str]) -> List[Dict[str, Any]]:
        try:
            j = self._get(org, node_route(pid, segments), {"$depth": "1"})
        except AdoApiError as e:
            if e.http_status == 404:
                return []
            raise
        out = []
        for c in (j or {}).get("children") or []:
            a = c.get("attributes") or {}
            out.append({"identifier": str(c.get("identifier") or "").lower(), "name": c.get("name"),
                        "path": node_to_item_path(c.get("path") or ""), "start": a.get("startDate"),
                        "finish": a.get("finishDate")})
        return out

    # estados

    def category(self, org: str, pid: str, wtype: Optional[str], state: Optional[str]) -> str:
        if not wtype or not state:
            return ""
        key = (pid, str(wtype).casefold())
        if key not in self._states:
            try:
                self._states[key] = {s["name"]: s.get("category", "") for s in self.api.type_states(org, pid, wtype)}
            except AdoApiError:
                self._states[key] = {}
        return self._states[key].get(state, "")

    def is_done(self, org: str, pid: str, wtype: Optional[str], state: Optional[str]) -> bool:
        return self.category(org, pid, wtype, state) in DONE_CATEGORIES

    # backlog

    def _category_types(self, org: str, pid: str, refs: Sequence[str]) -> List[str]:
        j = self._get(org, f"/{check_guid(pid)}/_apis/wit/workitemtypecategories")
        out: List[str] = []
        for c in (j or {}).get("value") or []:
            if c.get("referenceName") in refs:
                for t in c.get("workItemTypes") or []:
                    if t.get("name") and t["name"] not in out:
                        out.append(t["name"])
        return out

    def area_clause(self, org: str, pid: str, tid: str) -> Tuple[str, List[Dict[str, Any]]]:
        j = self._get(org, f"/{check_guid(pid)}/{check_guid(tid, 'team')}/_apis/work/teamsettings/teamfieldvalues") or {}
        ref = ((j.get("field") or {}).get("referenceName")) or "System.AreaPath"
        vals = [{"value": v.get("value"), "include_children": bool(v.get("includeChildren"))}
                for v in j.get("values") or [] if v.get("value")]
        if not vals:
            raise AdoApiError("TEAM_NO_AREA", "O time não tem área configurada.")
        exact = [v["value"] for v in vals if not v["include_children"]]
        parts = []
        if exact:
            parts.append(f"[{ref}] IN (" + ", ".join(wiql_literal(v) for v in exact) + ")")
        parts += [f"[{ref}] UNDER {wiql_literal(v['value'])}" for v in vals if v["include_children"]]
        return (parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"), vals

    def backlog(self, org: str, project: str, team: str, types: Optional[Sequence[str]] = None,
                state_category: Optional[str] = None, top: int = 100) -> Dict[str, Any]:
        t = self.team(org, project, team)
        pid, tid = t["project"]["id"], t["team_id"]
        st = self.settings(org, pid, tid)
        bpath = self.backlog_path(t["project"]["name"], st)
        area, areas = self.area_clause(org, pid, tid)
        tlist = list(types or [])
        if not tlist:
            refs = ["Microsoft.RequirementCategory"]
            if (st.get("bugsBehavior") or "").casefold() == "asrequirements":
                refs.append("Microsoft.BugCategory")
            tlist = self._category_types(org, pid, refs)
        cat = state_category or "open"
        cats = OPEN_CATEGORIES if cat.casefold() == "open" else (cat,)
        if any(c not in STATE_CATEGORIES for c in cats):
            raise AdoApiError("STATE_INVALID", "Categoria de estado desconhecida. Use: "
                              + ", ".join(STATE_CATEGORIES + ("open",)))
        names = self.api.states_by_category(org, pid, tlist, cats)
        if not names:
            raise AdoApiError("STATE_NOT_AVAILABLE", "Nenhum estado nessa categoria.")
        cond = ["[System.TeamProject] = @project", f"[{ITER}] = {wiql_literal(bpath)}", area,
                "[System.State] IN (" + ", ".join(wiql_literal(n) for n in names) + ")"]
        if tlist:
            cond.append("[System.WorkItemType] IN (" + ", ".join(wiql_literal(x) for x in tlist) + ")")
        base = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(cond)
        top = max(1, min(int(top or 100), MAX_TOP))
        ids, order = None, None
        for f in ORDER_FIELDS + (None,):
            q = base + (f" ORDER BY [{f}] ASC, [System.Id] ASC" if f else " ORDER BY [System.ChangedDate] DESC")
            try:
                ids = self.api.wiql_ids(org, pid, q, top)
                order = f or "System.ChangedDate"
                break
            except AdoApiError as e:
                if e.http_status != 400 or f is None:
                    raise
        fields = list(ITEM_FIELDS) + ([order] if order in ORDER_FIELDS else [])
        rows = self.api.get_many(org, ids, fields) if ids else []
        items = [{"id": r["id"], "type": r["fields"].get("System.WorkItemType"), "title": r["fields"].get("System.Title"),
                  "state": r["fields"].get(STATE), "assigned_to": owner_of(r["fields"]),
                  "area": r["fields"].get("System.AreaPath"), "rank": r["fields"].get(order)} for r in rows]
        return {"project": t["project"], "team": team, "backlog_iteration": bpath, "areas": areas, "types": tlist,
                "state_category": cat, "order_by": order, "count": len(items), "truncated": len(ids or []) >= top,
                "items": items}

    # itens da sprint

    def iteration_ids(self, org: str, pid: str, tid: str, iteration_id: str) -> List[Tuple[int, Optional[int]]]:
        """(id, parent) dos itens da iteração do time; parent só quando o pai está na mesma iteração."""
        j = self._get(org, f"/{check_guid(pid)}/{check_guid(tid, 'team')}/_apis/work/teamsettings/iterations/"
                      f"{check_guid(iteration_id, 'iteração')}/workitems")
        out: List[Tuple[int, Optional[int]]] = []
        seen = set()
        for r in (j or {}).get("workItemRelations") or []:
            tgt = (r.get("target") or {}).get("id")
            if tgt is None or int(tgt) in seen:
                continue
            seen.add(int(tgt))
            src = (r.get("source") or {}).get("id") if r.get("rel") else None
            out.append((int(tgt), int(src) if src is not None else None))
        return out

    def items_with_work(self, org: str, ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        try:
            return self.api.get_many(org, ids, list(ITEM_FIELDS) + list(WORK_FIELDS))
        except AdoApiError as e:
            if e.http_status != 400:  # processo sem os campos de esforço
                raise
            return self.api.get_many(org, ids, list(ITEM_FIELDS))

    def resolve_team_iteration(self, org: str, pid: str, tid: str, spec: Optional[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        iters = self.team_iterations(org, pid, tid)
        it = self.find_iteration(iters, spec)
        if it is None:
            raise AdoApiError("ITERATION_NOT_FOUND", ("Sem sprint atual no time." if not spec else
                              f"Sprint não encontrada no time: {spec}.") + " Sprints: "
                              + ", ".join(str(i["name"]) for i in iters[-8:]))
        return it, iters

    def sprint_items(self, org: str, project: str, team: str, iteration: Optional[str] = None) -> Dict[str, Any]:
        t = self.team(org, project, team)
        pid, tid = t["project"]["id"], t["team_id"]
        it, _ = self.resolve_team_iteration(org, pid, tid, iteration)
        pairs = self.iteration_ids(org, pid, tid, it["id"])
        parents = dict(pairs)
        rows = self.items_with_work(org, [i for i, _ in pairs])
        items, people, totals = [], {}, {"items": 0, "open": 0, "remaining": 0.0, "original": 0.0, "completed": 0.0}
        for r in rows:
            f = r["fields"]
            cat = self.category(org, pid, f.get("System.WorkItemType"), f.get(STATE))
            who = owner_of(f)
            row = {"id": r["id"], "rev": r.get("rev"), "type": f.get("System.WorkItemType"), "title": f.get("System.Title"),
                   "state": f.get(STATE), "state_category": cat, "assigned_to": who,
                   "parent_id": parents.get(r["id"]) or f.get("System.Parent"),
                   "remaining": _num(f.get(REMAINING)), "original": _num(f.get(ORIGINAL)),
                   "completed": _num(f.get(COMPLETED))}
            items.append(row)
            key = who["uniqueName"] or NO_OWNER
            p = people.setdefault(key, {"assigned_to": who if who["uniqueName"] else {"displayName": NO_OWNER},
                                        "items": 0, "open": 0, "remaining": 0.0, "original": 0.0, "completed": 0.0})
            for agg in (p, totals):
                agg["items"] += 1
                agg["open"] += 0 if cat in DONE_CATEGORIES else 1
                for k in ("remaining", "original", "completed"):
                    agg[k] += row[k] or 0.0
        by_person = sorted(people.values(), key=lambda p: (-p["remaining"], str(p["assigned_to"].get("displayName"))))
        return {"project": t["project"], "team": team,
                "iteration": {k: it.get(k) for k in ("id", "name", "path", "start", "finish", "timeframe")},
                "count": len(items), "items": items, "totals": totals, "totals_by_person": by_person}

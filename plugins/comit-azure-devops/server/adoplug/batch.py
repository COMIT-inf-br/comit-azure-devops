"""Escritas em lote: mover para a sprint, criar sprint, excluir, virar a sprint e vários comentários.

Cada prévia guarda uma lista de passos em `plan.steps`. O hash cobre a lista inteira.
O estado de cada passo fica em `result.steps`, fora do plano, e não muda o hash:
- `pending`: ainda não enviado, ou não processado (sem login, 429, recusa antes do envio).
- `sending`: o envio começou. Se o processo cair aqui, só a reconciliação decide.
- `done`: efeito confirmado.
- `failed`: o ADO recusou (inclusive `test /rev` e 403). Não se repete.
- `ambiguous`: TIMEOUT, rede ou 5xx. O lote para e a reconciliação procura o efeito.
- `skipped`: não enviado porque um passo-portão (criar ou associar a sprint) falhou.

O `ado_apply` numa prévia `partial` envia só os passos `pending`: um retry não duplica comentário nem
movimento. Passo-portão que falha impede os passos seguintes. Exclusão vai para a lixeira
(DELETE sem `destroy`); `destroy=true` nunca é usado. Movimento leva `test /rev`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import htmlsafe
from .adoapi import AdoApiError
from .http import JSON_PATCH
from .planning import (ITER, Planning, iso_day, node_route, node_to_item_path, owner_of, parse_day,
                       path_segments, suggest_next, _day)
from .preview import PreviewError
from .workitems import API, CHILD_REL, NeedsDecision, check_guid, check_id, item_id_from_url

MAX_ITEMS = 200
MAX_COMMENTS = 50
MAX_DEPTH = 4
ACTIONS = {
    "set_iteration": ("mover o item", "editar work items na área e na iteração"),
    "delete": ("excluir o item", "excluir work items"),
    "comment": ("comentar", "comentar em work items"),
    "create_node": ("criar a sprint", "criar iterações no projeto"),
    "associate": ("associar a sprint ao time", "administrar o time"),
}


def _env(*a, **kw):
    from .writes import envelope
    return envelope(*a, **kw)


def _err(e):
    from .writes import error_envelope
    return error_envelope(e)


def _status_for(code, default="failed"):
    from .writes import status_for
    return status_for(code, default)


class Batch:
    OPERATIONS = ("move", "create_sprint", "delete", "rollover", "comments_batch")

    def __init__(self, writes: Any):
        self.w = writes
        self.api = writes.api
        self.store = writes.store
        self.plan = Planning(writes.api)

    # --- auxiliares ----------------------------------------------------------------------------

    def _preview(self, op: str, org: str, plan: Dict[str, Any], readable: Dict[str, Any],
                 display: Dict[str, Any], na: str) -> Dict[str, Any]:
        rec = self.store.create(op, org, plan, readable, display)
        return _env("ok", "PREVIEW_READY", self.w._preview_data(rec), na)

    def _validate_moves(self, org: str, steps: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        bad = []
        for s in steps:
            if s["kind"] != "set_iteration":
                continue
            refused = self.w._validate_only(org, "PATCH", s["path"], s["patch"])
            if refused:
                bad.append({"id": s["id"], "reason_code": refused["reason_code"], "diagnostic": refused["diagnostic"]})
        return bad

    @staticmethod
    def _move_step(item_id: int, rev: Any, project_id: str, to: str) -> Dict[str, Any]:
        pid = check_guid(project_id)
        return {"kind": "set_iteration", "id": item_id, "rev": rev, "project_id": pid, "to": to,
                "path": f"/{pid}/_apis/wit/workitems/{item_id}",
                "patch": [{"op": "test", "path": "/rev", "value": rev},
                          {"op": "add", "path": "/fields/" + ITER, "value": to}]}

    def _target(self, org: str, t: Mapping[str, Any], spec: Optional[str]) -> Dict[str, Any]:
        pid, tid = t["project"]["id"], t["team_id"]
        if not spec:
            raise AdoApiError("ITERATION_REQUIRED", "Informe a sprint de destino (nome, caminho ou backlog).")
        if str(spec).strip().casefold() in ("backlog", "@backlog"):
            path = self.plan.backlog_path(t["project"]["name"], self.plan.settings(org, pid, tid))
            return {"name": "Backlog", "path": path, "in_team": True}
        iters = self.plan.team_iterations(org, pid, tid)
        it = self.plan.find_iteration(iters, spec)
        if it:
            return {**it, "in_team": True}
        if "\\" in str(spec):
            node = self.plan.node(org, pid, path_segments(str(spec)))
            if node:
                a = node.get("attributes") or {}
                return {"name": node.get("name"), "path": node_to_item_path(node.get("path") or ""),
                        "start": a.get("startDate"), "finish": a.get("finishDate"), "in_team": False,
                        "note": "A sprint existe, mas não está associada ao time."}
        raise AdoApiError("ITERATION_NOT_FOUND", f"Sprint não encontrada: {spec}. Sprints do time: "
                          + ", ".join(str(i["name"]) for i in iters[-8:]))

    def _row(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        f = raw.get("fields") or {}
        return {"id": raw.get("id"), "type": f.get("System.WorkItemType"), "title": f.get("System.Title"),
                "state": f.get("System.State"), "assigned_to": owner_of(f), "from": f.get(ITER)}

    @staticmethod
    def _children(raw: Mapping[str, Any]) -> List[int]:
        out = []
        for r in raw.get("relations") or []:
            if r.get("rel") == CHILD_REL:
                i = item_id_from_url(r.get("url", ""))
                if i:
                    out.append(i)
        return out

    # --- mover para a sprint ---------------------------------------------------------------------

    def prepare_move(self, org: str, project: str, team: str, ids: Sequence[Any], iteration: Optional[str],
                     include_children: bool = True) -> Dict[str, Any]:
        try:
            if not ids:
                raise AdoApiError("IDS_REQUIRED", "Informe ao menos um id.")
            t = self.plan.team(org, project, team)
            target = self._target(org, t, iteration)
            to = target["path"]
            queue: List[Tuple[int, Optional[int], int]] = [(check_id(i), None, 0) for i in ids]
            seen, steps, moving, staying = set(), [], [], []
            while queue:
                n, via, depth = queue.pop(0)
                if n in seen:
                    continue
                seen.add(n)
                raw = self.api.get_raw(org, n, "relations")
                pid = self.api.item_project(org, raw)["id"]
                row = self._row(raw)
                row["to"], row["via_parent"] = to, via
                if via is not None and self.plan.is_done(org, pid, row["type"], row["state"]):
                    staying.append({**row, "reason": "filha concluída: fica onde está"})
                    continue
                if str(row["from"] or "").casefold() == to.casefold():
                    staying.append({**row, "reason": "já está no destino"})
                else:
                    steps.append(self._move_step(n, raw["rev"], pid, to))
                    moving.append(row)
                if len(steps) > MAX_ITEMS:
                    raise AdoApiError("TOO_MANY_ITEMS", f"Mais de {MAX_ITEMS} itens. Divida o pedido.")
                if include_children and depth < MAX_DEPTH:
                    queue += [(c, n, depth + 1) for c in self._children(raw)]
            if not steps:
                return _env("ok", "NO_CHANGES", {"to": target, "staying": staying}, "Nada a mover.")
            bad = self._validate_moves(org, steps)
            if bad:
                return _env("needs_decision", "VALIDATION_FAILED", {"refused": bad, "moving": moving},
                            "O Azure DevOps recusou a validação de algum item. Ajuste e prepare de novo.")
            display = {"project": t["project"], "team": team, "to": target, "moving": moving, "staying": staying,
                       "count": len(steps), "include_children": include_children, "validate_only": "accepted"}
            return self._preview("move", org, {"steps": steps}, {"iteration": to, "count": len(steps)}, display,
                                 "Mostre cada item de → para e peça o OK. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- criar sprint ----------------------------------------------------------------------------

    def sprint_steps(self, org: str, t: Mapping[str, Any], iters: Sequence[Mapping[str, Any]],
                     name: Optional[str] = None, start: Optional[str] = None, finish: Optional[str] = None,
                     assign: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Passos para criar (ou só associar) a sprint e o resumo dela para a prévia."""
        pid, tid = t["project"]["id"], t["team_id"]
        sug = suggest_next(iters)
        name = (name or (sug or {}).get("name") or "").strip()
        if not name:
            raise AdoApiError("SPRINT_NAME_REQUIRED", "O time não tem sprint com datas. Informe nome e datas.")
        if any(c in name for c in "\\/:*?\"<>|#$&%+") or len(name) > 255:
            raise AdoApiError("SPRINT_NAME_INVALID", "Nome de sprint com caractere proibido.")
        parent = (sug or {}).get("parent_path") or t["project"]["name"]
        path = parent + "\\" + name
        info: Dict[str, Any] = {"name": name, "path": path, "parent_path": parent, "suggested": sug is not None,
                                "based_on": (sug or {}).get("based_on"), "team": t["team"]}
        if any(str(i.get("name") or "").casefold() == name.casefold() for i in iters):
            info["action"] = "exists"
            return [], info
        node = next((c for c in self.plan.node_children(org, pid, path_segments(parent))
                     if str(c["name"]).casefold() == name.casefold()), None)
        if node:
            info.update(action="associate", path=node["path"], start=(node["start"] or "")[:10] or None,
                        finish=(node["finish"] or "")[:10] or None,
                        note="A sprint já existe no projeto, fora do time: o plano só a associa, com as datas atuais.")
            if sug and info["start"] and info["start"] != sug["start"]:
                info["warning"] = (f"As datas atuais da sprint não seguem a última do time ({sug['based_on']}): "
                                   f"o início sugerido seria {sug['start']}. Para mudar as datas, use o Azure DevOps.")
            if not assign:
                info["action"] = "exists"
                return [], info
            return [{"kind": "associate", "gate": True, "project_id": pid, "team_id": tid, "name": name,
                     "identifier": node["identifier"],
                     "route": f"/{pid}/{tid}/_apis/work/teamsettings/iterations"}], info
        d1 = parse_day(start, "start_date") if start else (_day((sug or {}).get("start") or ""))
        d2 = parse_day(finish, "finish_date") if finish else (_day((sug or {}).get("finish") or ""))
        if not d1 or not d2:
            raise AdoApiError("SPRINT_DATES_REQUIRED", "Informe start_date e finish_date (AAAA-MM-DD).")
        if d2 < d1:
            raise AdoApiError("DATE_INVALID", "finish_date é anterior a start_date.")
        info.update(action="create", start=d1.isoformat(), finish=d2.isoformat(),
                    dates_suggested=not (start and finish))
        last = max((_day(i.get("finish") or "") for i in iters if _day(i.get("finish") or "")), default=None)
        if last and d1 <= last:
            info["warning"] = "As datas sobrepõem a última sprint do time."
        steps = [{"kind": "create_node", "gate": True, "project_id": pid, "name": name, "path": path,
                  "route": node_route(pid, path_segments(parent)),
                  "body": {"name": name, "attributes": {"startDate": iso_day(d1), "finishDate": iso_day(d2)}}}]
        if assign:
            steps.append({"kind": "associate", "gate": True, "project_id": pid, "team_id": tid, "name": name,
                          "identifier": None, "from_step": 0,
                          "route": f"/{pid}/{tid}/_apis/work/teamsettings/iterations"})
        return steps, info

    def prepare_create_sprint(self, org: str, project: str, team: str, name: Optional[str] = None,
                              start: Optional[str] = None, finish: Optional[str] = None,
                              assign: bool = True) -> Dict[str, Any]:
        try:
            t = self.plan.team(org, project, team)
            iters = self.plan.team_iterations(org, t["project"]["id"], t["team_id"])
            steps, info = self.sprint_steps(org, t, iters, name, start, finish, assign)
            if not steps:
                return _env("ok", "NO_CHANGES", {"sprint": info}, "A sprint já existe no time. Nada a criar.")
            display = {"project": t["project"], "team": team, "sprint": info,
                       "steps": [s["kind"] for s in steps], "validate_only": "not_available",
                       "note": "Criar iteração não tem validação prévia no Azure DevOps. A prévia e o OK são a trava."}
            return self._preview("create_sprint", org, {"steps": steps}, {"title": info["name"]}, display,
                                 "Mostre nome, caminho, datas e time e peça o OK. Só então chame ado_apply.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- excluir ---------------------------------------------------------------------------------

    def prepare_delete(self, org: str, item_id: Any, include_children: Optional[bool] = None) -> Dict[str, Any]:
        try:
            n = check_id(item_id)
            raw = self.api.get_raw(org, n, "relations")
            root = self._row(raw)
            order: List[Tuple[Mapping[str, Any], int]] = [(raw, 0)]
            queue, seen = [(c, 1) for c in self._children(raw)], {n}
            while queue:
                c, depth = queue.pop(0)
                if c in seen:
                    continue
                seen.add(c)
                craw = self.api.get_raw(org, c, "relations")
                order.append((craw, depth))
                if len(order) > MAX_ITEMS:
                    raise AdoApiError("TOO_MANY_ITEMS", f"Mais de {MAX_ITEMS} itens na árvore.")
                if depth < MAX_DEPTH:
                    queue += [(x, depth + 1) for x in self._children(craw)]
            kids = [{**self._row(r), "depth": d} for r, d in order[1:]]
            if kids and include_children is not True:
                raise NeedsDecision("CHILDREN_NOT_INCLUDED",
                                    f"O item {n} tem {len(kids)} filho(s). Para excluir junto, prepare de novo com "
                                    "include_children=true. Sem isso, nada é excluído.", kids)
            steps = []
            for r, _ in sorted(order, key=lambda x: -x[1]):  # filhos antes do pai
                pid = self.api.item_project(org, r)["id"]
                f = r.get("fields") or {}
                steps.append({"kind": "delete", "id": int(r["id"]), "rev": r.get("rev"), "project_id": pid,
                              "title": f.get("System.Title"), "path": f"/{pid}/_apis/wit/workitems/{int(r['id'])}"})
            title = str(root["title"] or "")
            display = {"id": n, "type": root["type"], "title": title, "state": root["state"], "children": kids,
                       "count": len(steps), "recoverable": True, "validate_only": "not_available",
                       "note": "Exclusão vai para a lixeira do projeto (Recycle bin) e pode ser restaurada lá. "
                               "O plugin nunca exclui em definitivo."}
            return self._preview("delete", org, {"steps": steps}, {"target_id": n, "title": title}, display,
                                 "Mostre o item e os filhos e peça o OK. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- virada de sprint ------------------------------------------------------------------------

    def prepare_rollover(self, org: str, project: str, team: str, from_iteration: Optional[str] = None,
                         to_iteration: Optional[str] = None, create_if_missing: bool = True) -> Dict[str, Any]:
        try:
            t = self.plan.team(org, project, team)
            pid, tid = t["project"]["id"], t["team_id"]
            frm, iters = self.plan.resolve_team_iteration(org, pid, tid, from_iteration)
            sprint_steps: List[Dict[str, Any]] = []
            to = self.plan.find_iteration(iters, to_iteration) if to_iteration else None
            if to_iteration and to is None:
                raise AdoApiError("ITERATION_NOT_FOUND", f"Sprint de destino não encontrada no time: {to_iteration}.")
            if to is None:
                end = _day(frm.get("finish") or "")
                later = [i for i in iters if end and _day(i.get("start") or "") and _day(i["start"]) > end]
                to = later[0] if later else None
            to_info: Dict[str, Any]
            if to is not None:
                to_info = {k: to.get(k) for k in ("name", "path", "start", "finish")}
                to_info["action"] = "exists"
            elif create_if_missing:
                sprint_steps, to_info = self.sprint_steps(org, t, iters)
            else:
                raise NeedsDecision("NO_NEXT_SPRINT", "O time não tem sprint depois da origem. Use "
                                    "create_if_missing=true ou informe to_iteration.", [])
            dest = to_info["path"]
            pairs = self.plan.iteration_ids(org, pid, tid, frm["id"])
            rows = self.plan.items_with_work(org, [i for i, _ in pairs])
            going, staying, moves = [], [], []
            for r in rows:
                f = r["fields"]
                row = {"id": r["id"], "type": f.get("System.WorkItemType"), "title": f.get("System.Title"),
                       "state": f.get("System.State"), "assigned_to": owner_of(f),
                       "remaining": f.get("Microsoft.VSTS.Scheduling.RemainingWork"), "parent_id": f.get("System.Parent")}
                if self.plan.is_done(org, pid, row["type"], row["state"]):
                    staying.append(row)
                else:
                    going.append(row)
                    moves.append(self._move_step(r["id"], r.get("rev"), pid, dest))
            if len(moves) > MAX_ITEMS:
                raise AdoApiError("TOO_MANY_ITEMS", f"Mais de {MAX_ITEMS} itens para mover.")
            steps = sprint_steps + moves
            if not steps:
                return _env("ok", "NO_CHANGES", {"from": frm, "to": to_info, "staying": staying},
                            "Nada a mover: tudo na sprint de origem está concluído.")
            node_exists = to_info["action"] in ("exists", "associate")
            vo = "accepted"
            if moves and node_exists:
                bad = self._validate_moves(org, moves)
                if bad:
                    return _env("needs_decision", "VALIDATION_FAILED", {"refused": bad},
                                "O Azure DevOps recusou a validação de algum item. Ajuste e prepare de novo.")
            elif moves:
                vo = "deferred"
            remaining = sum(float(x["remaining"]) for x in going if isinstance(x["remaining"], (int, float)))
            display = {"project": t["project"], "team": team,
                       "from": {k: frm.get(k) for k in ("name", "path", "start", "finish")}, "to": to_info,
                       "going": going, "staying": [{k: x[k] for k in ("id", "type", "title", "state")} for x in staying],
                       "totals": {"going": len(going), "staying": len(staying), "remaining_going": remaining},
                       "steps": [s["kind"] for s in sprint_steps] + (["set_iteration"] * len(moves) if moves else []),
                       "validate_only": vo}
            if vo == "deferred":
                display["note"] = ("A sprint de destino ainda não existe: a validação dos movimentos só roda no envio, "
                                   "com test /rev.")
            return self._preview("rollover", org, {"steps": steps}, {"iteration": dest, "count": len(moves)}, display,
                                 "Mostre origem → destino, o que vai e o que fica, e peça o OK. Só então chame ado_apply.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- vários comentários -----------------------------------------------------------------------

    def prepare_comments(self, org: str, project: Optional[str], team: Optional[str],
                         items: Sequence[Mapping[str, Any]], source_label: Optional[str] = None) -> Dict[str, Any]:
        try:
            if not items:
                raise AdoApiError("ITEMS_REQUIRED", "Informe ao menos um comentário.")
            if len(items) > MAX_COMMENTS:
                raise AdoApiError("TOO_MANY_ITEMS", f"Mais de {MAX_COMMENTS} comentários. Divida o lote.")
            steps, shown, errors = [], [], []
            for pos, it in enumerate(items):
                try:
                    plan, d = self.w.comment_parts(org, it.get("id"), it.get("text"), it.get("html"),
                                                   it.get("mentions"), project, team)
                except NeedsDecision as e:
                    raise NeedsDecision(e.reason_code, f"Item {it.get('id')} (posição {pos + 1}): {e}",
                                        [{"item_id": it.get("id"), **o} if isinstance(o, dict) else o
                                         for o in e.options]) from None
                except (AdoApiError, htmlsafe.HtmlError) as e:
                    errors.append({"position": pos + 1, "id": it.get("id"),
                                   "reason_code": getattr(e, "reason_code", "FAILED"), "message": str(e)})
                    continue
                steps.append({"kind": "comment", "id": plan["target_id"], "project_id": plan["project_id"],
                              "path": plan["path"], "api_version": plan["api_version"], "body": plan["body"]})
                shown.append({"id": d["id"], "title": d["title"], "text": d["text"], "html": d["html"],
                              "mentions": d["mentions"]})
            if errors:
                return _env("needs_decision", "ITEMS_INVALID", {"errors": errors, "valid": len(steps)},
                            "Corrija os itens com erro e prepare o lote de novo. Nada foi enviado.")
            display = {"source_label": source_label, "count": len(steps), "items": shown,
                       "validate_only": "not_available"}
            return self._preview("comments_batch", org, {"steps": steps, "source_label": source_label},
                                 {"count": len(steps)}, display,
                                 "Mostre todos os comentários e peça um OK. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- envio -----------------------------------------------------------------------------------

    def _forbidden(self, kind: str) -> str:
        what, perm = ACTIONS.get(kind, ("fazer o envio", "essa operação"))
        return (f"Sem permissão para {what} no Azure DevOps (HTTP 403). Peça ao administrador do projeto a "
                f"permissão de {perm}.")

    def _send_step(self, org: str, s: Mapping[str, Any], st: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
        k = s["kind"]
        now = self.w.clock()
        if k == "set_iteration":
            res = self.w._send(org, "PATCH", s["path"], {"api-version": API}, json_body=s["patch"],
                               content_type=JSON_PATCH)
        elif k == "delete":
            try:
                cur = self.api.get_raw(org, s["id"], "none", ["System.Title"])
            except AdoApiError as e:
                if e.http_status == 404:
                    return {"state": "done", "at": now, "note": "já estava excluído"}
                return {"state": "pending", "code": e.reason_code, "message": str(e)}
            if cur.get("rev") != s["rev"]:
                return {"state": "failed", "code": "REVISION_CHANGED",
                        "message": "O item mudou depois da prévia. Nada foi excluído."}
            res = self.w._send(org, "DELETE", s["path"], {"api-version": API})
        elif k == "comment":
            res = self.w._send(org, "POST", s["path"], {"api-version": s["api_version"]}, json_body=s["body"])
        elif k == "create_node":
            res = self.w._send(org, "POST", s["route"], {"api-version": API}, json_body=s["body"])
        elif k == "associate":
            ident = s.get("identifier") or (st.get(str(s.get("from_step"))) or {}).get("identifier")
            if not ident:
                return {"state": "skipped", "code": "NO_IDENTIFIER", "message": "A sprint não foi criada."}
            res = self.w._send(org, "POST", s["route"], {"api-version": API}, json_body={"id": ident})
        else:
            return {"state": "failed", "code": "STEP_UNKNOWN"}
        kind, j, resp, diag = res
        if kind == "ok":
            out: Dict[str, Any] = {"state": "done", "at": now}
            if k == "comment":
                out["comment_id"] = (j or {}).get("id")
            elif k == "create_node":
                out["identifier"] = str((j or {}).get("identifier") or "").lower() or None
            return out
        if kind == "not_sent" or (kind == "refused" and resp.status in (401, 203, 429)):
            code = diag.get("reason_code") or (resp.reason_code if resp is not None else None) or "NOT_SENT"
            return {"state": "pending", "code": code, "message": diag.get("message")}
        if kind == "refused":
            if resp.status == 403:
                return {"state": "failed", "code": "FORBIDDEN", "message": self._forbidden(k), "diagnostic": diag}
            if k == "create_node" and resp.status == 409:
                return {"state": "failed", "code": "SPRINT_EXISTS", "message": "Já existe sprint com esse nome.",
                        "diagnostic": diag}
            if k == "set_iteration" and self.w._is_rev_conflict(resp):
                return {"state": "failed", "code": "REVISION_CHANGED",
                        "message": "O item mudou depois da prévia. Prepare de novo.", "diagnostic": diag}
            return {"state": "failed", "code": resp.reason_code or "REFUSED", "diagnostic": diag}
        return {"state": "ambiguous", "at": now, "code": diag.get("reason_code") or diag.get("http_status"),
                "diagnostic": diag}

    def apply(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        org, steps = rec["org"], rec["plan"]["steps"]
        st = {k: dict(v) for k, v in ((rec.get("result") or {}).get("steps") or {}).items()}
        gate_failed = False
        for i, s in enumerate(steps):
            k = str(i)
            cur = (st.get(k) or {}).get("state", "pending")
            if gate_failed:
                if cur == "pending":
                    st[k] = {"state": "skipped", "code": "GATE_FAILED"}
                continue
            if cur != "pending":
                gate_failed = bool(s.get("gate")) and cur in ("failed", "skipped")
                continue
            st[k] = {"state": "sending", "at": self.w.clock()}
            self.store.transition(rec, "applying", note=f"passo {i}", steps=st)
            st[k] = self._send_step(org, s, st)
            if st[k]["state"] == "ambiguous":
                self.store.transition(rec, "ambiguous", note=f"passo {i}", steps=st)
                try:
                    st[k] = self._resolved(st[k], self._check_step(org, s, st[k]))
                except AdoApiError:
                    pass
            rec["result"]["steps"] = st
            if st[k]["state"] in ("ambiguous", "pending"):
                break
            if st[k]["state"] in ("failed", "skipped") and s.get("gate"):
                gate_failed = True
        res = self._finish(rec, st)
        if any(v.get("after") == "ambiguous_write" for v in st.values()):
            res["data"]["after"] = "ambiguous_write"
        return res

    @staticmethod
    def _resolved(cur: Mapping[str, Any], check: Mapping[str, Any]) -> Dict[str, Any]:
        out = {**cur, **check, "after": "ambiguous_write"}
        if out["state"] == "done":
            out.pop("code", None)
            out.pop("diagnostic", None)
        return out

    # --- reconciliação ---------------------------------------------------------------------------

    def _check_step(self, org: str, s: Mapping[str, Any], cur: Mapping[str, Any]) -> Dict[str, Any]:
        k = s["kind"]
        if k == "set_iteration":
            raw = self.api.get_raw(org, s["id"], "none", [ITER])
            if str((raw.get("fields") or {}).get(ITER) or "").casefold() == s["to"].casefold():
                return {"state": "done", "note": "reconcile"}
            return {"state": "pending"} if raw.get("rev") == s["rev"] else {"state": "ambiguous", "rev": raw.get("rev")}
        if k == "delete":
            try:
                raw = self.api.get_raw(org, s["id"], "none", ["System.Title"])
            except AdoApiError as e:
                if e.http_status == 404:
                    return {"state": "done", "note": "reconcile"}
                raise
            return {"state": "pending"} if raw.get("rev") == s["rev"] else {"state": "ambiguous", "rev": raw.get("rev")}
        if k == "comment":
            from .writes import CLOCK_SKEW, _ts
            since = float(cur.get("at") or 0) - CLOCK_SKEW
            hits = [c.get("id") for c in self.api.comments(org, s["id"], s["project_id"])
                    if (_ts(c.get("createdDate")) or 0) >= since
                    and htmlsafe.equivalent(c.get("text") or "", s["body"]["text"], org, s["project_id"])]
            return {"state": "done", "comment_id": hits[0], "note": "reconcile"} if hits else {"state": "pending"}
        if k == "create_node":
            node = self.plan.node(org, s["project_id"], path_segments(s["path"]))
            if node:
                return {"state": "done", "identifier": str(node.get("identifier") or "").lower(), "note": "reconcile"}
            return {"state": "pending"}
        if k == "associate":
            ident = s.get("identifier")
            iters = self.plan.team_iterations(org, s["project_id"], s["team_id"])
            if any(str(i["name"]).casefold() == str(s["name"]).casefold() or (ident and i["id"] == ident)
                   for i in iters):
                return {"state": "done", "note": "reconcile"}
            return {"state": "pending"}
        return {"state": "ambiguous"}

    def reconcile(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        org, steps = rec["org"], rec["plan"]["steps"]
        st = {k: dict(v) for k, v in ((rec.get("result") or {}).get("steps") or {}).items()}
        for i, s in enumerate(steps):
            cur = st.get(str(i)) or {}
            if cur.get("state") in ("sending", "ambiguous"):
                st[str(i)] = self._resolved(cur, self._check_step(org, s, cur))
        return self._finish(rec, st, reconciled=True)

    # --- fechamento ------------------------------------------------------------------------------

    def _readback(self, org: str, steps: Sequence[Mapping[str, Any]], st: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
        moved = {s["id"]: s["to"] for i, s in enumerate(steps)
                 if s["kind"] == "set_iteration" and (st.get(str(i)) or {}).get("state") == "done"}
        deleted = [s["id"] for i, s in enumerate(steps)
                   if s["kind"] == "delete" and (st.get(str(i)) or {}).get("state") == "done"]
        bad: List[Dict[str, Any]] = []
        try:
            if moved:
                for r in self.api.get_many(org, list(moved), ["System.Id", ITER]):
                    got = r["fields"].get(ITER)
                    if str(got or "").casefold() != moved[r["id"]].casefold():
                        bad.append({"id": r["id"], "field": ITER, "sent": moved[r["id"]], "read": got})
            if deleted:
                for r in self.api.get_many(org, deleted, ["System.Id"]):
                    bad.append({"id": r["id"], "deleted": False, "read": "ainda existe"})
        except AdoApiError as e:
            return [{"readback": "falhou", "reason_code": e.reason_code}]
        return bad

    def _finish(self, rec: Dict[str, Any], st: Dict[str, Dict[str, Any]], reconciled: bool = False) -> Dict[str, Any]:
        org, steps = rec["org"], rec["plan"]["steps"]
        states = [(st.get(str(i)) or {}).get("state", "pending") for i in range(len(steps))]
        count = {x: states.count(x) for x in ("done", "failed", "pending", "ambiguous", "sending", "skipped")}
        if count["ambiguous"] or count["sending"]:
            final = "ambiguous"
        elif count["pending"]:
            final = "partial"
        elif count["failed"] or count["skipped"]:
            final = "failed"
        else:
            final = "applied"
        self.store.transition(rec, final, note="reconcile" if reconciled else None, steps=st)
        report = []
        for i, s in enumerate(steps):
            r = st.get(str(i)) or {}
            row = {"step": i, "kind": s["kind"], "state": states[i]}
            for key in ("id", "name", "to"):
                if key in s:
                    row[key] = s[key]
            for key in ("code", "message", "comment_id", "identifier", "note"):
                if r.get(key) is not None:
                    row[key] = r[key]
            report.append(row)
        data = {"preview_id": rec["preview_id"], "operation": rec["operation"], "outcome": final,
                "counts": {k: v for k, v in count.items() if v}, "steps": report,
                "readback": {"mismatches": self._readback(org, steps, st)}}
        if final == "applied":
            return _env("ok", "APPLIED", data, "Mostre o resultado de cada passo."
                        + (" Avise as divergências da releitura." if data["readback"]["mismatches"] else ""))
        if final == "partial":
            codes = [r.get("code") for r in st.values() if r.get("state") == "pending" and r.get("code")]
            return _env(_status_for(codes[0] if codes else None), "PARTIAL", data,
                        "Parte do lote ficou pendente. Resolva o motivo e chame ado_apply de novo com os mesmos "
                        "apply_args: ele envia só os passos pendentes.")
        if final == "failed":
            forb = any(r.get("code") == "FORBIDDEN" for r in st.values())
            return _env("failed", "FORBIDDEN" if forb and not count["done"] else "PARTIAL_FAILED", data,
                        "Mostre os passos recusados e o motivo. Os passos concluídos ficaram gravados. "
                        "Para repetir os recusados, prepare de novo.")
        return _env("blocked", "AMBIGUOUS", data,
                    "Não dá para saber se algum passo aconteceu. Rode ado_reconcile antes de repetir.")

"""Impedimento de work item: marcar e desmarcar pelo padrão do perfil da organização.

O código é genérico. O bloco `blocking` do tipo no perfil diz quais campos usar (`fields`) e a
política (`policy`): responsável padrão, data, prazo de previsão, tipos e modelos de comentário.
Sem `blocking`, a org não tem padrão mapeado e a prévia devolve `needs_setup`.

A prévia é um lote de dois passos (`batch.py`): o PATCH dos campos, com `test /rev` e passo-portão,
e depois o comentário obrigatório. O PATCH nunca leva `System.State`: o item continua no estado atual.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import htmlsafe
from .adoapi import AdoApiError
from .planning import owner_of, parse_day
from .preview import PreviewError
from .rules import BLOCKED_FIELD
from .workitems import PARENT_REL, NeedsDecision, check_guid, check_id, identity_view, item_id_from_url

ROLES = ("responsible", "reason", "date", "forecast")
# Modelos neutros, para o perfil que mapeia os campos mas não define `comment_templates`.
NEUTRAL_BLOCK = "Impedimento {tipo}: {motivo}. Aguardando: {esperado}. Responsável: @{responsavel}. Previsão: {previsao}."
NEUTRAL_UNBLOCK = "Impedimento resolvido: {resolucao}."
PLACEHOLDER_RE = re.compile(r"(@?)\{([a-z_]+)\}")
STRING_MAX = 255  # limite do campo `string` do Azure DevOps


def _env(*a, **kw):
    from .writes import envelope
    return envelope(*a, **kw)


def _err(e):
    from .writes import error_envelope
    return error_envelope(e)


def br_day(d: Optional[dt.date]) -> Optional[str]:
    return d.strftime("%d/%m") if d else None


class Blocking:
    def __init__(self, batch: Any):
        self.b = batch
        self.w = batch.w
        self.api = batch.api

    # --- leitura comum -------------------------------------------------------------------

    def _today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self.w.clock()).date()

    def _day_value(self, d: dt.date) -> str:
        """Meia-noite local com o fuso da máquina: o Azure DevOps mostra o mesmo dia para a pessoa.

        `AAAA-MM-DD` sozinho vira meia-noite UTC e aparece como o dia anterior em UTC-3.
        """
        local = dt.datetime.fromtimestamp(self.w.clock()).astimezone().tzinfo
        return dt.datetime(d.year, d.month, d.day, tzinfo=local).isoformat()

    def _load(self, org: str, item_id: Any) -> Tuple[int, Dict[str, Any], Dict[str, Any], str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        n = check_id(item_id)
        raw = self.api.get_raw(org, n, "relations")
        proj = self.api.item_project(org, raw)
        pid = check_guid(proj["id"])
        wtype = (raw.get("fields") or {}).get("System.WorkItemType")
        tp = self.api.type_profile(org, pid, wtype) if wtype else None
        block = (tp or {}).get("blocking")
        return n, raw, proj, pid, tp, block if isinstance(block, dict) and (block.get("fields") or {}).get("flag") else None

    def _not_mapped(self, raw: Mapping[str, Any], tp: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        f = raw.get("fields") or {}
        has_flag = BLOCKED_FIELD in ((tp or {}).get("fields") or {}) or BLOCKED_FIELD in f
        data = {"id": raw.get("id"), "type": f.get("System.WorkItemType"), "profile": "present" if tp else "absent",
                "flag_field": BLOCKED_FIELD if has_flag else None,
                "options": ["mapear o padrão pela descoberta (ado_discover_org e ado_profile_save, com confirmação)"]
                + (["só marcar a flag com ado_prepare_update fields={" + BLOCKED_FIELD + ": 'Yes'}"] if has_flag else [])}
        return _env("needs_setup", "BLOCKING_NOT_MAPPED", data,
                    "O perfil da organização não define um padrão de impedimento para este tipo. Explique à pessoa e "
                    "ofereça as opções de data.options. Não invente campos.")

    @staticmethod
    def _fmap(tp: Mapping[str, Any]) -> Dict[str, Any]:
        return (tp or {}).get("fields") or {}

    def _resolve_person(self, org: str, text: str, pid: str, team: Optional[str]) -> Dict[str, Any]:
        cands = self.api.resolve_identity(org, text, pid, team)
        if not cands:
            raise AdoApiError("IDENTITY_NOT_FOUND", f"Ninguém encontrado para o responsável '{text}'.")
        if len(cands) > 1:
            raise NeedsDecision("IDENTITY_AMBIGUOUS", f"Mais de uma pessoa para o responsável '{text}'.", cands)
        c = cands[0]
        return {"displayName": c.get("displayName"), "uniqueName": c.get("uniqueName"), "id": c.get("id"),
                "source": "informed"}

    def _parent(self, org: str, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        pid = next((item_id_from_url(r.get("url", "")) for r in raw.get("relations") or []
                    if r.get("rel") == PARENT_REL), None) or (raw.get("fields") or {}).get("System.Parent")
        if not pid:
            return None
        praw = self.api.get_raw(org, pid, "none", ["System.Title", "System.WorkItemType", "System.State",
                                                   "System.AssignedTo"])
        pf = praw.get("fields") or {}
        return {"id": int(pid), "type": pf.get("System.WorkItemType"), "title": pf.get("System.Title"),
                "state": pf.get("System.State"), "assigned_to": pf.get("System.AssignedTo")}

    def _default_person(self, org: str, raw: Mapping[str, Any], policy: Mapping[str, Any],
                        parent: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        rule = policy.get("responsible_default") or "ask"
        if rule == "parent_assigned_to":
            if parent is None:
                raise NeedsDecision("RESPONSIBLE_REQUIRED", "O item não tem pai, então não há dono da história. "
                                    "Pergunte o nome do responsável pelo impedimento e prepare de novo com responsible.", [])
            a = parent.get("assigned_to")
            if not isinstance(a, Mapping) or not a.get("uniqueName"):
                raise NeedsDecision("RESPONSIBLE_REQUIRED", f"O pai {parent['id']} não tem responsável. Pergunte o "
                                    "nome do responsável pelo impedimento e prepare de novo com responsible.",
                                    [{"parent_id": parent["id"], "title": parent.get("title")}])
            return {**owner_of({"System.AssignedTo": a}), "id": a.get("id"), "source": "parent_assigned_to"}
        if rule == "item_assigned_to":
            a = (raw.get("fields") or {}).get("System.AssignedTo")
            if not isinstance(a, Mapping) or not a.get("uniqueName"):
                raise NeedsDecision("RESPONSIBLE_REQUIRED", "O item não tem responsável. Pergunte o nome do "
                                    "responsável pelo impedimento e prepare de novo com responsible.", [])
            return {**owner_of({"System.AssignedTo": a}), "id": a.get("id"), "source": "item_assigned_to"}
        raise NeedsDecision("RESPONSIBLE_REQUIRED", "O perfil não define o responsável padrão. Pergunte o nome do "
                            "responsável pelo impedimento e prepare de novo com responsible.", [])

    # --- comentário ------------------------------------------------------------------------

    @staticmethod
    def render(template: str, values: Mapping[str, Optional[str]], person: Optional[Mapping[str, Any]],
               mention: bool) -> str:
        """HTML do comentário: texto escapado, `@{responsavel}` vira menção (ou só o nome)."""
        out: List[str] = []
        pos = 0
        for m in PLACEHOLDER_RE.finditer(template):
            out.append(htmlsafe.escape(template[pos:m.start()]))
            at, key = m.group(1), m.group(2)
            if key == "responsavel" and person:
                if mention and at and person.get("id"):
                    out.append(htmlsafe.mention(str(person["id"]), str(person.get("displayName") or "")))
                else:
                    out.append(htmlsafe.escape(person.get("displayName") or person.get("uniqueName") or ""))
            elif values.get(key) is not None:
                out.append(htmlsafe.escape(at) + htmlsafe.escape(values[key]))
            else:
                raise NeedsDecision(f"{key.upper()}_REQUIRED", f"O modelo de comentário do perfil usa {{{key}}}, "
                                    "mas o valor não foi informado.", [])
            pos = m.end()
        out.append(htmlsafe.escape(template[pos:]))
        return "".join(out)

    def _comment_step(self, org: str, n: int, html: str, project: Optional[str], team: Optional[str]
                      ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        plan, d = self.w.comment_parts(org, n, None, html, [], project, team)
        step = {"kind": "comment", "id": plan["target_id"], "project_id": plan["project_id"], "path": plan["path"],
                "api_version": plan["api_version"], "body": plan["body"]}
        return step, {"text": d["text"], "html": d["html"]}

    @staticmethod
    def _fields_step(n: int, rev: Any, pid: str, ops: List[Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, Any]:
        return {"kind": "set_fields", "gate": True, "id": n, "rev": rev, "project_id": pid,
                "path": f"/{pid}/_apis/wit/workitems/{n}",
                "patch": [{"op": "test", "path": "/rev", "value": rev}] + ops, "fields": values}

    # --- marcar ----------------------------------------------------------------------------

    def prepare_block(self, org: str, item_id: Any, reason: str, waiting_for: Optional[str], kind: str,
                      responsible: Optional[str] = None, date: Optional[str] = None, forecast: Optional[str] = None,
                      mention_responsible: bool = True, project: Optional[str] = None,
                      team: Optional[str] = None) -> Dict[str, Any]:
        try:
            n, raw, proj, pid, tp, block = self._load(org, item_id)
            if block is None:
                return self._not_mapped(raw, tp)
            f = raw.get("fields") or {}
            bf, pol = block["fields"], block.get("policy") or {}
            fmap = self._fmap(tp)
            reason = (reason or "").strip()
            waiting = (waiting_for or "").strip() or None
            if not reason:
                raise AdoApiError("REASON_REQUIRED", "Informe o motivo do impedimento.")
            if bf.get("reason") and (fmap.get(bf["reason"]) or {}).get("type", "string") == "string" \
                    and len(reason) > STRING_MAX:
                raise AdoApiError("REASON_TOO_LONG", f"O motivo passa de {STRING_MAX} caracteres. Resuma o motivo; "
                                  "o detalhe pode ir no comentário.")
            kinds = pol.get("kinds") or {}
            kind = str(kind or "").strip().lower()
            if kinds and kind not in kinds:
                raise NeedsDecision("KIND_INVALID", f"Tipo de impedimento desconhecido: {kind or '(vazio)'}.",
                                    [{"kind": k, "label": v} for k, v in kinds.items()])
            if not kind:
                raise AdoApiError("KIND_REQUIRED", "Informe o tipo do impedimento.")
            parent = self._parent(org, raw)
            person = (self._resolve_person(org, responsible, pid, team) if responsible
                      else self._default_person(org, raw, pol, parent))
            today = self._today()
            if date:
                day = parse_day(date, "date")
            elif (pol.get("date_default") or "today") == "today":
                day = today
            else:
                raise NeedsDecision("DATE_REQUIRED", "O perfil pede a data do impedimento. Pergunte e prepare de novo.", [])
            fc: Optional[dt.date] = None
            if forecast:
                fc = parse_day(forecast, "forecast")
            elif isinstance(pol.get("forecast_days"), int):
                fc = today + dt.timedelta(days=pol["forecast_days"])
            elif bf.get("forecast"):
                raise NeedsDecision("FORECAST_REQUIRED", "O perfil não define o prazo padrão da previsão. Pergunte a "
                                    "data prevista de desimpedimento e prepare de novo com forecast.", [])
            if fc and fc < day:
                raise AdoApiError("DATE_INVALID", "A previsão é anterior à data do impedimento.")
            flag = bf["flag"]
            values: Dict[str, Any] = {flag["field"]: flag["value_on"]}
            if bf.get("responsible"):
                values[bf["responsible"]] = person["uniqueName"]
            if bf.get("reason"):
                values[bf["reason"]] = reason
            if bf.get("date"):
                values[bf["date"]] = self._day_value(day)
            if bf.get("forecast") and fc:
                values[bf["forecast"]] = self._day_value(fc)
            ops = [{"op": "add", "path": f"/fields/{ref}", "value": v} for ref, v in values.items()]
            changes = [{"field": ref, "name": (fmap.get(ref) or {}).get("name", ref),
                        "from": identity_view(f.get(ref)), "to": v} for ref, v in values.items()]
            label = kinds.get(kind, kind)
            templates = pol.get("comment_templates") or {}
            template = templates.get(kind) or templates.get("default") or NEUTRAL_BLOCK
            html = self.render(template, {"tipo": label, "motivo": reason.rstrip("."), "esperado": waiting,
                                          "previsao": br_day(fc)}, person, mention_responsible)
            comment, shown = self._comment_step(org, n, html, project, team)
            step = self._fields_step(n, raw["rev"], pid, ops, values)
            refused = self.w._validate_only(org, "PATCH", step["path"], step["patch"])
            if refused:
                refused["data"].update({"id": n, "changes": changes})
                return refused
            title = str(f.get("System.Title") or "")
            already = str(f.get(flag["field"])) == str(flag["value_on"])
            display = {
                "id": n, "type": f.get("System.WorkItemType"), "title": title, "state": f.get("System.State"),
                "state_unchanged": True, "already_blocked": already, "kind": kind, "kind_label": label,
                "parent": {k: (identity_view(v) if k == "assigned_to" else v) for k, v in parent.items()} if parent else None,
                "values": {"responsible": {k: person.get(k) for k in ("displayName", "uniqueName", "source")},
                           "date": day.strftime("%d/%m/%Y"), "forecast": fc.strftime("%d/%m/%Y") if fc else None,
                           "reason": reason, "waiting_for": waiting},
                "defaults_used": [k for k, used in (("responsible", not responsible), ("date", not date),
                                                    ("forecast", not forecast and fc is not None)) if used],
                "changes": changes, "comment": shown, "mention": bool(mention_responsible),
                "validate_only": "accepted",
                "note": "O estado do item não muda. O comentário é obrigatório e vai depois dos campos.",
            }
            return self.b._preview("block", org, {"steps": [step, comment]}, {"target_id": n, "title": title}, display,
                                   "Mostre os 4 valores (responsável, data, previsão, motivo), o que se espera e o "
                                   "comentário. Peça para confirmar ou trocar. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError, htmlsafe.HtmlError) as e:
            return _err(e)

    # --- desmarcar -------------------------------------------------------------------------

    def prepare_unblock(self, org: str, item_id: Any, resolution: str, project: Optional[str] = None,
                        team: Optional[str] = None) -> Dict[str, Any]:
        try:
            n, raw, proj, pid, tp, block = self._load(org, item_id)
            if block is None:
                return self._not_mapped(raw, tp)
            resolution = (resolution or "").strip()
            if not resolution:
                raise AdoApiError("RESOLUTION_REQUIRED", "Informe como o impedimento foi resolvido.")
            f = raw.get("fields") or {}
            bf, pol = block["fields"], block.get("policy") or {}
            fmap = self._fmap(tp)
            flag = bf["flag"]
            title = str(f.get("System.Title") or "")
            if str(f.get(flag["field"])) != str(flag["value_on"]):
                return _env("ok", "NOT_BLOCKED", {"id": n, "title": title, "flag": {flag["field"]: f.get(flag["field"])}},
                            "O item não está impedido. Nada a mudar.")
            values: Dict[str, Any] = {flag["field"]: flag["value_off"]}
            ops = [{"op": "add", "path": f"/fields/{flag['field']}", "value": flag["value_off"]}]
            for role in pol.get("clear_on_unblock") or []:
                ref = bf.get(role)
                if ref and f.get(ref) not in (None, ""):
                    values[ref] = None
                    ops.append({"op": "remove", "path": f"/fields/{ref}"})
            changes = [{"field": ref, "name": (fmap.get(ref) or {}).get("name", ref),
                        "from": identity_view(f.get(ref)), "to": v} for ref, v in values.items()]
            kept = [{"field": bf[r], "name": (fmap.get(bf[r]) or {}).get("name", bf[r]), "value": identity_view(f.get(bf[r]))}
                    for r in ROLES if bf.get(r) and bf[r] not in values]
            template = (pol.get("comment_templates") or {}).get("unblock") or NEUTRAL_UNBLOCK
            html = self.render(template, {"resolucao": resolution.rstrip(".")}, None, False)
            comment, shown = self._comment_step(org, n, html, project, team)
            step = self._fields_step(n, raw["rev"], pid, ops, values)
            refused = self.w._validate_only(org, "PATCH", step["path"], step["patch"])
            if refused:
                refused["data"].update({"id": n, "changes": changes})
                return refused
            display = {"id": n, "type": f.get("System.WorkItemType"), "title": title, "state": f.get("System.State"),
                       "state_unchanged": True, "changes": changes, "kept_as_history": kept, "comment": shown,
                       "validate_only": "accepted",
                       "note": "O estado do item não muda. Os campos do impedimento ficam como histórico, salvo os "
                               "que o perfil manda limpar."}
            return self.b._preview("unblock", org, {"steps": [step, comment]}, {"target_id": n, "title": title}, display,
                                   "Mostre a flag, os campos mantidos e o comentário de resolução e peça o OK. Só então "
                                   "chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError, htmlsafe.HtmlError) as e:
            return _err(e)

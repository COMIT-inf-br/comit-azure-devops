"""Escritas em duas fases: prepare (prévia) → apply (envio) → releitura, e reconcile.

Invariantes:
- Toda criação e toda atualização passam por `validateOnly=true` antes da prévia.
- Comentário e anexo não têm `validateOnly`: a prévia é a única trava, e nada é enviado no prepare.
- O `apply` confere `preview_id`, hash, validade e campos legíveis antes de qualquer pedido (I7).
- A atualização leva `test /rev` com a revisão lida na prévia.
- `bypassRules` nunca é usado (I8).
- TIMEOUT, falha de rede ou 5xx numa escrita é efeito ambíguo: o journal fica `ambiguous` e a
  reconciliação procura o efeito. O `http` já repete 1x em 401/203; aqui não há repetição.
- Anexo: caminho real (`resolve`), sem link para fora da pasta indicada e fora das pastas de
  credencial (I9); upload pela rota com GUID do projeto.

Todo método público devolve o envelope `{"status", "reason_code", "data", "next_action", "diagnostic"}`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import htmlsafe, paths, rules
from .adoapi import WRITE_AMBIGUOUS, AdoApiError, call
from .http import JSON_PATCH, HttpError, segment
from .preview import PreviewError, PreviewStore
from .workitems import (API, COMMENTS_API, PARENT_REL, NeedsDecision, WorkItemsApi, attachment_id, check_guid,
                        check_id, identity_view, item_api_url, item_id_from_url, merge_tags, split_tags,
                        state_for_intent, wiql_literal)

# Limite do upload simples de anexo segundo a documentação do ADO. Não medido.
ATTACH_MAX_BYTES = 130 * 1024 * 1024
CLOCK_SKEW = 300.0
FIELD_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_-]+)+$")
READONLY_FIELDS = frozenset({"System.Id", "System.Rev", "System.WorkItemType", "System.TeamProject",
                             "System.CreatedBy", "System.CreatedDate", "System.ChangedBy", "System.ChangedDate",
                             "System.AreaId", "System.IterationId", "System.Parent"})
HTML_FIELDS = frozenset({"System.Description", "Microsoft.VSTS.Common.AcceptanceCriteria",
                         "Microsoft.VSTS.TCM.ReproSteps", "System.History"})
TAGS = "System.Tags"
STATE = "System.State"
# Pastas de credencial recusadas em qualquer posição do caminho.
DENIED_DIR_NAMES = frozenset({".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".password-store",
                              ".claude", ".codex", ".snowflake", ".mozilla"})
DENIED_FILE_NAMES = frozenset({".netrc", "_netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc",
                               ".vault-token", ".claude.json", ".env"})
# Chaves e certificados privados. Recusa também `*.key` do Keynote (macOS): decisão de segurança.
DENIED_SUFFIXES = (".pem", ".key", ".p8", ".pfx", ".p12")
DENIED_NAME_PREFIXES = (".env.",)
# Pastas de credencial relativas ao HOME.
DENIED_HOME_DIRS = (
    (".config", "gh"), (".config", "gcloud"), (".config", "google-chrome"), (".config", "chromium"),
    (".config", "BraveSoftware"), (".config", "microsoft-edge"),
    ("Library", "Application Support", "Google", "Chrome"), ("Library", "Application Support", "Microsoft Edge"),
    ("Library", "Application Support", "BraveSoftware"), ("Library", "Application Support", "Firefox"),
    ("Library", "Application Support", "Arc"), ("Library", "Safari"),
    ("AppData", "Local", "Google", "Chrome"), ("AppData", "Local", "Microsoft", "Edge"),
    ("AppData", "Local", "BraveSoftware"), ("AppData", "Roaming", "Mozilla"), (".local", "share", "keyrings"), ("Library", "Keychains"),
    ("AppData", "Roaming", "Microsoft", "Credentials"), ("AppData", "Local", "Microsoft", "Credentials"),
    ("AppData", "Roaming", "Microsoft", "Protect"),
)


def envelope(status: str, reason_code: Optional[str] = None, data: Optional[Mapping[str, Any]] = None,
             next_action: Optional[str] = None, diagnostic: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {"status": status, "reason_code": reason_code, "data": dict(data or {}),
            "next_action": next_action, "diagnostic": dict(diagnostic or {})}


# Códigos do az, do cofre, do auth e do http que pedem setup (skill de configuração), não nova tentativa.
SETUP_CODES = frozenset({
    "AUTH_REJECTED", "AZ_NOT_FOUND", "AZ_NOT_LOGGED_IN", "AZ_FAILED", "AZ_TIMEOUT", "INVALID_TENANT", "INVALID_MODE",
    "ORG_NOT_CONFIGURED", "ORG_NOT_ALLOWED", "CONFIG_INVALID", "NP_LOCKED", "NP_SIGNIN", "NP_NO_ITEM",
    "NP_AMBIGUOUS", "NP_FAILED", "PAT_NOT_FOUND", "NO_OS_VAULT",
})


def status_for(code: Optional[str], default: str = "failed") -> str:
    return "needs_setup" if code in SETUP_CODES else default


def error_envelope(e: Exception, status: str = "failed") -> Dict[str, Any]:
    """Envelope de erro para `AdoApiError`, `NeedsDecision`, `PreviewError`, `HtmlError` e afins."""
    code = getattr(e, "reason_code", None) or "FAILED"
    if isinstance(e, NeedsDecision):
        return envelope("needs_decision", code, {"options": e.options}, str(e))
    diag = getattr(e, "diagnostic", None) or {}
    return envelope(status_for(code, status), code, None, str(e), diag)


_err = error_envelope


# --- conversão e comparação de valores ---------------------------------------------------

def coerce(value: Any, ftype: Optional[str]) -> Any:
    """Converte pelo tipo do campo no perfil: integer → int, double → float, boolean → bool."""
    if isinstance(value, bool) or value is None:
        return value
    try:
        if ftype == "integer":
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
                return int(value)
        elif ftype == "double":
            if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"\s*-?\d+([.,]\d+)?\s*", value)):
                return float(str(value).replace(",", "."))
        elif ftype == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    except ValueError:
        pass
    return value


def normalize_fields(type_profile: Optional[Mapping[str, Any]], fields: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(fields, Mapping):
        raise AdoApiError("FIELDS_INVALID", "fields precisa ser um objeto {campo: valor}.")
    types = (type_profile or {}).get("fields") or {}
    out = {}
    for ref, v in fields.items():
        if not isinstance(ref, str) or not FIELD_REF_RE.match(ref):
            raise AdoApiError("FIELD_INVALID", f"Nome de campo inválido: {ref!r}. Use o referenceName.")
        if ref in READONLY_FIELDS:
            raise AdoApiError("FIELD_READONLY", f"O campo {ref} não pode ser gravado por aqui.")
        if isinstance(v, (dict, list)) and ref != TAGS:
            raise AdoApiError("FIELD_INVALID", f"Valor de {ref} precisa ser texto, número ou booleano.")
        out[ref] = coerce(v, (types.get(ref) or {}).get("type"))
    return out


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    if not isinstance(v, str):
        return None
    s = v.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def value_equal(ref: str, sent: Any, got: Any, org: Optional[str] = None, project: Optional[str] = None) -> bool:
    """O valor relido equivale ao enviado? Tolerante a formato, nunca a conteúdo."""
    if ref == TAGS:
        return {t.casefold() for t in split_tags(sent)} == {t.casefold() for t in split_tags(got)}
    if isinstance(got, dict):
        g = identity_view(got)
        s = str(sent or "").casefold()
        return bool(s) and any(x and (str(x).casefold() == s or f"<{str(x).casefold()}>" in s)
                               for x in (g.get("uniqueName"), g.get("displayName")))
    if sent == got:
        return True
    if isinstance(sent, bool) or isinstance(got, bool):
        return str(sent).casefold() == str(got).casefold()
    if isinstance(sent, (int, float)) or isinstance(got, (int, float)):
        try:
            return float(sent) == float(got)
        except (TypeError, ValueError):
            return False
    if ref in HTML_FIELDS and isinstance(sent, str) and isinstance(got, str):
        return htmlsafe.equivalent(got, sent, org, project)
    ds, dg = _parse_dt(sent), _parse_dt(got)
    if ds and dg:
        if re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", str(sent)):
            return abs((dg - ds).total_seconds()) < 86400  # fuso da pessoa no ADO
        return ds == dg
    return isinstance(sent, str) and isinstance(got, str) and sent.strip() == got.strip()


def _ts(v: Any) -> Optional[float]:
    d = _parse_dt(v)
    return d.timestamp() if d else None


# --- caminho de anexo (I9) ------------------------------------------------------------------

def _within(path: Path, base: Path) -> bool:
    p = [x.casefold() for x in path.parts]
    b = [x.casefold() for x in base.parts]
    return len(p) >= len(b) and p[:len(b)] == b


def denied_dirs(home: Path, config_dir: Optional[Path] = None,
                env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """Pastas de credencial. No Windows, `APPDATA`/`LOCALAPPDATA` podem estar fora do HOME."""
    env = os.environ if env is None else env
    out = [home.joinpath(*d) for d in DENIED_HOME_DIRS]
    for var, subs in (("APPDATA", (("Microsoft", "Credentials"), ("Microsoft", "Protect"))),
                      ("LOCALAPPDATA", (("Microsoft", "Credentials"),))):
        base = env.get(var)
        if base and os.path.isabs(base):
            out.extend(Path(base).joinpath(*s) for s in subs)
    out.append(config_dir or paths.config_dir(home=home))
    real = []
    for d in out:
        real.append(d)
        try:
            rd = d.resolve()
            if rd != d:
                real.append(rd)
        except OSError:
            pass
    return real


def check_attachment_path(file_path: str, home: Optional[Path] = None,
                          config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Confere o arquivo do anexo e devolve {path (real), name, size}. Levanta `AdoApiError`."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise AdoApiError("FILE_REQUIRED", "Informe o caminho do arquivo.")
    p = Path(os.path.expanduser(file_path.strip()))
    if not p.is_absolute():
        raise AdoApiError("FILE_PATH_RELATIVE", "Use o caminho absoluto do arquivo.")
    given = Path(os.path.abspath(p))
    try:
        real = p.resolve(strict=True)
        folder = given.parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise AdoApiError("FILE_NOT_FOUND", f"Arquivo não encontrado: {given}") from None
    except (OSError, RuntimeError):
        raise AdoApiError("FILE_UNREADABLE", f"Não consegui resolver o caminho: {given}") from None
    if real.parent != folder:
        raise AdoApiError("FILE_SYMLINK_OUTSIDE",
                          f"O arquivo é um link para fora da pasta indicada. Caminho real: {real}")
    h = Path(home) if home is not None else Path.home()
    for cand in (given, real):
        names = {x.casefold() for x in cand.parts[:-1]}
        low = cand.name.casefold()
        if (names & DENIED_DIR_NAMES or low in DENIED_FILE_NAMES or low.endswith(DENIED_SUFFIXES)
                or low.startswith(DENIED_NAME_PREFIXES)):
            raise AdoApiError("FILE_DENIED", f"Arquivo em pasta de credencial, recusado: {real}")
        for d in denied_dirs(h, config_dir):
            if _within(cand, d):
                raise AdoApiError("FILE_DENIED", f"Arquivo em pasta de credencial, recusado: {real}")
    try:
        st = os.stat(real)
    except OSError:
        raise AdoApiError("FILE_UNREADABLE", f"Não consegui ler o arquivo: {real}") from None
    if not stat.S_ISREG(st.st_mode):
        raise AdoApiError("FILE_NOT_REGULAR", f"Não é um arquivo comum: {real}")
    if st.st_size > ATTACH_MAX_BYTES:
        raise AdoApiError("FILE_TOO_LARGE", f"Arquivo com {st.st_size} bytes, acima do limite de {ATTACH_MAX_BYTES}.")
    return {"path": str(real), "name": real.name, "size": st.st_size}


def read_attachment(info: Mapping[str, Any], home: Optional[Path] = None,
                    config_dir: Optional[Path] = None) -> Tuple[Dict[str, Any], bytes]:
    """Confere o caminho de novo, lê os bytes e calcula o SHA-256 sobre eles."""
    cur = check_attachment_path(info["path"], home, config_dir)
    try:
        with open(cur["path"], "rb") as f:
            data = f.read(ATTACH_MAX_BYTES + 1)
    except OSError:
        raise AdoApiError("FILE_UNREADABLE", f"Não consegui ler o arquivo: {cur['path']}") from None
    if len(data) > ATTACH_MAX_BYTES:
        raise AdoApiError("FILE_TOO_LARGE", "Arquivo acima do limite.")
    cur["size"] = len(data)
    cur["sha256"] = hashlib.sha256(data).hexdigest()
    return cur, data


# --- serviço ----------------------------------------------------------------------------

class Writes:
    """Prepare, apply e reconcile de create, update, comment e attach."""

    def __init__(self, api: WorkItemsApi, store: PreviewStore, cfg: Optional[Mapping[str, Any]] = None,
                 clock: Callable[[], float] = time.time, home: Optional[Path] = None,
                 config_dir: Optional[Path] = None):
        self.api = api
        self.http = api.http
        self.store = store
        self.cfg = cfg or {}
        self.clock = clock
        self.home = home
        self.config_dir = config_dir
        # apply e reconcile da mesma sessão não correm em paralelo (o journal é lido e regravado).
        self._lock = threading.RLock()

    # --- auxiliares -------------------------------------------------------------------

    def _org_defaults(self, org: str, project_id: str) -> Mapping[str, Any]:
        for name, entry in (self.cfg.get("orgs") or {}).items():
            if name.casefold() == org.casefold():
                d = (entry.get("defaults") or {}) if isinstance(entry, Mapping) else {}
                for k, v in d.items():
                    if str(k).casefold() == project_id.casefold() and isinstance(v, Mapping):
                        return v
        return {}

    def _send(self, org: str, method: str, path: str, query: Dict[str, Any], *, json_body: Any = None,
              data: Optional[bytes] = None, content_type: Optional[str] = None):
        """Envia uma escrita. Devolve (tipo, json, resposta ou None, diagnóstico).

        tipo: ok | refused (ADO recusou, sem efeito) | not_sent (recusado antes do envio) |
        ambiguous (TIMEOUT, rede ou 5xx: o efeito pode ter acontecido).
        """
        try:
            r = self.http.request(method, org, path, query=query, json_body=json_body, data=data,
                                  content_type=content_type)
        except HttpError as e:
            kind = "ambiguous" if e.reason_code in WRITE_AMBIGUOUS else "not_sent"
            return kind, None, None, {"reason_code": e.reason_code, "message": str(e)}
        if r.ok:
            try:
                return "ok", r.json(), r, {}
            except ValueError:
                return "ok", None, r, {}
        if r.status >= 500:
            return "ambiguous", None, r, r.diagnostic()
        return "refused", None, r, r.diagnostic()

    def _validate_only(self, org: str, method: str, path: str, patch: list) -> Optional[Dict[str, Any]]:
        """POST/PATCH com validateOnly=true. Devolve None se o ADO aceitou; senão, o envelope de recusa."""
        q = {"api-version": API, "validateOnly": "true"}
        try:
            call(self.http, method, org, path, query=q, json_body=patch, content_type=JSON_PATCH, what="validateOnly")
        except AdoApiError as e:
            if e.http_status == 400:
                return envelope("needs_decision", "VALIDATION_FAILED", {"validate_only": "rejected"},
                                "O Azure DevOps recusou a validação. Ajuste os campos e prepare de novo.",
                                e.diagnostic)
            return _err(e)
        return None

    @staticmethod
    def _is_rev_conflict(resp) -> bool:
        if resp is None:
            return False
        if resp.status in (409, 412):
            return True
        msg = str(resp.diagnostic().get("message") or "")
        return "TF26071" in msg or ("test" in msg.lower() and "rev" in msg.lower())

    def _duplicates(self, org: str, project_id: str, wtype: str, title: str, parent_id: Optional[int]) -> list:
        try:
            q = self.api.build_wiql(org, project_id, types=[wtype], parent=parent_id, title=title)
            ids = self.api.wiql_ids(org, project_id, q, top=20)
            if not ids:
                return []
            return [{"id": i["id"], "title": i["fields"].get("System.Title"), "state": i["fields"].get(STATE)}
                    for i in self.api.get_many(org, ids, ["System.Id", "System.Title", STATE])]
        except AdoApiError:
            return []

    def _apply_args(self, rec: Mapping[str, Any]) -> Dict[str, Any]:
        out = {"preview_id": rec["preview_id"], "hash": rec["hash"]}
        out.update(rec["readable"])
        return out

    def _preview_data(self, rec: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
        d = {"preview_id": rec["preview_id"], "hash": rec["hash"], "expires_at": rec["expires_at"],
             "operation": rec["operation"], "org": rec["org"], "apply_args": self._apply_args(rec)}
        d.update(rec.get("display") or {})
        d.update(extra)
        return d

    @staticmethod
    def _hyperlinks(items: Optional[Sequence[Any]]) -> List[Dict[str, str]]:
        out = []
        for h in items or []:
            url, comment = (h, "") if isinstance(h, str) else (h.get("url"), h.get("comment") or "")
            if not htmlsafe.is_http_url(url or ""):
                raise AdoApiError("HYPERLINK_INVALID", "Hyperlink precisa ser http ou https.")
            out.append({"url": url, "comment": str(comment)})
        return out

    @staticmethod
    def _rel_op(rel: str, url: str, comment: str = "") -> Dict[str, Any]:
        v: Dict[str, Any] = {"rel": rel, "url": url}
        if comment:
            v["attributes"] = {"comment": comment}
        return {"op": "add", "path": "/relations/-", "value": v}

    # --- create -----------------------------------------------------------------------

    def prepare_create(self, org: str, project: str, wtype: str, fields: Mapping[str, Any],
                       parent_id: Optional[Any] = None, hyperlinks: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
        try:
            if not isinstance(wtype, str) or not wtype.strip():
                raise AdoApiError("TYPE_REQUIRED", "Informe o tipo do work item.")
            proj = self.api.project(org, project)
            pid = check_guid(proj["id"])
            tp = self.api.type_profile(org, pid, wtype)
            flds = normalize_fields(tp, fields or {})
            flds = {k: v for k, v in flds.items() if v is not None}
            if TAGS in flds:
                flds[TAGS] = merge_tags("", flds[TAGS])
            parent = None
            if parent_id is not None:
                praw = self.api.get_raw(org, parent_id, "none", [
                    "System.Title", STATE, "System.WorkItemType", "System.AreaPath", "System.IterationPath",
                    "System.TeamProject", "Microsoft.VSTS.Scheduling.RemainingWork"])
                pf = praw.get("fields") or {}
                pproj = self.api.item_project(org, praw)
                parent = {"id": check_id(parent_id), "title": pf.get("System.Title"), "state": pf.get(STATE),
                          "type": pf.get("System.WorkItemType"), "project_id": pproj["id"]}
                if pproj["id"] == pid:
                    for ref in ("System.AreaPath", "System.IterationPath"):
                        if ref not in flds and pf.get(ref):
                            flds[ref] = pf[ref]
            conf = self._org_defaults(org, pid)
            for ref, v in (conf.get("fields") or {}).items():
                if FIELD_REF_RE.match(str(ref)) and ref not in flds:
                    flds[ref] = v
            if "System.AreaPath" not in flds:
                flds["System.AreaPath"] = conf.get("area_path") or proj["name"]
            if "System.IterationPath" not in flds:
                flds["System.IterationPath"] = proj["name"]
            if tp:
                flds = normalize_fields(tp, rules.effective_fields(tp, flds))
            if tp:
                missing = rules.missing_required(tp, flds)
            else:
                missing = [] if flds.get("System.Title") else ["System.Title"]
            bad = rules.invalid_picklist_values(tp, flds) if tp else {}
            if missing or bad:
                picks = (tp or {}).get("picklists") or {}
                names = (tp or {}).get("fields") or {}
                return envelope("needs_decision", "MISSING_FIELDS", {
                    "missing": [{"field": f, "name": (names.get(f) or {}).get("name", f),
                                 "options": picks.get(f)} for f in missing],
                    "invalid": [{"field": f, "value": v, "options": picks.get(f)} for f, v in bad.items()],
                    "fields": flds, "parent": parent,
                }, "Pergunte à pessoa os campos que faltam e prepare de novo.")
            links = self._hyperlinks(hyperlinks)
            patch: List[Dict[str, Any]] = [{"op": "add", "path": f"/fields/{ref}", "value": flds[ref]}
                                           for ref in sorted(flds)]
            if parent:
                patch.append(self._rel_op(PARENT_REL, item_api_url(org, parent["id"])))
            for h in links:
                patch.append(self._rel_op("Hyperlink", h["url"], h["comment"]))
            title = str(flds["System.Title"])
            path = f"/{pid}/_apis/wit/workitems/$" + segment(wtype)
            refused = self._validate_only(org, "POST", path, patch)
            if refused:
                refused["data"].update({"fields": flds, "parent": parent})
                return refused
            dups = self._duplicates(org, pid, wtype, title, parent["id"] if parent else None)
            plan = {"project_id": pid, "type": wtype, "title": title, "path": path, "patch": patch,
                    "parent_id": parent["id"] if parent else None}
            display = {"project": {"id": pid, "name": proj["name"]}, "type": wtype, "title": title,
                       "fields": flds, "parent": parent, "hyperlinks": links, "duplicates": dups,
                       "profile": "present" if tp else "absent", "validate_only": "accepted"}
            rec = self.store.create("create", org, plan, {"type": wtype, "title": title}, display)
            na = "Mostre a prévia e peça o OK. Só então chame ado_apply com apply_args."
            if dups:
                na = "Há item com o mesmo título. Mostre as duplicatas antes de pedir o OK. " + na
            return envelope("ok", "PREVIEW_READY", self._preview_data(rec), na)
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    # --- update -----------------------------------------------------------------------

    def prepare_update(self, org: str, item_id: Any, fields: Optional[Mapping[str, Any]] = None,
                       state_intent: Optional[str] = None, add_parent: Optional[Any] = None,
                       add_hyperlinks: Optional[Sequence[Any]] = None,
                       remove_relations: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
        try:
            n = check_id(item_id)
            raw = self.api.get_raw(org, n, "relations")
            cur = raw.get("fields") or {}
            proj = self.api.item_project(org, raw)
            pid = check_guid(proj["id"])
            wtype = cur.get("System.WorkItemType")
            tp = self.api.type_profile(org, pid, wtype) if wtype else None
            flds = normalize_fields(tp, fields or {})
            changes: List[Dict[str, Any]] = []
            if state_intent is not None:
                if STATE in flds:
                    raise AdoApiError("STATE_CONFLICT", "Use state_intent ou System.State, não os dois.")
                flds[STATE] = state_intent
            if STATE in flds and flds[STATE] is not None:
                name, cat = state_for_intent(self.api.type_states(org, pid, wtype), str(flds[STATE]))
                flds[STATE] = name
            if TAGS in flds and flds[TAGS] is not None:
                flds[TAGS] = merge_tags(cur.get(TAGS), flds[TAGS])
            ops: List[Dict[str, Any]] = [{"op": "test", "path": "/rev", "value": raw["rev"]}]
            for ref in sorted(flds):
                v, old = flds[ref], cur.get(ref)
                if v is None:
                    if old not in (None, ""):
                        ops.append({"op": "remove", "path": f"/fields/{ref}"})
                        changes.append({"field": ref, "from": identity_view(old), "to": None})
                    continue
                if old is not None and value_equal(ref, v, old, org, pid):
                    continue
                ops.append({"op": "add", "path": f"/fields/{ref}", "value": v})
                changes.append({"field": ref, "from": identity_view(old), "to": v})
            rels = raw.get("relations") or []
            removed: List[Dict[str, Any]] = []
            idx: List[int] = []
            for spec in remove_relations or []:
                found = self._find_relation(rels, spec)
                if found is None:
                    raise AdoApiError("RELATION_NOT_FOUND", f"Relação não encontrada no item: {spec!r}")
                if found not in idx:
                    idx.append(found)
                    removed.append({"rel": rels[found].get("rel"), "url": rels[found].get("url")})
            for i in sorted(idx, reverse=True):
                ops.append({"op": "remove", "path": f"/relations/{i}"})
            parent_info = None
            if add_parent is not None:
                p = check_id(add_parent)
                if p == n:
                    raise AdoApiError("PARENT_INVALID", "O item não pode ser parent de si mesmo.")
                current_parent = [i for i, r in enumerate(rels) if r.get("rel") == PARENT_REL]
                same = [i for i in current_parent if self._rel_target(rels[i]) == p]
                if same and same[0] not in idx:
                    changes.append({"relation": PARENT_REL, "target_id": p, "note": "já é o parent"})
                else:
                    keep = [i for i in current_parent if i not in idx]
                    if keep:
                        raise NeedsDecision("PARENT_EXISTS", "O item já tem parent. Remova o atual com "
                                            "remove_relations antes de vincular outro.",
                                            [{"parent_id": self._rel_target(rels[i])} for i in keep])
                    praw = self.api.get_raw(org, p, "none", ["System.Title", STATE, "System.WorkItemType",
                                                             "Microsoft.VSTS.Scheduling.RemainingWork"])
                    pf = praw.get("fields") or {}
                    parent_info = {"id": p, "title": pf.get("System.Title"), "state": pf.get(STATE),
                                   "type": pf.get("System.WorkItemType"),
                                   "remaining_work": pf.get("Microsoft.VSTS.Scheduling.RemainingWork")}
                    ops.append(self._rel_op(PARENT_REL, item_api_url(org, p)))
                    changes.append({"relation": PARENT_REL, "target_id": p})
            for h in self._hyperlinks(add_hyperlinks):
                if any(r.get("rel") == "Hyperlink" and r.get("url") == h["url"] for r in rels):
                    changes.append({"relation": "Hyperlink", "url": h["url"], "note": "já existe"})
                    continue
                ops.append(self._rel_op("Hyperlink", h["url"], h["comment"]))
                changes.append({"relation": "Hyperlink", "url": h["url"], "comment": h["comment"]})
            title = str(cur.get("System.Title") or "")
            if len(ops) == 1:
                return envelope("ok", "NO_CHANGES", {"id": n, "title": title, "changes": changes},
                                "Nada a mudar: os valores já são esses.")
            path = f"/{pid}/_apis/wit/workitems/{n}"
            refused = self._validate_only(org, "PATCH", path, ops)
            if refused:
                refused["data"].update({"id": n, "changes": changes})
                return refused
            plan = {"project_id": pid, "target_id": n, "rev": raw["rev"], "path": path, "patch": ops}
            display = {"id": n, "title": title, "type": wtype, "state": cur.get(STATE), "rev": raw["rev"],
                       "project": proj, "changes": changes, "removed_relations": removed,
                       "parent": parent_info, "validate_only": "accepted"}
            rec = self.store.create("update", org, plan, {"target_id": n, "title": title}, display)
            return envelope("ok", "PREVIEW_READY", self._preview_data(rec),
                            "Mostre a prévia e peça o OK. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError) as e:
            return _err(e)

    @staticmethod
    def _rel_target(r: Mapping[str, Any]) -> Optional[int]:
        return item_id_from_url(r.get("url", ""))

    @staticmethod
    def _same_target(read_url: str, sent_url: str) -> bool:
        """Mesmo alvo de relação. O ADO devolve link de item e de anexo na forma do projeto."""
        if read_url == sent_url:
            return True
        a, b = attachment_id(sent_url), attachment_id(read_url)
        if a or b:
            return a == b
        i, j = item_id_from_url(sent_url), item_id_from_url(read_url)
        return i is not None and i == j

    def _find_relation(self, rels: Sequence[Mapping[str, Any]], spec: Any) -> Optional[int]:
        """Índice da relação por URL, por {rel, url}, por {rel, target_id} ou por id de anexo."""
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, Mapping):
            raise AdoApiError("RELATION_INVALID", "remove_relations aceita URL ou {rel, url|target_id}.")
        rel, url, tid = spec.get("rel"), spec.get("url"), spec.get("target_id")
        att = attachment_id(url) if url else None
        for i, r in enumerate(rels):
            if rel and r.get("rel") != rel:
                continue
            if tid is not None and self._rel_target(r) == check_id(tid):
                return i
            if url and (r.get("url") == url or (att and attachment_id(r.get("url", "")) == att)):
                return i
        return None

    # --- comment ----------------------------------------------------------------------

    def prepare_comment(self, org: str, item_id: Any, text: Optional[str] = None, html: Optional[str] = None,
                        mentions: Optional[Sequence[Any]] = None, project: Optional[str] = None,
                        team: Optional[str] = None) -> Dict[str, Any]:
        """`text` (escapado) ou `html` (lista fechada). Menção: {id, displayName} já resolvida ou texto de busca."""
        try:
            if (text is None) == (html is None):
                raise AdoApiError("COMMENT_BODY", "Informe text ou html, um dos dois.")
            body = htmlsafe.sanitize(html) if html is not None else htmlsafe.text_to_html(text or "")
            if not htmlsafe.to_text(body):
                raise AdoApiError("COMMENT_EMPTY", "O comentário está vazio.")
            n = check_id(item_id)
            raw = self.api.get_raw(org, n, "none", ["System.Title", "System.TeamProject", "System.WorkItemType"])
            proj = self.api.item_project(org, raw)
            pid = check_guid(proj["id"])
            resolved = []
            for m in mentions or []:
                if isinstance(m, Mapping) and m.get("id"):
                    resolved.append({"id": str(m["id"]), "displayName": str(m.get("displayName") or "")})
                    continue
                cands = self.api.resolve_identity(org, str(m), project or pid, team)
                if not cands:
                    raise AdoApiError("IDENTITY_NOT_FOUND", f"Ninguém encontrado para a menção '{m}'.")
                if len(cands) > 1:
                    raise NeedsDecision("IDENTITY_AMBIGUOUS", f"Mais de uma pessoa para a menção '{m}'.", cands)
                resolved.append({"id": cands[0]["id"], "displayName": cands[0]["displayName"] or ""})
            head = " ".join(htmlsafe.mention(r["id"], r["displayName"]) for r in resolved)
            final = (head + "<br>" + body) if head else body
            title = str((raw.get("fields") or {}).get("System.Title") or "")
            route = self.api.comment_route(pid, n)
            plan = {"project_id": pid, "target_id": n, "path": route, "api_version": COMMENTS_API,
                    "body": {"text": final}}
            display = {"id": n, "title": title, "project": proj, "html": final, "text": htmlsafe.to_text(final),
                       "mentions": [r["displayName"] for r in resolved]}
            rec = self.store.create("comment", org, plan, {"target_id": n, "title": title}, display)
            return envelope("ok", "PREVIEW_READY", self._preview_data(rec),
                            "Mostre o texto e o HTML e peça o OK. Só então chame ado_apply com apply_args.")
        except (AdoApiError, NeedsDecision, PreviewError, htmlsafe.HtmlError) as e:
            return _err(e)

    # --- attach -----------------------------------------------------------------------

    def prepare_attach(self, org: str, item_id: Any, file_path: str, comment: Optional[str] = None) -> Dict[str, Any]:
        """Confere o arquivo e o item. Nada é enviado: o upload só roda no apply."""
        try:
            info, _ = read_attachment({"path": file_path}, self.home, self.config_dir)
            n = check_id(item_id)
            raw = self.api.get_raw(org, n, "none", ["System.Title", "System.TeamProject"])
            proj = self.api.item_project(org, raw)
            pid = check_guid(proj["id"])
            title = str((raw.get("fields") or {}).get("System.Title") or "")
            plan = {"project_id": pid, "target_id": n, "rev": raw.get("rev"), "file": info,
                    "comment": str(comment or "")}
            display = {"id": n, "title": title, "project": proj, "file": info, "relation": "AttachedFile",
                       "relation_comment": str(comment or ""), "requested_path": file_path}
            rec = self.store.create("attach", org, plan, {"target_id": n, "file_name": info["name"]}, display)
            return envelope("ok", "PREVIEW_READY", self._preview_data(rec),
                            "Mostre o caminho real, o tamanho e o SHA-256 e peça o OK. Só então chame ado_apply.")
        except (AdoApiError, PreviewError) as e:
            return _err(e)

    # --- apply ------------------------------------------------------------------------

    def apply(self, preview_id: str, hash: str, operation: str, org: str, target_id: Optional[Any] = None,  # noqa: A002
              type: Optional[str] = None, title: Optional[str] = None,  # noqa: A002
              file_name: Optional[str] = None) -> Dict[str, Any]:
        claims = {"operation": operation, "org": org, "target_id": target_id, "type": type, "title": title,
                  "file_name": file_name}
        with self._lock:
            return self._apply(preview_id, hash, claims)

    def _apply(self, preview_id: str, hash: str, claims: Mapping[str, Any]) -> Dict[str, Any]:  # noqa: A002
        try:
            rec = self.store.check_apply(preview_id, hash, claims)
        except PreviewError as e:
            return envelope("blocked", e.reason_code, {"preview_id": preview_id},
                            "Nada foi enviado. " + str(e))
        op = rec["operation"]
        try:
            if op == "create":
                return self._apply_create(rec)
            if op == "update":
                return self._apply_update(rec)
            if op == "comment":
                return self._apply_comment(rec)
            return self._apply_attach(rec)
        except (AdoApiError, PreviewError) as e:
            return _err(e)

    def _outcome(self, rec: Dict[str, Any], kind: str, resp, diag: Mapping[str, Any], what: str) -> Dict[str, Any]:
        """Estado e envelope para um envio sem sucesso."""
        if kind == "not_sent":
            self.store.transition(rec, "prepared", note=diag.get("reason_code"))
            return envelope(status_for(diag.get("reason_code")), diag.get("reason_code") or "NOT_SENT",
                            {"preview_id": rec["preview_id"]},
                            f"O {what} não foi enviado. Resolva o motivo e chame ado_apply de novo.", diag)
        if kind == "refused" and resp.status in (401, 203, 429):
            # O ADO não processou o pedido: a prévia continua válida para nova tentativa.
            self.store.transition(rec, "prepared", note=resp.reason_code)
            st = "needs_setup" if resp.status in (401, 203) else "failed"
            return envelope(st, resp.reason_code, {"preview_id": rec["preview_id"]},
                            f"O {what} não foi processado. Resolva o login ou espere e chame ado_apply de novo.", diag)
        if kind == "refused":
            code = "REVISION_CHANGED" if self._is_rev_conflict(resp) else (resp.reason_code or "REFUSED")
            self.store.transition(rec, "failed", note=code, http_status=resp.status)
            na = ("O item mudou depois da prévia. Prepare de novo." if code == "REVISION_CHANGED"
                  else f"O Azure DevOps recusou o {what}. Prepare de novo.")
            return envelope("failed", code, {"preview_id": rec["preview_id"]}, na, diag)
        self.store.transition(rec, "ambiguous", note=diag.get("reason_code") or diag.get("http_status"))
        res = self.reconcile(rec["preview_id"])
        res["data"]["after"] = "ambiguous_write"
        res["diagnostic"] = dict(diag)
        return res

    def _apply_create(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        plan, org = rec["plan"], rec["org"]
        self.store.transition(rec, "applying")
        kind, j, resp, diag = self._send(org, "POST", plan["path"], {"api-version": API},
                                         json_body=plan["patch"], content_type=JSON_PATCH)
        if kind != "ok":
            return self._outcome(rec, kind, resp, diag, "work item")
        new_id = int((j or {}).get("id") or 0)
        self.store.transition(rec, "applied", item_id=new_id)
        return self._readback_create(rec, new_id)

    def _apply_update(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        plan, org = rec["plan"], rec["org"]
        self.store.transition(rec, "applying")
        kind, j, resp, diag = self._send(org, "PATCH", plan["path"], {"api-version": API},
                                         json_body=plan["patch"], content_type=JSON_PATCH)
        if kind != "ok":
            return self._outcome(rec, kind, resp, diag, "update")
        self.store.transition(rec, "applied", item_id=plan["target_id"], rev=(j or {}).get("rev"))
        return self._readback_update(rec)

    def _apply_comment(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        plan, org = rec["plan"], rec["org"]
        self.store.transition(rec, "applying")
        kind, j, resp, diag = self._send(org, "POST", plan["path"], {"api-version": plan["api_version"]},
                                         json_body=plan["body"])
        if kind != "ok":
            return self._outcome(rec, kind, resp, diag, "comentário")
        cid = (j or {}).get("id")
        self.store.transition(rec, "applied", comment_id=cid)
        return self._readback_comment(rec, cid)

    def _apply_attach(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        plan, org = rec["plan"], rec["org"]
        upload_url = (rec.get("result") or {}).get("upload_url") if rec["state"] == "partial" else None
        if not upload_url:
            cur, data = read_attachment(plan["file"], self.home, self.config_dir)
            if (cur["path"], cur["size"], cur["sha256"]) != (plan["file"]["path"], plan["file"]["size"],
                                                            plan["file"]["sha256"]):
                return envelope("blocked", "FILE_CHANGED", {"preview_id": rec["preview_id"], "file": cur},
                                "Nada foi enviado: o arquivo mudou depois da prévia. Prepare de novo.")
            now = self.api.get_raw(org, plan["target_id"], "none", ["System.Title"])
            if now.get("rev") != plan["rev"]:
                return envelope("blocked", "REVISION_CHANGED", {"preview_id": rec["preview_id"]},
                                "Nada foi enviado: o item mudou depois da prévia. Prepare de novo.")
            self.store.transition(rec, "applying", step="upload")
            kind, j, resp, diag = self._send(
                org, "POST", f"/{check_guid(plan['project_id'])}/_apis/wit/attachments",
                {"fileName": plan["file"]["name"], "api-version": API}, data=data,
                content_type="application/octet-stream")
            if kind != "ok":
                return self._outcome(rec, kind, resp, diag, "upload do anexo")
            upload_url = (j or {}).get("url")
            if not upload_url or not attachment_id(upload_url):
                self.store.transition(rec, "ambiguous", note="upload sem url")
                return envelope("blocked", "BAD_RESPONSE", {"preview_id": rec["preview_id"]},
                                "O upload não devolveu a URL do anexo. Rode a reconciliação.")
            self.store.transition(rec, "partial", upload_url=upload_url, attachment_id=attachment_id(upload_url))
        rel: Dict[str, Any] = {"rel": "AttachedFile", "url": upload_url}
        if plan.get("comment"):
            rel["attributes"] = {"comment": plan["comment"]}
        self.store.transition(rec, "applying", step="relation")
        kind, j, resp, diag = self._send(
            org, "PATCH", f"/{check_guid(plan['project_id'])}/_apis/wit/workitems/{check_id(plan['target_id'])}",
            {"api-version": API}, json_body=[{"op": "add", "path": "/relations/-", "value": rel}],
            content_type=JSON_PATCH)
        if kind == "ok":
            self.store.transition(rec, "applied", item_id=plan["target_id"])
            return self._readback_attach(rec)
        if kind in ("refused", "not_sent"):
            self.store.transition(rec, "partial", note=diag.get("reason_code") or diag.get("http_status"))
            return envelope("failed", (resp.reason_code if resp is not None else diag.get("reason_code")) or "REFUSED",
                            {"preview_id": rec["preview_id"], "uploaded": True},
                            "O arquivo subiu, mas a relação não foi gravada. Chame ado_apply de novo: "
                            "ele não sobe o arquivo outra vez.", diag)
        return self._outcome(rec, kind, resp, diag, "vínculo do anexo")

    # --- releitura --------------------------------------------------------------------

    def _check_patch(self, org: str, pid: str, item: Mapping[str, Any], patch: Sequence[Mapping[str, Any]],
                     removed_urls: Sequence[str] = ()) -> List[Dict[str, Any]]:
        f = item.get("fields") or {}
        rels = item.get("relations") or []
        bad = []
        for e in patch:
            path = e.get("path", "")
            if e.get("op") == "add" and path.startswith("/fields/"):
                ref = path[8:]
                if not value_equal(ref, e["value"], f.get(ref), org, pid):
                    bad.append({"field": ref, "sent": e["value"], "read": identity_view(f.get(ref))})
            elif e.get("op") == "remove" and path.startswith("/fields/"):
                ref = path[8:]
                if f.get(ref) not in (None, ""):
                    bad.append({"field": ref, "sent": None, "read": identity_view(f.get(ref))})
            elif e.get("op") == "add" and path == "/relations/-":
                v = e["value"]
                if not any(r.get("rel") == v["rel"] and self._same_target(r.get("url", ""), v["url"]) for r in rels):
                    bad.append({"relation": v["rel"], "url": v["url"], "read": "ausente"})
        for u in removed_urls:
            if any(r.get("url") == u for r in rels):
                bad.append({"relation_removed": u, "read": "presente"})
        return bad

    def _readback_create(self, rec: Mapping[str, Any], new_id: int) -> Dict[str, Any]:
        org, plan = rec["org"], rec["plan"]
        try:
            raw = self.api.get_raw(org, new_id, "relations")
        except AdoApiError as e:
            return envelope("ok", "APPLIED_NO_READBACK", {"id": new_id, "preview_id": rec["preview_id"]},
                            "Criado, mas a releitura falhou. Consulte o item.", e.diagnostic)
        item = self.api.summarize(org, raw)
        bad = self._check_patch(org, plan["project_id"], raw, plan["patch"])
        return envelope("ok", "APPLIED", {"id": new_id, "web_url": item.get("web_url"), "state": item["state"],
                                          "parent_id": item["parent_id"], "rev": item["rev"],
                                          "readback": {"mismatches": bad}, "preview_id": rec["preview_id"]},
                        "Mostre o id, a URL, o estado e o parent." + (" Avise as divergências da releitura." if bad else ""))

    def _readback_update(self, rec: Mapping[str, Any]) -> Dict[str, Any]:
        org, plan = rec["org"], rec["plan"]
        n = plan["target_id"]
        try:
            raw = self.api.get_raw(org, n, "relations")
        except AdoApiError as e:
            return envelope("ok", "APPLIED_NO_READBACK", {"id": n, "preview_id": rec["preview_id"]},
                            "Atualizado, mas a releitura falhou. Consulte o item.", e.diagnostic)
        item = self.api.summarize(org, raw)
        removed = [r.get("url") for r in (rec.get("display") or {}).get("removed_relations") or []]
        bad = self._check_patch(org, plan["project_id"], raw, plan["patch"], removed)
        return envelope("ok", "APPLIED", {"id": n, "web_url": item.get("web_url"), "state": item["state"],
                                          "state_category": item.get("state_category"), "rev": item["rev"],
                                          "parent_id": item["parent_id"], "readback": {"mismatches": bad},
                                          "preview_id": rec["preview_id"]},
                        "Mostre o estado e os campos relidos." + (" Avise as divergências da releitura." if bad else ""))

    def _readback_comment(self, rec: Mapping[str, Any], cid: Any) -> Dict[str, Any]:
        org, plan = rec["org"], rec["plan"]
        data = {"id": plan["target_id"], "comment_id": cid, "preview_id": rec["preview_id"]}
        try:
            c = self.api.comment(org, plan["project_id"], plan["target_id"], cid)
        except AdoApiError as e:
            return envelope("ok", "APPLIED_NO_READBACK", data, "Comentado, mas a releitura falhou.", e.diagnostic)
        same = htmlsafe.equivalent(c.get("text") or "", plan["body"]["text"], org, plan["project_id"])
        data["readback"] = {"mismatches": [] if same else [{"comment": "HTML relido diferente do enviado"}]}
        return envelope("ok", "APPLIED", data, "Comentário gravado." + ("" if same else " O HTML relido difere: avise."))

    def _readback_attach(self, rec: Mapping[str, Any]) -> Dict[str, Any]:
        org, plan = rec["org"], rec["plan"]
        att = (rec.get("result") or {}).get("attachment_id")
        data = {"id": plan["target_id"], "file": plan["file"], "attachment_id": att, "preview_id": rec["preview_id"]}
        try:
            raw = self.api.get_raw(org, plan["target_id"], "relations")
        except AdoApiError as e:
            return envelope("ok", "APPLIED_NO_READBACK", data, "Anexado, mas a releitura falhou.", e.diagnostic)
        found = [r for r in raw.get("relations") or []
                 if r.get("rel") == "AttachedFile" and attachment_id(r.get("url", "")) == att]
        data["relation_url"] = found[0]["url"] if found else None
        data["readback"] = {"mismatches": [] if found else [{"relation": "AttachedFile", "read": "ausente"}]}
        return envelope("ok", "APPLIED", data,
                        "Para citar o anexo num comentário, use relation_url (a URL gravada na relação).")

    # --- reconcile --------------------------------------------------------------------

    def reconcile(self, preview_id: str) -> Dict[str, Any]:
        """Procura o efeito de um envio incerto. Resultado em data.outcome: applied, not_applied, ambiguous, partial."""
        with self._lock:
            return self._reconcile(preview_id)

    def _reconcile(self, preview_id: str) -> Dict[str, Any]:
        try:
            rec = self.store.load(preview_id)
        except PreviewError as e:
            return envelope("blocked", e.reason_code, {"preview_id": preview_id}, str(e))
        if rec["state"] not in ("applying", "ambiguous", "partial"):
            return envelope("ok", "NOTHING_TO_RECONCILE", {"preview_id": preview_id, "outcome": rec["state"],
                                                           "result": rec.get("result")},
                            f"A prévia está em {rec['state']}; não há o que reconciliar.")
        op = rec["operation"]
        try:
            if op == "create":
                outcome, extra = self._rec_create(rec)
            elif op == "update":
                outcome, extra = self._rec_update(rec)
            elif op == "comment":
                outcome, extra = self._rec_comment(rec)
            else:
                outcome, extra = self._rec_attach(rec)
        except AdoApiError as e:
            return envelope("blocked", "RECONCILE_FAILED", {"preview_id": preview_id, "outcome": "ambiguous"},
                            "Não consegui conferir o efeito. Tente ado_reconcile de novo mais tarde; não reenvie.",
                            e.diagnostic)
        state = {"applied": "applied", "not_applied": "not_applied", "partial": "partial"}.get(outcome, "ambiguous")
        self.store.transition(rec, state, note="reconcile", **extra)
        data = {"preview_id": preview_id, "outcome": outcome}
        data.update(extra)
        if outcome == "applied":
            if op == "create":
                return self._merge(self._readback_create(rec, extra["item_id"]), data)
            if op == "update":
                return self._merge(self._readback_update(rec), data)
            if op == "comment":
                return self._merge(self._readback_comment(rec, extra.get("comment_id")), data)
            return self._merge(self._readback_attach(rec), data)
        if outcome == "not_applied":
            return envelope("failed", "NOT_APPLIED", data, "O efeito não aconteceu. Prepare de novo para repetir.")
        if outcome == "partial":
            return envelope("failed", "PARTIAL", data,
                            "O arquivo subiu, mas a relação não existe. Chame ado_apply de novo para vincular.")
        return envelope("blocked", "AMBIGUOUS", data,
                        "Não dá para saber se a escrita aconteceu. Confira o item no navegador antes de repetir.")

    @staticmethod
    def _merge(env: Dict[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
        env["data"].update(data)
        return env

    def _since(self, rec: Mapping[str, Any]) -> float:
        return float((rec.get("result") or {}).get("applying_at") or rec.get("created_at") or 0) - CLOCK_SKEW

    def _rec_create(self, rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        org, plan = rec["org"], rec["plan"]
        since = self._since(rec)
        days = int(max(0.0, self.clock() - since) // 86400) + 1
        q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
             f" AND [System.WorkItemType] = {wiql_literal(plan['type'])}"
             f" AND [System.Title] = {wiql_literal(plan['title'])}"
             f" AND [System.CreatedBy] = @Me AND [System.CreatedDate] >= @Today - {days}")
        ids = self.api.wiql_ids(org, plan["project_id"], q, top=50)
        items = self.api.get_many(org, ids, ["System.Id", "System.Title", "System.CreatedDate", "System.Parent"]) if ids else []
        hits = []
        for it in items:
            f = it["fields"]
            created = _ts(f.get("System.CreatedDate"))
            if created is None or created < since:
                continue
            if plan.get("parent_id") is not None and f.get("System.Parent") != plan["parent_id"]:
                continue
            hits.append(it["id"])
        if len(hits) == 1:
            return "applied", {"item_id": hits[0]}
        if not hits:
            return "not_applied", {}
        return "ambiguous", {"candidates": hits}

    def _rec_update(self, rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        org, plan = rec["org"], rec["plan"]
        raw = self.api.get_raw(org, plan["target_id"], "relations")
        if raw.get("rev") == plan["rev"]:
            return "not_applied", {}
        removed = [r.get("url") for r in (rec.get("display") or {}).get("removed_relations") or []]
        bad = self._check_patch(org, plan["project_id"], raw, plan["patch"], removed)
        if not bad:
            return "applied", {"item_id": plan["target_id"]}
        return "ambiguous", {"rev": raw.get("rev"), "mismatches": bad}

    def _rec_comment(self, rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        org, plan = rec["org"], rec["plan"]
        since = self._since(rec)
        hits = []
        for c in self.api.comments(org, plan["target_id"], plan["project_id"]):
            created = _ts(c.get("createdDate"))
            if created is not None and created >= since and htmlsafe.equivalent(
                    c.get("text") or "", plan["body"]["text"], org, plan["project_id"]):
                hits.append(c.get("id"))
        if len(hits) == 1:
            return "applied", {"comment_id": hits[0]}
        if not hits:
            return "not_applied", {}
        return "ambiguous", {"candidates": hits}

    def _rec_attach(self, rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        org, plan = rec["org"], rec["plan"]
        result = rec.get("result") or {}
        raw = self.api.get_raw(org, plan["target_id"], "relations")
        rels = [r for r in raw.get("relations") or [] if r.get("rel") == "AttachedFile"]
        att = result.get("attachment_id")
        if att:
            if any(attachment_id(r.get("url", "")) == att for r in rels):
                return "applied", {"item_id": plan["target_id"]}
            return "partial", {}
        # Upload incerto: sem a URL, procura um AttachedFile novo com o mesmo nome e tamanho.
        since = self._since(rec)
        hits = []
        for r in rels:
            a = r.get("attributes") or {}
            when = _ts(a.get("authorizedDate"))
            if a.get("name") == plan["file"]["name"] and a.get("resourceSize") == plan["file"]["size"] and (
                    when is None or when >= since):
                hits.append(r.get("url"))
        if len(hits) == 1:
            return "applied", {"item_id": plan["target_id"], "attachment_id": attachment_id(hits[0])}
        if not hits:
            # Um upload sem relação não aparece no item; repetir é seguro.
            return "not_applied", {}
        return "ambiguous", {"candidates": hits}

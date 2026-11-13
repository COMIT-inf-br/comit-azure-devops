"""Ferramentas MCP do plugin: contexto do processo, esquemas de entrada e despacho.

Todo retorno é o envelope `{status, reason_code, data, next_action, diagnostic}`.
O `Context` existe uma vez por processo e guarda uma só `Writes`. A config é
relida do disco a cada chamada (o CLI pode tê-la mudado) e atualizada no lugar,
porque `AuthManager`, `AdoHttp` e `Writes` guardam a referência do dict.
Nenhuma variável de ambiente é lida aqui.
"""
from __future__ import annotations

import copy
import json
import platform
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlparse

from . import config as cfgmod
from . import discovery, htmlsafe, paths, profile_validate
from .adoapi import AdoApiError, DiscoveryClient
from .auth import AuthError, AuthManager
from .http import AdoHttp, HttpError
from .preview import PreviewStore
from .vault import VaultError, VaultTools, read_pat, redact
from .workitems import NeedsDecision, WorkItemsApi, check_guid
from .writes import FIELD_REF_RE, Writes, envelope, error_envelope

MAX_DIFF = 100
MAX_PROPOSALS = 20


class ToolError(Exception):
    """Interrompe o handler com um envelope pronto."""

    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("reason_code") or "ERROR")
        self.env = result


def ok(data: Optional[Mapping[str, Any]] = None, next_action: Optional[str] = None,
       reason_code: Optional[str] = None) -> Dict[str, Any]:
    return envelope("ok", reason_code, data, next_action)


def stop(status: str, code: str, message: str, data: Optional[Mapping[str, Any]] = None) -> ToolError:
    return ToolError(envelope(status, code, data, message))


# --- validação de argumentos (subconjunto do JSON Schema) -------------------------------

_TYPES: Dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate_args(schema: Mapping[str, Any], value: Any, path: str = "") -> List[str]:
    """Erros de `value` contra `schema`. Lista vazia = válido."""
    errs: List[str] = []
    where = path or "argumentos"
    if "anyOf" in schema:
        if not any(not validate_args(s, value, path) for s in schema["anyOf"]):
            errs.append(f"{where}: valor fora dos formatos aceitos")
        return errs
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_TYPES[n](value) for n in names):
            return [f"{where}: esperado {'/'.join(names)}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: use um destes valores: {', '.join(map(str, schema['enum']))}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in value:
                errs.append(f"{path + '.' if path else ''}{k}: obrigatório")
        for k, v in value.items():
            if k in props:
                errs += validate_args(props[k], v, f"{path + '.' if path else ''}{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path + '.' if path else ''}{k}: argumento desconhecido")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate_args(schema["items"], v, f"{where}[{i}]")
    return errs


# --- contexto -----------------------------------------------------------------------------

class Context:
    """Estado do processo. Os testes e o modo --mock injetam as peças pelo construtor."""

    def __init__(self, config_dir: Optional[Path] = None, embedded_dir: Optional[Path] = None,
                 http: Optional[AdoHttp] = None, auth: Optional[Any] = None,
                 auth_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 http_factory: Optional[Callable[[Any, Dict[str, Any]], AdoHttp]] = None,
                 az_factory: Optional[Callable[[], Any]] = None, vault_tools: Optional[VaultTools] = None,
                 discovery_factory: Optional[Callable[[AdoHttp], Any]] = None,
                 tenant_lookup: Optional[Callable[[str], Optional[str]]] = None,
                 scheme_host: Optional[str] = None, home: Optional[Path] = None, mode: str = "real", info: Optional[Mapping[str, Any]] = None):
        self.config_dir = Path(config_dir) if config_dir is not None else paths.config_dir()
        self.config_path = paths.config_file(self.config_dir)
        self.learned_dir = paths.learned_profiles_dir(self.config_dir)
        self.embedded_dir = Path(embedded_dir) if embedded_dir is not None else paths.embedded_profiles_dir()
        self.mode = mode
        self.info = dict(info or {})
        self.cfg: Dict[str, Any] = cfgmod.default_config()
        self.config_error: Optional[cfgmod.ConfigError] = None
        self.vault = vault_tools or VaultTools()
        self.reload()
        self.auth = auth or (auth_factory(self.cfg) if auth_factory
                             else AuthManager(self.cfg, az_factory=az_factory, vault_tools=self.vault))
        self.http = http or (http_factory(self.auth, self.cfg) if http_factory
                             else AdoHttp(self.auth, lambda: cfgmod.configured_orgs(self.cfg),
                                          scheme_host=scheme_host))
        self.scheme_host = scheme_host
        self.api = WorkItemsApi(self.http, self.profile_of)
        self.store = PreviewStore(paths.previews_dir(self.config_dir))
        self.writes = Writes(self.api, self.store, self.cfg, home=home, config_dir=self.config_dir)
        self.discovery_factory = discovery_factory or DiscoveryClient
        self.tenant_lookup = tenant_lookup or self.http.resource_tenant
        self.logins: Dict[str, Any] = {}
        self.proposals: Dict[str, Dict[str, Any]] = {}
        self.discovered: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # config
    def reload(self) -> None:
        try:
            new, self.config_error = cfgmod.load_config(self.config_path), None
        except cfgmod.ConfigError as e:
            new, self.config_error = cfgmod.default_config(), e
        except OSError:
            new = cfgmod.default_config()
            self.config_error = cfgmod.ConfigError("CONFIG_UNREADABLE", "Não consegui ler o config.json.")
        self.cfg.clear()
        self.cfg.update(new)

    def save_config(self, new: Dict[str, Any]) -> Path:
        p = cfgmod.save_config(new, self.config_path)
        self.cfg.clear()
        self.cfg.update(new)
        return p

    def profile_of(self, org: str) -> Tuple[Optional[Dict[str, Any]], str]:
        return cfgmod.load_profile(org, self.learned_dir, self.embedded_dir)

    def org(self, a: Mapping[str, Any]) -> str:
        """Nome configurado da org do pedido ou a org padrão."""
        text = a.get("org") or self.cfg.get("default_org")
        if not text:
            raise stop("needs_setup", "NO_ORG", "Nenhuma organização configurada. Use ado_setup_org.")
        name = cfgmod.find_org(self.cfg, cfgmod.normalize_org(str(text)))
        if name is None:
            raise stop("needs_setup", "ORG_NOT_CONFIGURED",
                       f"A organização {text} não está configurada. Use ado_setup_org.",
                       {"configured": list(cfgmod.configured_orgs(self.cfg))})
        return name

    def project(self, org: str, a: Mapping[str, Any]) -> Dict[str, Any]:
        p = a.get("project") or (self.cfg["orgs"][org].get("default_project"))
        if not p:
            raise stop("needs_decision", "PROJECT_REQUIRED", "Informe o projeto (nome ou GUID).")
        return self.api.project(org, str(p))

    def team(self, org: str, proj: Mapping[str, Any], a: Mapping[str, Any]) -> str:
        """Team do pedido ou o padrão da pessoa no projeto (ado_set_defaults)."""
        team = a.get("team")
        if not team:
            for k, v in ((self.cfg["orgs"][org].get("defaults")) or {}).items():
                if str(k).casefold() == proj["id"].casefold():
                    team = (v or {}).get("team")
        if not team:
            raise stop("needs_decision", "TEAM_REQUIRED", "Informe o team (ado_list_teams lista as opções).")
        return str(team)

    @property
    def planning(self):
        return self.writes.batch.plan

    def az(self) -> Optional[Any]:
        f = getattr(self.auth, "az", None)
        return f() if f else None

    # descoberta
    def discover(self, org: str, project: str) -> Dict[str, Any]:
        """Proposta de perfil já mesclada ao perfil atual, validada, com diff."""
        client = self.discovery_factory(self.http)
        try:
            found = discovery.discover_org(client, org, project=project)
        except LookupError as e:
            raise AdoApiError("PROJECT_NOT_FOUND", str(e)) from None
        cur, src = self.api.profile(org)
        merged = discovery.merge_profiles(cur, found)
        errors = profile_validate.validate_profile(merged)
        diff = discovery.diff_profiles(cur, merged) if cur else None
        return {"profile": merged, "errors": errors, "diff": diff, "current_source": src}

    def stash(self, org: str, profile: Dict[str, Any]) -> str:
        pid = uuid.uuid4().hex
        self.proposals[pid] = {"org": org, "profile": profile}
        while len(self.proposals) > MAX_PROPOSALS:
            self.proposals.pop(next(iter(self.proposals)))
        return pid


# --- ferramentas ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[[Context, Dict[str, Any]], Dict[str, Any]]
    read_only: bool
    title: str


TOOLS: Dict[str, Tool] = {}


def S(props: Mapping[str, Any], required: Tuple[str, ...] = ()) -> Dict[str, Any]:
    return {"type": "object", "properties": dict(props), "required": list(required), "additionalProperties": False}


def tool(name: str, title: str, description: str, schema: Dict[str, Any], read_only: bool = True):
    def deco(fn):
        TOOLS[name] = Tool(name, description, schema, fn, read_only, title)
        return fn
    return deco


ORG = {"type": "string", "description": "Nome da organização. Omita para usar a organização padrão."}
PROJECT = {"type": "string", "description": "Nome ou GUID do projeto."}
ITEM_ID = {"type": ["integer", "string"], "description": "Id numérico do work item."}
CONFIRM = {"type": "boolean", "description": "true só depois do sim explícito da pessoa. Sem ele, nada é gravado."}
FIELDS_LIST = {"type": "array", "items": {"type": "string"},
               "description": "referenceName dos campos a devolver. Omita para o conjunto padrão."}
HYPERLINKS = {"type": "array", "description": "Links http ou https, como texto ou {url, comment}.",
              "items": {"anyOf": [{"type": "string"}, S({"url": {"type": "string"}, "comment": {"type": "string"}},
                                                         ("url",))]}}
FIELDS_OBJ = {"type": "object", "description": "Campos {referenceName: valor}. Use o referenceName do campo."}


# --- status e setup ------------------------------------------------------------------------------

@tool("ado_status", "Status do Azure DevOps",
      "Diagnostica o setup: runtime, Azure CLI, login, organizações configuradas, perfil e pendências "
      "(pending_setup). Chame antes de qualquer operação. Sem rede por padrão.",
      S({"org": ORG, "check_remote": {"type": "boolean", "description": "true faz um GET de identidade na org."}}))
def t_status(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    pending: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {"mode": ctx.mode, "runtime": {"python": platform.python_version(),
                                                          "system": paths.system()}}
    if ctx.info:
        data["mock"] = ctx.info
    if ctx.config_error is not None:
        pending.append({"code": "CONFIG_INVALID", "message": str(ctx.config_error)})
    az_state, logged = "unavailable", []
    try:
        az = ctx.az()
        if az is not None:
            az_state = "found"
            logged = [x.casefold() for x in az.logged_tenants()]
            data["az"] = {"found": True, "path": az.cmd[0], "version": az.version(), "logged_tenants": len(logged)}
        else:
            data["az"] = {"found": None, "note": "sem Azure CLI neste contexto"}
    except AuthError as e:
        az_state = "missing"
        data["az"] = {"found": False, "reason_code": e.reason_code}
    names = list(cfgmod.configured_orgs(ctx.cfg))
    if a.get("org"):
        one = cfgmod.find_org(ctx.cfg, cfgmod.normalize_org(str(a["org"])))
        if one is None:
            pending.append({"code": "ORG_NOT_CONFIGURED", "org": a["org"],
                            "message": f"A organização {a['org']} não está configurada."})
        names = [one] if one else []
    elif not names and ctx.config_error is None:
        pending.append({"code": "NO_ORG", "message": "Nenhuma organização configurada."})
    orgs = []
    for name in names:
        entry = ctx.cfg["orgs"][name]
        auth = entry.get("auth") or "az"
        prof, psrc = ctx.profile_of(name) if _safe_profile(ctx, name) else (None, "absent")
        row: Dict[str, Any] = {"org": name, "auth": auth, "tenant": entry.get("tenant"),
                               "default_project": entry.get("default_project"), "profile": psrc,
                               "usage": (prof or {}).get("usage"), "login": "n/a"}
        blocked = False
        if auth == "az":
            if az_state == "missing":
                pending.append({"code": "AZ_NOT_FOUND", "org": name, "message": "Azure CLI não encontrado."})
                row["login"], blocked = "unknown", True
            elif not entry.get("tenant"):
                pending.append({"code": "TENANT_UNKNOWN", "org": name,
                                "message": "A organização não tem tenant na config. Rode ado_setup_org."})
                row["login"], blocked = "unknown", True
            elif az_state == "found":
                good = entry["tenant"].casefold() in logged
                row["login"] = "ok" if good else "missing"
                if not good:
                    pending.append({"code": "LOGIN_MISSING", "org": name, "message": "Sem login do Azure CLI no tenant."})
                    blocked = True
        else:
            try:
                res = read_pat(name, entry.get("pat_ref") or {}, ctx.vault)
                row["pat"] = {"found": True, "source": res.source}
            except VaultError as e:
                row["pat"] = {"found": False, "reason_code": e.reason_code}
                pending.append({"code": e.reason_code, "org": name, "message": str(e)})
                blocked = True
        if psrc == "absent" and (row["usage"] or {}).get("work_items", True):
            pending.append({"code": "PROFILE_ABSENT", "org": name,
                            "message": "Sem perfil. Use ado_discover_org e ado_profile_save."})
        if a.get("check_remote") and not blocked:
            try:
                who = ctx.api.whoami(name)
                row["remote"] = {"ok": True, "authenticated_as": who.get("displayName")}
            except AdoApiError as e:
                row["remote"] = {"ok": False, "reason_code": e.reason_code, "diagnostic": e.diagnostic}
                pending.append({"code": e.reason_code, "org": name, "message": str(e)})
        orgs.append(row)
    data.update({"default_org": ctx.cfg.get("default_org"), "orgs": orgs, "pending_setup": pending,
                 "logins_running": [o for o, h in ctx.logins.items() if h.poll() is None]})
    if pending:
        return envelope("needs_setup", "SETUP_PENDING", data, "Conduza o setup pela skill ado-configurar.")
    return ok(data, reason_code="READY")


def _safe_profile(ctx: Context, org: str) -> bool:
    try:
        ctx.profile_of(org)
        return True
    except cfgmod.ConfigError:
        return False


def _url_parts(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "://" not in text:
        return out
    parts = [unquote(p) for p in urlparse(text).path.split("/") if p]
    if len(parts) > 1 and not parts[1].startswith("_"):
        out["project_hint"] = parts[1]
    m = re.search(r"/_workitems/edit/(\d+)", urlparse(text).path)
    if m:
        out["item_id"] = int(m.group(1))
    return out


PAT_REF = S({"nordpass_title": {"type": ["string", "null"]}, "nordpass_folder": {"type": ["string", "null"]},
             "os_service": {"type": "string"}, "os_account": {"type": "string"}})


@tool("ado_setup_org", "Configurar organização",
      "Normaliza a organização (nome ou URL de item/projeto), descobre o tenant por GET anônimo e diz se há perfil. "
      "Só grava na config local com confirm=true. auth é a escolha de login da pessoa: az (Azure CLI, sem PAT, "
      "recomendado) ou pat. pat_ref registra a referência do PAT (nunca o valor).",
      S({"org": {"type": "string", "description": "Nome da organização ou URL dev.azure.com/<org>/..."},
         "auth": {"enum": ["az", "pat"]}, "pat_ref": PAT_REF, "confirm": CONFIRM}, ("org",)), read_only=False)
def t_setup_org(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(a["org"])
    name = cfgmod.find_org(ctx.cfg, cfgmod.normalize_org(raw)) or cfgmod.normalize_org(raw)
    known = (ctx.cfg.get("orgs") or {}).get(name) or {}
    tenant, note = known.get("tenant"), None
    if not tenant:
        try:
            tenant = ctx.tenant_lookup(name)
        except HttpError as e:
            note = f"Tenant não lido: {e.reason_code}."
    _, psrc = ctx.profile_of(name)
    fields: Dict[str, Any] = {}
    if tenant:
        fields["tenant"] = tenant
    if a.get("auth"):
        fields["auth"] = a["auth"]
    if a.get("pat_ref"):
        cfgmod.validate_pat_ref(name, a["pat_ref"])
        fields["pat_ref"] = a["pat_ref"]
    data = {"org": name, "tenant": tenant, "already_configured": bool(known), "profile": psrc,
            "would_save": {k: v for k, v in fields.items()}, **_url_parts(raw)}
    if note:
        data["note"] = note
    if not tenant and (a.get("auth") or known.get("auth") or "az") == "az":
        data["tenant_warning"] = ("Sem tenant. A organização pode usar conta Microsoft pessoal: "
                                  "use auth=pat (PAT de contingência).")
    if not a.get("confirm"):
        return envelope("needs_decision", "CONFIRM_REQUIRED", data,
                        "Mostre à pessoa e chame de novo com confirm=true depois do sim.")
    ctx.save_config(cfgmod.set_org(ctx.cfg, name, **fields))
    data["saved"] = True
    return ok(data, "Organização gravada. Próximo passo: login (ado_auth_login) ou perfil.", "ORG_SAVED")


@tool("ado_auth_login", "Login do Azure CLI",
      "Inicia az login no host (navegador da máquina) ou com código de dispositivo. Não bloqueia: devolve o estado. "
      "Chame de novo para ver o andamento.",
      S({"org": ORG, "mode": {"enum": ["browser", "device_code"]}}), read_only=False)
def t_auth_login(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    mode = a.get("mode") or "browser"
    h = ctx.logins.get(org)
    started = False
    if h is None:
        entry = ctx.cfg["orgs"][org]
        tenant = entry.get("tenant") or ctx.tenant_lookup(org)
        if not tenant:
            raise stop("needs_decision", "TENANT_UNKNOWN",
                       "A organização não informou tenant. Use o PAT de contingência (ado_setup_org auth=pat).")
        az = ctx.auth.az()
        h = az.start_login(tenant, mode)
        ctx.logins[org], started = h, True
    st = h.status()
    if st["state"] == "ok":
        ctx.logins.pop(org, None)
        ctx.auth.invalidate(org)
        return ok({"login": st}, "Login concluído. Chame ado_status para conferir.", "LOGIN_OK")
    if st["state"] == "failed":
        ctx.logins.pop(org, None)
        return envelope("failed", "AZ_LOGIN_FAILED", {"login": st}, "O login falhou. Repita ou use device_code.")
    if st.get("code"):
        na = f"Diga à pessoa: abra {st.get('url')} e digite o código {st['code']}."
    elif h.mode == "device_code":
        na = "O código ainda não apareceu. Chame ado_auth_login de novo em alguns segundos."
    else:
        na = "Peça à pessoa que conclua o login no navegador. Depois chame ado_status."
    return ok({"login": st, "started": started}, na, "LOGIN_RUNNING")


@tool("ado_whoami", "Identidade autenticada", "Devolve displayName, uniqueName e id da identidade autenticada na org.",
      S({"org": ORG}))
def t_whoami(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ok(ctx.api.whoami(ctx.org(a)))


@tool("ado_list_projects", "Listar projetos", "Lista id, nome e processo dos projetos da organização.", S({"org": ORG}))
def t_projects(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    rows = ctx.api.list_projects(ctx.org(a))
    return ok({"count": len(rows), "projects": rows})


@tool("ado_list_teams", "Listar teams", "Lista os teams de um projeto.", S({"org": ORG, "project": PROJECT}))
def t_teams(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    rows = ctx.api.list_teams(org, ctx.project(org, a)["id"])
    return ok({"count": len(rows), "teams": rows})


@tool("ado_iterations", "Listar iterações", "Lista as iterações (sprints) de um team, com início e fim.",
      S({"org": ORG, "project": PROJECT, "team": {"type": "string", "description": "Nome ou GUID do team."},
         "timeframe": {"enum": ["current", "future", "past", "all"]}}))
def t_iterations(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    team = ctx.team(org, proj, a)
    rows = ctx.api.iterations(org, proj["id"], team, a.get("timeframe") or "current")
    return ok({"count": len(rows), "iterations": rows})


TEAM = {"type": "string", "description": "Nome ou GUID do team. Omita para usar o padrão da pessoa."}
ITERATION = {"type": "string", "description": "Sprint: nome, caminho (Projeto\\Sprint 3) ou GUID."}


@tool("ado_backlog", "Backlog do time",
      "Itens do time ainda sem sprint: iteração de backlog do time, áreas do time e estados abertos por padrão. "
      "Sem types, usa os tipos de requisito do processo. Ordena pela prioridade do backlog quando o campo existe.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "types": {"type": "array", "items": {"type": "string"}},
         "state_category": {"type": "string", "description": "Proposed, InProgress, Resolved, Completed, Removed "
                                                              "ou open (padrão)."},
         "top": {"type": "integer", "description": "Máximo de itens (1 a 200, padrão 100)."}}))
def t_backlog(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    return ok(ctx.planning.backlog(org, proj["id"], ctx.team(org, proj, a), a.get("types"), a.get("state_category"),
                                   a.get("top") or 100))


@tool("ado_sprint_items", "Itens da sprint",
      "Itens de uma sprint do time (padrão: a atual), com pai, estado, responsável e esforço "
      "(RemainingWork, OriginalEstimate, CompletedWork). Traz totais por pessoa.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "iteration": ITERATION}))
def t_sprint_items(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    return ok(ctx.planning.sprint_items(org, proj["id"], ctx.team(org, proj, a), a.get("iteration")))


@tool("ado_get", "Ler work item", "Lê um work item: campos, relações, parent, filhos, URL web, estado e categoria.",
      S({"org": ORG, "id": ITEM_ID, "expand": {"enum": ["none", "relations", "all"]}}, ("id",)))
def t_get(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ok({"item": ctx.api.get(ctx.org(a), a["id"], a.get("expand") or "relations")})


@tool("ado_query", "Consultar work items",
      "Lista work items por filtros (assigned_to_me, type, state_category, parent) ou por WIQL livre. "
      "state_category: Proposed, InProgress, Resolved, Completed, Removed ou open. Devolve ids e campos.",
      S({"org": ORG, "project": PROJECT, "wiql": {"type": "string"}, "assigned_to_me": {"type": "boolean"},
         "type": {"type": "string"}, "types": {"type": "array", "items": {"type": "string"}},
         "state_category": {"type": "string"}, "parent": ITEM_ID,
         "top": {"type": "integer", "description": "Máximo de itens (1 a 200, padrão 50)."}, "fields": FIELDS_LIST}))
def t_query(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    types = list(a.get("types") or []) + ([a["type"]] if a.get("type") else [])
    proj = a.get("project")
    return ok(ctx.api.query(org, proj, a.get("wiql"), bool(a.get("assigned_to_me")), types or None,
                            a.get("state_category"), a.get("parent"), a.get("top") or 50, a.get("fields")))


@tool("ado_children", "Filhos de um work item", "Lista os filhos (Hierarchy-Forward) com tipo, estado e título.",
      S({"org": ORG, "id": ITEM_ID, "fields": FIELDS_LIST}, ("id",)))
def t_children(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    rows = ctx.api.children(ctx.org(a), a["id"], a.get("fields"))
    return ok({"count": len(rows), "items": rows})


@tool("ado_comments", "Ler comentários", "Lista os comentários de um work item, em HTML e em texto.",
      S({"org": ORG, "project": PROJECT, "id": ITEM_ID, "limit": {"type": "integer"}}, ("id",)))
def t_comments(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    pid = ctx.api.project(org, str(a["project"]))["id"] if a.get("project") else None
    rows = ctx.api.comments(org, a["id"], pid, max(1, int(a.get("limit") or 1000)))
    for r in rows:
        r["plain"] = htmlsafe.to_text(r.get("text") or "")
    return ok({"count": len(rows), "comments": rows})


@tool("ado_resolve_identity", "Resolver pessoa",
      "Busca candidatos {displayName, uniqueName, id} por nome ou e-mail. Com team, busca nos membros. "
      "Só em memória.",
      S({"org": ORG, "project": PROJECT, "team": {"type": "string"}, "text": {"type": "string"}}, ("text",)))
def t_identity(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)["id"] if (a.get("team") or a.get("project")) else None
    rows = ctx.api.resolve_identity(org, a["text"], proj, a.get("team"))
    return ok({"count": len(rows), "candidates": rows},
              "Com mais de um candidato, pergunte à pessoa qual deles." if len(rows) > 1 else None)


def _type_view(tp: Mapping[str, Any], source: str) -> Dict[str, Any]:
    states = tp.get("states") or []
    by_cat: Dict[str, List[str]] = {}
    for s in states:
        by_cat.setdefault(s.get("category", ""), []).append(s["name"])
    return {"source": source, "reference_name": tp.get("reference_name"), "states": states,
            "states_by_category": by_cat, "required_on_create": tp.get("required_on_create") or [],
            "required_by_rule": tp.get("required_by_rule") or [], "defaults": tp.get("defaults") or {},
            "picklists": tp.get("picklists") or {}, "suggested_fields": tp.get("suggested_fields") or [],
            "blocking": tp.get("blocking")}


def _types_of(profile: Mapping[str, Any], project_id: str) -> Optional[Dict[str, Any]]:
    proc = next((p.get("process") for p in profile.get("projects") or []
                 if p["id"].casefold() == project_id.casefold()), None)
    types = ((profile.get("processes") or {}).get(proc) or {}).get("work_item_types")
    return types if isinstance(types, dict) else None


@tool("ado_type_schema", "Esquema de um tipo",
      "Obrigatórios na criação, regras condicionais, listas de valores, padrões e estados por categoria de um tipo. "
      "Lê o perfil; sem perfil, faz a descoberta (só GET) sem gravar.",
      S({"org": ORG, "project": PROJECT, "type": {"type": "string"}}, ("type",)))
def t_type_schema(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    wtype = str(a["type"])
    tp = ctx.api.type_profile(org, proj["id"], wtype)
    if tp:
        return ok({"type": wtype, **_type_view(tp, ctx.api.profile(org)[1])})
    key = (org.casefold(), proj["id"])
    found = ctx.discovered.get(key)
    if found is None:
        found = ctx.discover(org, proj["id"])
        if found["errors"]:
            return envelope("failed", "PROFILE_INVALID", {"errors": found["errors"][:20]},
                            "A descoberta gerou um perfil inválido. Não grave.")
        ctx.discovered[key] = found
    types = _types_of(found["profile"], proj["id"]) or {}
    hit = next((n for n in types if n.casefold() == wtype.casefold()), None)
    if hit is None:
        raise AdoApiError("TYPE_NOT_FOUND", f"Tipo {wtype} não existe neste projeto. Tipos: " + ", ".join(types))
    return ok({"type": hit, **_type_view(types[hit], "discovered")},
              "Perfil não gravado. Para gravar, use ado_discover_org e ado_profile_save.")


def summarize_profile(profile: Mapping[str, Any]) -> Dict[str, Any]:
    procs = []
    for pid, proc in (profile.get("processes") or {}).items():
        wit = proc.get("work_item_types")
        types: Any = "discover"
        if isinstance(wit, dict):
            types = {n: {"states": [s["name"] for s in t.get("states") or []],
                         "required_on_create": t.get("required_on_create") or [],
                         "required_by_rule": t.get("required_by_rule") or [],
                         "picklists": {f: len(v) for f, v in (t.get("picklists") or {}).items()}}
                     for n, t in wit.items()}
        procs.append({"id": pid, "name": proc.get("name"), "types": types})
    return {"org": profile.get("org"), "projects": len(profile.get("projects") or []), "processes": procs}


@tool("ado_discover_org", "Descobrir perfil da organização",
      "Lê processos, tipos, obrigatórios, regras e estados do projeto (só GET) e propõe um perfil, com a diferença "
      "contra o perfil atual. Não grava: use ado_profile_save com o proposal_id.",
      S({"org": ORG, "project": PROJECT}))
def t_discover(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    project = a.get("project") or ctx.cfg["orgs"][org].get("default_project")
    if not project:
        raise stop("needs_decision", "PROJECT_REQUIRED", "Informe o projeto (nome ou GUID).")
    found = ctx.discover(org, str(project))
    if found["errors"]:
        return envelope("failed", "PROFILE_INVALID", {"errors": found["errors"][:20]},
                        "A descoberta gerou um perfil inválido. Não grave.")
    pid = ctx.stash(org, found["profile"])
    diff = found["diff"]
    data = {"proposal_id": pid, "current_profile": found["current_source"], "summary": summarize_profile(found["profile"])}
    if diff is not None:
        data["diff_total"], data["diff"] = len(diff), diff[:MAX_DIFF]
    return ok(data, "Mostre o resumo à pessoa. Para gravar na config local, chame ado_profile_save com confirm=true.")


@tool("ado_profile_save", "Gravar perfil descoberto",
      "Grava a proposta de ado_discover_org como perfil aprendido, na config local (nunca no plugin).",
      S({"proposal_id": {"type": "string"}, "confirm": CONFIRM}, ("proposal_id",)), read_only=False)
def t_profile_save(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    prop = ctx.proposals.get(a["proposal_id"])
    if prop is None:
        raise stop("failed", "PROPOSAL_NOT_FOUND", "Proposta desconhecida. Rode ado_discover_org de novo.")
    errors = profile_validate.validate_profile(prop["profile"])
    if errors:
        return envelope("failed", "PROFILE_INVALID", {"errors": errors[:20]}, "Perfil inválido. Não foi gravado.")
    if not a.get("confirm"):
        return envelope("needs_decision", "CONFIRM_REQUIRED",
                        {"org": prop["org"], "summary": summarize_profile(prop["profile"])},
                        "Peça o sim da pessoa e chame de novo com confirm=true.")
    p = cfgmod.save_learned_profile(prop["org"], prop["profile"], ctx.learned_dir)
    return ok({"org": prop["org"], "path": str(p), "source": "learned"}, "Perfil gravado na config local.", "PROFILE_SAVED")


@tool("ado_set_defaults", "Padrões pessoais",
      "Grava na config local os padrões da pessoa num projeto: team, area_path e campos (valor null remove). "
      "Aceita me_unique_name. Só grava com confirm=true.",
      S({"org": ORG, "project": PROJECT,
         "defaults": S({"team": {"type": "string"}, "area_path": {"type": "string"},
                        "fields": {"type": "object", "description": "{referenceName: valor ou null}"}}),
         "me_unique_name": {"type": "string"}, "confirm": CONFIRM}), read_only=False)
def t_set_defaults(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    new = copy.deepcopy(ctx.cfg)
    entry = new["orgs"][org]
    change: Dict[str, Any] = {}
    d = a.get("defaults") or {}
    if d:
        proj = ctx.project(org, a)
        slot = entry.setdefault("defaults", {}).setdefault(proj["id"], {})
        for k in ("team", "area_path"):
            if k in d:
                slot[k] = d[k]
        for ref, v in (d.get("fields") or {}).items():
            if not FIELD_REF_RE.match(str(ref)) or isinstance(v, (dict, list)):
                raise AdoApiError("FIELD_INVALID", f"Campo ou valor inválido: {ref!r}.")
            if v is None:
                (slot.setdefault("fields", {})).pop(ref, None)
            else:
                slot.setdefault("fields", {})[ref] = v
        change = {"project": proj, "defaults": slot}
    if a.get("me_unique_name"):
        entry["me"] = {"uniqueName": a["me_unique_name"]}
        change["me"] = entry["me"]
    if not change:
        raise stop("needs_decision", "NOTHING_TO_SET", "Informe defaults ou me_unique_name.")
    data = {"org": org, "would_save": change}
    if not a.get("confirm"):
        return envelope("needs_decision", "CONFIRM_REQUIRED", data, "Mostre à pessoa e chame de novo com confirm=true.")
    ctx.save_config(new)
    return ok({**data, "saved": True}, "Padrões gravados na config local.", "DEFAULTS_SAVED")


# --- escritas em duas fases ----------------------------------------------------------------------------

@tool("ado_prepare_create", "Preparar criação",
      "Fase 1 da criação: completa padrões, confere obrigatórios, procura duplicata, lê o parent e valida com "
      "validateOnly. Não cria nada. Devolve a prévia e data.apply_args. Mostre a prévia e peça o OK antes de ado_apply.",
      S({"org": ORG, "project": PROJECT, "type": {"type": "string"}, "fields": FIELDS_OBJ, "parent_id": ITEM_ID,
         "hyperlinks": HYPERLINKS}, ("project", "type", "fields")), read_only=False)
def t_prepare_create(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.prepare_create(ctx.org(a), a["project"], a["type"], a["fields"], a.get("parent_id"),
                                     a.get("hyperlinks"))


@tool("ado_prepare_update", "Preparar atualização",
      "Fase 1 da atualização: lê o item, resolve o estado pela intenção, concatena tags, valida com validateOnly e "
      "trava a revisão. Não altera nada. Devolve a prévia e data.apply_args.",
      S({"org": ORG, "id": ITEM_ID, "fields": FIELDS_OBJ,
         "state_intent": {"type": "string", "description": "iniciar, concluir, cancelar, reabrir ou o nome exato."},
         "add_parent": ITEM_ID, "add_hyperlinks": HYPERLINKS,
         "remove_relations": {"type": "array", "description": "URL ou {rel, url|target_id} das relações a remover."}},
        ("id",)), read_only=False)
def t_prepare_update(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.prepare_update(ctx.org(a), a["id"], a.get("fields"), a.get("state_intent"),
                                     a.get("add_parent"), a.get("add_hyperlinks"), a.get("remove_relations"))


@tool("ado_prepare_comment", "Preparar comentário",
      "Fase 1 do comentário: escapa o texto (text) ou aceita HTML permitido (html), resolve menções e mostra o "
      "resultado. Não envia nada. Informe text ou html, não os dois.",
      S({"org": ORG, "id": ITEM_ID, "text": {"type": "string"}, "html": {"type": "string"},
         "mentions": {"type": "array", "description": "Texto de busca ou {id, displayName} já resolvido.",
                      "items": {"anyOf": [{"type": "string"}, S({"id": {"type": "string"},
                                                                  "displayName": {"type": "string"}}, ("id",))]}},
         "project": PROJECT, "team": {"type": "string"}}, ("id",)), read_only=False)
def t_prepare_comment(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.prepare_comment(ctx.org(a), a["id"], a.get("text"), a.get("html"), a.get("mentions"),
                                      a.get("project"), a.get("team"))


@tool("ado_prepare_attach", "Preparar anexo",
      "Fase 1 do anexo: confere o arquivo (caminho real, tamanho, SHA-256) e o item. Não envia nada. "
      "Recusa pastas de credencial.",
      S({"org": ORG, "id": ITEM_ID, "file_path": {"type": "string", "description": "Caminho absoluto do arquivo."},
         "comment": {"type": "string"}}, ("id", "file_path")), read_only=False)
def t_prepare_attach(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.prepare_attach(ctx.org(a), a["id"], a["file_path"], a.get("comment"))


@tool("ado_prepare_move_to_sprint", "Preparar mudança de sprint",
      "Fase 1: move 1 ou mais itens para uma sprint do time (ou iteration=backlog). include_children (padrão true) "
      "leva as filhas não concluídas. Lê cada item, trava a revisão e valida com validateOnly. Não altera nada.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "ids": {"type": "array", "items": ITEM_ID},
         "iteration": ITERATION, "include_children": {"type": "boolean"}}, ("ids", "iteration")), read_only=False)
def t_prepare_move(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    return ctx.writes.batch.prepare_move(org, proj["id"], ctx.team(org, proj, a), a["ids"], a["iteration"],
                                         a.get("include_children", True))


@tool("ado_prepare_create_sprint", "Preparar nova sprint",
      "Fase 1: cria a iteração e a associa ao time. Sem nome e datas, sugere a próxima pelo padrão do time "
      "(número + 1, datas logo depois da última). Se o nó já existir fora do time, só associa. Não cria nada.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "name": {"type": "string"},
         "start_date": {"type": "string", "description": "AAAA-MM-DD"},
         "finish_date": {"type": "string", "description": "AAAA-MM-DD"},
         "assign_to_team": {"type": "boolean", "description": "Padrão true."}}), read_only=False)
def t_prepare_create_sprint(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    return ctx.writes.batch.prepare_create_sprint(org, proj["id"], ctx.team(org, proj, a), a.get("name"),
                                                  a.get("start_date"), a.get("finish_date"),
                                                  a.get("assign_to_team", True))


@tool("ado_prepare_sprint_rollover", "Preparar virada de sprint",
      "Fase 1: lista o que não está concluído na sprint de origem (padrão: a atual) e prepara a mudança para a "
      "próxima. Se a próxima não existir e create_if_missing=true (padrão), inclui a criação dela. Não altera nada.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "from_iteration": ITERATION, "to_iteration": ITERATION,
         "create_if_missing": {"type": "boolean"}}), read_only=False)
def t_prepare_rollover(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    proj = ctx.project(org, a)
    return ctx.writes.batch.prepare_rollover(org, proj["id"], ctx.team(org, proj, a), a.get("from_iteration"),
                                             a.get("to_iteration"), a.get("create_if_missing", True))


@tool("ado_prepare_delete", "Preparar exclusão",
      "Fase 1: exclui o work item para a lixeira do projeto (recuperável; nunca em definitivo). Com filhos, exige "
      "include_children=true para excluir a árvore. Não exclui nada.",
      S({"org": ORG, "id": ITEM_ID, "include_children": {"type": "boolean"}}, ("id",)), read_only=False)
def t_prepare_delete(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.batch.prepare_delete(ctx.org(a), a["id"], a.get("include_children"))


BATCH_COMMENT = S({"id": ITEM_ID, "text": {"type": "string"}, "html": {"type": "string"},
                   "mentions": {"type": "array", "items": {"anyOf": [
                       {"type": "string"}, S({"id": {"type": "string"}, "displayName": {"type": "string"}}, ("id",))]}}},
                  ("id",))


@tool("ado_prepare_comments_batch", "Preparar lote de comentários",
      "Fase 1: vários comentários (um por item) numa prévia só, com um hash. Cada item leva text ou html e "
      "mentions. Resolve as menções no team. Não envia nada.",
      S({"org": ORG, "project": PROJECT, "team": TEAM, "items": {"type": "array", "items": BATCH_COMMENT},
         "source_label": {"type": "string", "description": "Origem do lote, por exemplo: Daily 13/11."}},
        ("items",)), read_only=False)
def t_prepare_comments_batch(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    return ctx.writes.batch.prepare_comments(org, a.get("project"), a.get("team"), a["items"], a.get("source_label"))


@tool("ado_prepare_block", "Preparar impedimento",
      "Fase 1: marca o item como impedido pelo padrão de impedimento do perfil da org (campos e política). "
      "Prévia única: PATCH dos campos (test /rev, validateOnly) e comentário obrigatório com o motivo, o que se "
      "espera, o responsável e a previsão. Não muda o estado. Sem responsible, date e forecast, usa os padrões da "
      "política e mostra os valores para a pessoa confirmar ou trocar. Org sem padrão: needs_setup.",
      S({"org": ORG, "id": ITEM_ID,
         "reason": {"type": "string", "description": "Motivo do impedimento, curto."},
         "waiting_for": {"type": "string", "description": "O que se espera para desimpedir."},
         "kind": {"type": "string", "description": "Tipo do impedimento. Os tipos válidos vêm da política do perfil "
                                                   "(ado_type_schema, blocking.policy.kinds)."},
         "responsible": {"type": "string", "description": "Nome ou e-mail de quem resolve. Omita para o padrão."},
         "date": {"type": "string", "description": "AAAA-MM-DD. Omita para o padrão (hoje)."},
         "forecast": {"type": "string", "description": "AAAA-MM-DD. Omita para o padrão da política."},
         "mention_responsible": {"type": "boolean", "description": "Padrão true: menciona o responsável."},
         "project": PROJECT, "team": TEAM}, ("id", "reason", "kind")), read_only=False)
def t_prepare_block(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.batch.blocking.prepare_block(
        ctx.org(a), a["id"], a["reason"], a.get("waiting_for"), a["kind"], a.get("responsible"), a.get("date"),
        a.get("forecast"), a.get("mention_responsible", True), a.get("project"), a.get("team"))


@tool("ado_prepare_unblock", "Preparar fim do impedimento",
      "Fase 1: desmarca o impedimento (flag no valor de desligado) e prepara o comentário de resolução. Mantém os "
      "campos do impedimento como histórico, salvo os que o perfil manda limpar. Não muda o estado.",
      S({"org": ORG, "id": ITEM_ID, "resolution": {"type": "string", "description": "Como o impedimento foi resolvido."},
         "project": PROJECT, "team": TEAM}, ("id", "resolution")), read_only=False)
def t_prepare_unblock(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.batch.blocking.prepare_unblock(ctx.org(a), a["id"], a["resolution"], a.get("project"),
                                                     a.get("team"))


@tool("ado_apply", "Aplicar prévia",
      "Fase 2: envia ao Azure DevOps a prévia aprovada. Use os apply_args exatos do ado_prepare_*, só depois do OK "
      "explícito da pessoa para aquela prévia. Nunca chame sem OK. Em lote parcial, repetir envia só os pendentes.",
      S({"preview_id": {"type": "string"}, "hash": {"type": "string"},
         "operation": {"enum": ["create", "update", "comment", "attach", "move", "create_sprint", "delete",
                                "rollover", "comments_batch", "block", "unblock"]}, "org": {"type": "string"},
         "target_id": ITEM_ID, "type": {"type": "string"}, "title": {"type": "string"},
         "file_name": {"type": "string"}, "iteration": {"type": "string"}, "count": {"type": "integer"}},
        ("preview_id", "hash", "operation", "org")), read_only=False)
def t_apply(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    org = ctx.org(a)
    return ctx.writes.apply(a["preview_id"], a["hash"], a["operation"], org, a.get("target_id"), a.get("type"),
                            a.get("title"), a.get("file_name"), a.get("iteration"), a.get("count"))


@tool("ado_reconcile", "Reconciliar envio incerto",
      "Depois de timeout, erro de rede ou 5xx num ado_apply: procura o efeito no Azure DevOps. Resultado em "
      "data.outcome: applied, not_applied, ambiguous ou partial.",
      S({"preview_id": {"type": "string"}}, ("preview_id",)), read_only=False)
def t_reconcile(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.writes.reconcile(a["preview_id"])


# --- despacho ---------------------------------------------------------------------------------------------

class UnknownTool(KeyError):
    pass


def tool_list() -> List[Dict[str, Any]]:
    out = []
    for t in TOOLS.values():
        ann = {"title": t.title, "readOnlyHint": t.read_only, "openWorldHint": True}
        if not t.read_only:
            ann["destructiveHint"] = t.name == "ado_apply"
        out.append({"name": t.name, "title": t.title, "description": t.description,
                    "inputSchema": t.schema, "annotations": ann})
    return out


def call_tool(ctx: Context, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Executa a ferramenta e devolve sempre um envelope. Só levanta `UnknownTool`."""
    t = TOOLS.get(name)
    if t is None:
        raise UnknownTool(name)
    args = dict(arguments or {})
    errs = validate_args(t.schema, args)
    if errs:
        return envelope("failed", "INVALID_ARGUMENTS", {"errors": errs[:10]}, "Corrija os argumentos e chame de novo.")
    ctx.reload()
    if ctx.config_error is not None and name != "ado_status":
        return envelope("needs_setup", "CONFIG_INVALID", None, str(ctx.config_error))
    try:
        env = t.handler(ctx, args)
    except ToolError as e:
        env = e.env
    except (AdoApiError, NeedsDecision, AuthError, HttpError, VaultError, cfgmod.ConfigError) as e:
        env = error_envelope(e)
    except Exception as e:  # noqa: BLE001
        secrets = _secrets(ctx)
        env = envelope("failed", "INTERNAL_ERROR", {"error": type(e).__name__},
                       "Erro interno: " + redact(str(e), secrets, limit=200), None)
        if name == "ado_apply":
            env["next_action"] = ("Erro interno depois do envio: o efeito pode ter ocorrido. Rode ado_reconcile com "
                                  f"preview_id={args.get('preview_id')} antes de repetir o ado_apply.")
        elif name == "ado_reconcile":
            env["next_action"] = "Erro interno na reconciliação. Confira o item no Azure DevOps antes de repetir."
    for k, default in (("reason_code", None), ("data", {}), ("next_action", None), ("diagnostic", {})):
        env.setdefault(k, default)
    return env


def _secrets(ctx: Context) -> tuple:
    try:
        return tuple(ctx.auth.known_secrets())
    except Exception:  # noqa: BLE001
        return ()


def _no_secret(o: Any) -> Any:
    raise TypeError(f"valor não serializável: {type(o).__name__}")


def render(ctx: Context, env: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Texto JSON do envelope sem segredo em memória e o objeto correspondente."""
    try:
        text = json.dumps(env, ensure_ascii=False, default=_no_secret)
        # patterns=False: o AUTH_RE trocaria 'basic settings' num título e quebraria o ado_apply (PREVIEW_MISMATCH)
        text = redact(text, _secrets(ctx), limit=10 ** 9, patterns=False)
        return text, json.loads(text)
    except (TypeError, ValueError):
        safe = envelope("failed", "INTERNAL_ERROR", {"error": "resposta não serializável"},
                        "Erro interno ao montar a resposta.")
        return json.dumps(safe, ensure_ascii=False), safe

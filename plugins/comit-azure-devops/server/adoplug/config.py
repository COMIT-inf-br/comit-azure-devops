"""Config local (config.json) e leitura de perfis embutidos e aprendidos.

A config nunca guarda segredo: `save_config` recusa chaves com nome de
segredo. O PAT de contingência entra só como referência (`pat_ref`).
Os perfis voltam como dict cru; a validação de esquema é do validador de perfil.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from . import paths

SCHEMA_VERSION = 1
DEFAULT_OS_SERVICE = "comit-azure-devops"
AUTH_MODES = ("az", "pat")

# Nome de org do Azure DevOps: letras, dígitos e hífen, sem hífen nas pontas.
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$")
FORBIDDEN_KEY_RE = re.compile(r"^(pat|token|password|secret|access_?token|senha)$", re.I)


class ConfigError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def default_config() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "default_org": None, "az_path": None, "orgs": {}}


def default_org_entry(org: str) -> Dict[str, Any]:
    return {
        "auth": "az",
        "tenant": None,
        "pat_ref": {"nordpass_title": None, "nordpass_folder": None,
                    "os_service": DEFAULT_OS_SERVICE, "os_account": org},
        "default_project": None,
        "me": {},
        "defaults": {},
    }


def validate_org(org: str) -> str:
    if not isinstance(org, str) or not ORG_RE.match(org):
        raise ConfigError("INVALID_ORG", "Nome de organização inválido.")
    return org


def normalize_org(text: str) -> str:
    """Aceita o nome da org ou uma URL dev.azure.com/<org>/... e devolve o nome.

    URL *.visualstudio.com é recusada: o servidor só fala com dev.azure.com (I1).
    """
    t = (text or "").strip()
    if "://" not in t and "/" not in t:
        return validate_org(t)
    if "://" not in t:
        t = "https://" + t
    u = urlparse(t)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or host != "dev.azure.com" or u.username or u.password or u.port:
        raise ConfigError("UNSUPPORTED_HOST", "Use uma URL https://dev.azure.com/<org>/...")
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        raise ConfigError("INVALID_ORG", "A URL não tem a organização.")
    return validate_org(unquote(parts[0]))


def _scan_forbidden(obj: Any, where: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and FORBIDDEN_KEY_RE.match(k):
                raise ConfigError("SECRET_IN_CONFIG", f"A config não pode ter a chave '{where}{k}'.")
            _scan_forbidden(v, f"{where}{k}.")
    elif isinstance(obj, list):
        for v in obj:
            _scan_forbidden(v, where)


def _merge_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = default_config()
    out.update({k: v for k, v in cfg.items() if k != "orgs"})
    orgs = {}
    for name, entry in (cfg.get("orgs") or {}).items():
        base = default_org_entry(name)
        if isinstance(entry, dict):
            pat_ref = dict(base["pat_ref"])
            pat_ref.update(entry.get("pat_ref") or {})
            base.update(entry)
            base["pat_ref"] = pat_ref
        orgs[name] = base
    out["orgs"] = orgs
    return out


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Lê a config; arquivo ausente devolve a config padrão."""
    p = path or paths.config_file()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_config()
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError("CONFIG_INVALID", f"config.json inválido (linha {e.lineno}).") from None
    if not isinstance(cfg, dict):
        raise ConfigError("CONFIG_INVALID", "config.json precisa ser um objeto.")
    _scan_forbidden(cfg)
    return _merge_defaults(cfg)


def validate_config(cfg: Dict[str, Any]) -> None:
    _scan_forbidden(cfg)
    for name, entry in (cfg.get("orgs") or {}).items():
        validate_org(name)
        if entry.get("auth", "az") not in AUTH_MODES:
            raise ConfigError("CONFIG_INVALID", f"auth da org {name} precisa ser az ou pat.")
    d = cfg.get("default_org")
    if d is not None and d not in (cfg.get("orgs") or {}):
        raise ConfigError("CONFIG_INVALID", "default_org não está em orgs.")


def save_config(cfg: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Valida e grava a config de forma atômica, com permissão 0600."""
    validate_config(cfg)
    p = path or paths.config_file()
    data = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    paths.write_private(p, data)
    return p


def find_org(cfg: Dict[str, Any], org: str) -> Optional[str]:
    """Devolve a chave configurada da org, sem diferenciar maiúsculas."""
    if not org:
        return None
    for name in (cfg.get("orgs") or {}):
        if name.casefold() == org.casefold():
            return name
    return None


def get_org(cfg: Dict[str, Any], org: str) -> Dict[str, Any]:
    name = find_org(cfg, org)
    if name is None:
        raise ConfigError("ORG_NOT_CONFIGURED", f"A organização {org} não está configurada.")
    return cfg["orgs"][name]


def configured_orgs(cfg: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple((cfg.get("orgs") or {}).keys())


def set_org(cfg: Dict[str, Any], org: str, **fields: Any) -> Dict[str, Any]:
    """Devolve uma cópia da config com a org criada ou atualizada."""
    validate_org(org)
    _scan_forbidden(fields)
    out = copy.deepcopy(cfg)
    orgs = out.setdefault("orgs", {})
    name = find_org(out, org) or org
    entry = orgs.get(name) or default_org_entry(name)
    for k, v in fields.items():
        if k == "pat_ref" and isinstance(v, dict):
            entry["pat_ref"] = {**entry.get("pat_ref", {}), **v}
        else:
            entry[k] = v
    orgs[name] = entry
    if not out.get("default_org"):
        out["default_org"] = name
    return out


# --- perfis -----------------------------------------------------------------

def _read_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise ConfigError("PROFILE_INVALID", f"Perfil {p.name} inválido (linha {e.lineno}).") from None


def load_profile(org: str, learned_dir: Optional[Path] = None,
                 embedded_dir: Optional[Path] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Devolve (perfil, fonte). Fonte: 'learned', 'embedded' ou 'absent'.

    Precedência: aprendido (local) > embutido. O embutido é só leitura.
    """
    validate_org(org)
    ldir = learned_dir or paths.learned_profiles_dir()
    edir = embedded_dir or paths.embedded_profiles_dir()
    for d, src in ((ldir, "learned"), (edir, "embedded")):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.stem.casefold() == org.casefold():
                prof = _read_json(f)
                if prof is not None:
                    return prof, src
    return None, "absent"


def save_learned_profile(org: str, profile: Dict[str, Any], learned_dir: Optional[Path] = None) -> Path:
    """Grava o perfil aprendido na config local (nunca no plugin)."""
    validate_org(org)
    _scan_forbidden(profile)
    ldir = learned_dir or paths.learned_profiles_dir()
    p = ldir / f"{org}.json"
    data = json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    paths.write_private(p, data)
    return p

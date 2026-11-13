"""Leitura e gravação do PAT de contingência: NordPass (`np`) e cofre do SO.

Ordem de leitura: NordPass, depois o cofre do SO (Keychain no macOS,
Credential Manager no Windows, secret-tool no Linux). Nenhuma variável de
ambiente é fonte de segredo (I3). O valor fica só na memória, dentro de
`Secret` (I2). O servidor não destrava o NordPass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

from . import paths

TIMEOUT = 30
SECURITY_PATH = "/usr/bin/security"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

MSG_NP_LOCKED = "O NordPass está travado. Rode `np unlock` no terminal e repita."
MSG_NP_SIGNIN = "A sessão do NordPass venceu. Rode `np signin` no terminal e repita."


class Secret:
    """Guarda um valor secreto. `str` e `repr` nunca mostram o valor."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value:
            raise ValueError("segredo vazio")
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._v == self._v

    def __hash__(self) -> int:
        return hash(("Secret", self._v))

    def __reduce__(self):
        raise TypeError("Secret não é serializável")


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def redact(text: str, secrets: Sequence[object] = (), limit: int = 1000, patterns: bool = True) -> str:
    """Tira valores secretos e, com `patterns`, JWTs e cabeçalhos de autenticação; corta em `limit`."""
    import base64

    t = "" if text is None else str(text)
    for s in secrets:
        v = s.reveal() if isinstance(s, Secret) else s
        if not v:
            continue
        forms = {v, base64.b64encode((":" + v).encode()).decode()}
        for f in forms:
            t = t.replace(f, "***")
    if patterns:
        t = JWT_RE.sub("***", t)
        t = AUTH_RE.sub(lambda m: m.group(1) + " ***", t)
    if len(t) > limit:
        t = t[:limit] + "…"
    return t


class VaultError(RuntimeError):
    def __init__(self, reason_code: str, message: str, tried: Optional[List[str]] = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.tried = tried or []


@dataclass
class PatResult:
    secret: Secret
    source: str  # nordpass | keychain | credman | secret-tool
    notes: List[str] = field(default_factory=list)


Runner = Callable[..., "subprocess.CompletedProcess"]


def _run(argv: Sequence[str], input_text: Optional[str] = None, runner: Optional[Runner] = None,
         timeout: int = TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """Roda sem shell; stdin fechado quando não há entrada. None em timeout ou binário ausente."""
    run = runner or subprocess.run
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
              timeout=timeout, shell=False)
    if input_text is None:
        kw["stdin"] = subprocess.DEVNULL
    else:
        kw["input"] = input_text
    try:
        return run(list(argv), **kw)
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError):
        return None


# --- NordPass ---------------------------------------------------------------

def find_np(which: Callable[[str], Optional[str]] = shutil.which, home: Optional[os.PathLike] = None,
            env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
            exists: Callable[[str], bool] = os.path.isfile) -> Optional[str]:
    env = os.environ if env is None else env
    h = paths.home_dir(home)
    cands: List[Optional[str]] = [which("np")]
    s = paths.system(platform)
    if s == "windows":
        la = env.get("LOCALAPPDATA")
        if la:
            cands.append(str(Path(la) / "Programs" / "NordPass CLI" / "np.exe"))
    else:
        cands += ["/opt/homebrew/bin/np", "/usr/local/bin/np", str(h / ".local" / "bin" / "np")]
    for c in cands:
        if c and os.path.isabs(c) and exists(c):
            return c
    return None


def _np_error(cp: subprocess.CompletedProcess) -> str:
    """Classifica a falha do np: locked, signin ou error."""
    text = f"{cp.stdout or ''}\n{cp.stderr or ''}"
    code = ""
    for chunk in (cp.stderr, cp.stdout):
        try:
            j = json.loads(chunk or "")
            code = str(((j or {}).get("error") or {}).get("code") or "")
            if code:
                break
        except (json.JSONDecodeError, AttributeError):
            continue
    low = (code + " " + text).lower()
    if "lock" in low:
        return "locked"
    if "not_authenticated" in low or "signin" in low or "sign in" in low or "expired" in low:
        return "signin"
    return "error"


def read_nordpass(title: str, np_cmd: Sequence[str], folder: Optional[str] = None,
                  runner: Optional[Runner] = None) -> Secret:
    """Lê o campo password do item com o título exato. Nunca usa --field nem --copy."""
    if not title or title.startswith("-") or (folder or "").startswith("-"):
        raise VaultError("NP_NO_ITEM", "Título ou pasta do NordPass ausente ou inválido na config.")
    argv = list(np_cmd) + ["item", "list", "--format", "json", "--search", title]
    if folder:
        argv += ["--folder", folder]
    cp = _run(argv, runner=runner)
    if cp is None:
        raise VaultError("NP_FAILED", "O NordPass não respondeu.")
    if cp.returncode != 0:
        kind = _np_error(cp)
        if kind == "locked":
            raise VaultError("NP_LOCKED", MSG_NP_LOCKED)
        if kind == "signin":
            raise VaultError("NP_SIGNIN", MSG_NP_SIGNIN)
        raise VaultError("NP_FAILED", f"O NordPass saiu com código {cp.returncode}.")
    try:
        items = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        raise VaultError("NP_FAILED", "Resposta do NordPass ilegível.") from None
    if isinstance(items, dict):
        items = items.get("items") or []
    hits = [i for i in items if isinstance(i, dict) and i.get("title") == title]
    if not hits:
        raise VaultError("NP_NO_ITEM", "O NordPass não tem o item com esse título.")
    if len(hits) > 1:
        raise VaultError("NP_AMBIGUOUS", "O NordPass tem mais de um item com esse título.")
    item_id = str(hits[0].get("id") or "")
    if not item_id or item_id.startswith("-"):
        raise VaultError("NP_FAILED", "Item do NordPass sem id.")
    cp = _run(list(np_cmd) + ["item", "get", item_id, "--format", "json"], runner=runner)
    if cp is None or cp.returncode != 0:
        if cp is not None and _np_error(cp) == "locked":
            raise VaultError("NP_LOCKED", MSG_NP_LOCKED)
        raise VaultError("NP_FAILED", "O NordPass não devolveu o item.")
    try:
        pw = (json.loads(cp.stdout or "{}") or {}).get("password")
    except (json.JSONDecodeError, AttributeError):
        raise VaultError("NP_FAILED", "Resposta do NordPass ilegível.") from None
    if not isinstance(pw, str) or not pw:
        raise VaultError("NP_NO_ITEM", "O item do NordPass não tem senha.")
    return Secret(pw)


# --- cofre do SO --------------------------------------------------------------

def _check_value(value: str) -> None:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise VaultError("INVALID_VALUE", "Valor vazio ou com caractere de controle.")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def keychain_read(service: str, account: str, security: Sequence[str] = (SECURITY_PATH,),
                  runner: Optional[Runner] = None) -> Optional[Secret]:
    cp = _run(list(security) + ["find-generic-password", "-s", service, "-a", account, "-w"], runner=runner)
    if cp is None or cp.returncode != 0:  # 44 = item ausente
        return None
    v = cp.stdout or ""
    if v.endswith("\n"):
        v = v[:-1]
    return Secret(v) if v else None


def keychain_write(service: str, account: str, value: str, security: Sequence[str] = (SECURITY_PATH,),
                   runner: Optional[Runner] = None) -> None:
    """Grava pelo stdin do `security -i`: o valor não aparece no argv."""
    _check_value(value)
    line = f"add-generic-password -U -s {_q(service)} -a {_q(account)} -w {_q(value)}\n"
    _run(list(security) + ["-i"], input_text=line, runner=runner)
    got = keychain_read(service, account, security, runner)
    if got is None or got.reveal() != value:
        raise VaultError("WRITE_UNCONFIRMED", "O Keychain não confirmou a gravação.")


def keychain_delete(service: str, account: str, security: Sequence[str] = (SECURITY_PATH,),
                    runner: Optional[Runner] = None) -> bool:
    cp = _run(list(security) + ["delete-generic-password", "-s", service, "-a", account], runner=runner)
    return bool(cp is not None and cp.returncode == 0)


def secret_tool_read(service: str, account: str, secret_tool: Sequence[str],
                     runner: Optional[Runner] = None) -> Optional[Secret]:
    cp = _run(list(secret_tool) + ["lookup", "service", service, "account", account], runner=runner)
    if cp is None or cp.returncode != 0 or not cp.stdout:
        return None
    return Secret(cp.stdout)  # o lookup não acrescenta quebra de linha


def secret_tool_write(service: str, account: str, value: str, secret_tool: Sequence[str],
                      runner: Optional[Runner] = None) -> None:
    """Grava com o valor pelo stdin, fora do argv."""
    _check_value(value)
    _run(list(secret_tool) + ["store", f"--label={service}/{account}", "service", service, "account", account],
         input_text=value, runner=runner)
    got = secret_tool_read(service, account, secret_tool, runner)
    if got is None or got.reveal() != value:
        raise VaultError("WRITE_UNCONFIRMED", "O secret-tool não confirmou a gravação.")


def secret_tool_delete(service: str, account: str, secret_tool: Sequence[str],
                       runner: Optional[Runner] = None) -> bool:
    cp = _run(list(secret_tool) + ["clear", "service", service, "account", account], runner=runner)
    return bool(cp is not None and cp.returncode == 0)


def _win():
    """Carrega as funções do Credential Manager só no Windows."""
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return ctypes, CREDENTIALW, adv


def credman_read(target: str) -> Optional[Secret]:
    ctypes, CREDENTIALW, adv = _win()
    ptr = ctypes.POINTER(CREDENTIALW)()
    if not adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        c = ptr.contents
        n = int(c.CredentialBlobSize)
        if n == 0:
            return None
        raw = ctypes.string_at(c.CredentialBlob, n)
        v = raw.decode("utf-16-le", errors="strict") if n % 2 == 0 else raw.decode("utf-8")
        return Secret(v) if v else None
    except UnicodeDecodeError:
        return None
    finally:
        adv.CredFree(ptr)


def credman_write(target: str, value: str, user: str = "comit-azure-devops") -> None:
    _check_value(value)
    ctypes, CREDENTIALW, adv = _win()
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise VaultError("WRITE_FAILED", "O Credential Manager recusou a gravação.")
    got = credman_read(target)
    if got is None or got.reveal() != value:
        raise VaultError("WRITE_UNCONFIRMED", "O Credential Manager não confirmou a gravação.")


def credman_delete(target: str) -> bool:
    _, _, adv = _win()
    return bool(adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0))


# --- fachada ----------------------------------------------------------------------

@dataclass
class VaultTools:
    """Comandos dos cofres. Os testes trocam por fakes; em produção, valores padrão."""
    np_cmd: Optional[Sequence[str]] = None          # None = procurar com find_np
    security_cmd: Sequence[str] = (SECURITY_PATH,)
    secret_tool_cmd: Optional[Sequence[str]] = None  # None = procurar no PATH
    platform: Optional[str] = None
    runner: Optional[Runner] = None
    use_nordpass: bool = True

    def np(self) -> Optional[List[str]]:
        if self.np_cmd is not None:
            return list(self.np_cmd) or None
        if not self.use_nordpass:
            return None
        p = find_np(platform=self.platform)
        return [p] if p else None

    def secret_tool(self) -> Optional[List[str]]:
        if self.secret_tool_cmd is not None:
            return list(self.secret_tool_cmd) or None
        p = shutil.which("secret-tool")
        return [p] if p else None


def os_store_name(platform: Optional[str] = None) -> str:
    return {"mac": "keychain", "windows": "credman", "linux": "secret-tool"}[paths.system(platform)]


def read_os(service: str, account: str, tools: Optional[VaultTools] = None) -> Optional[Secret]:
    t = tools or VaultTools()
    s = paths.system(t.platform)
    if s == "mac":
        return keychain_read(service, account, t.security_cmd, t.runner)
    if s == "windows":
        return credman_read(f"{service}/{account}")
    st = t.secret_tool()
    return secret_tool_read(service, account, st, t.runner) if st else None


def write_os(service: str, account: str, value: str, tools: Optional[VaultTools] = None) -> str:
    """Grava no cofre do SO, relê e devolve o nome do cofre usado."""
    t = tools or VaultTools()
    s = paths.system(t.platform)
    if s == "mac":
        keychain_write(service, account, value, t.security_cmd, t.runner)
    elif s == "windows":
        credman_write(f"{service}/{account}", value)
    else:
        st = t.secret_tool()
        if not st:
            raise VaultError("NO_OS_VAULT", "Sem secret-tool neste Linux. Instale o libsecret-tools.")
        secret_tool_write(service, account, value, st, t.runner)
    return os_store_name(t.platform)


def delete_os(service: str, account: str, tools: Optional[VaultTools] = None) -> bool:
    t = tools or VaultTools()
    s = paths.system(t.platform)
    if s == "mac":
        return keychain_delete(service, account, t.security_cmd, t.runner)
    if s == "windows":
        return credman_delete(f"{service}/{account}")
    st = t.secret_tool()
    return secret_tool_delete(service, account, st, t.runner) if st else False


def read_pat(org: str, pat_ref: Optional[Mapping[str, object]], tools: Optional[VaultTools] = None) -> PatResult:
    """Lê o PAT da org: NordPass (se houver título e np) e depois o cofre do SO.

    Sem PAT em nenhum dos dois, levanta VaultError com o motivo mais útil:
    NordPass travado ou vencido tem prioridade sobre "não encontrado".
    """
    t = tools or VaultTools()
    ref = dict(pat_ref or {})
    notes: List[str] = []
    np_error: Optional[VaultError] = None
    title = ref.get("nordpass_title")
    if title:
        npc = t.np()
        if npc is None:
            notes.append("nordpass:absent")
        else:
            try:
                sec = read_nordpass(str(title), npc, folder=ref.get("nordpass_folder") or None, runner=t.runner)
                return PatResult(sec, "nordpass", notes)
            except VaultError as e:
                np_error = e
                notes.append("nordpass:" + e.reason_code)
    service = str(ref.get("os_service") or "comit-azure-devops")
    account = str(ref.get("os_account") or org)
    sec = read_os(service, account, t)
    if sec is not None:
        return PatResult(sec, os_store_name(t.platform), notes)
    notes.append(os_store_name(t.platform) + ":absent")
    if np_error is not None and np_error.reason_code in ("NP_LOCKED", "NP_SIGNIN"):
        raise VaultError(np_error.reason_code, str(np_error), notes)
    raise VaultError("PAT_NOT_FOUND", f"Nenhum PAT encontrado para a organização {org}.", notes)

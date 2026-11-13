"""Azure CLI: localizar o binário, pedir o token do Azure DevOps e fazer login.

O `az` só entrega o token; o REST é do `http`. O binário é chamado pelo
caminho absoluto, sem shell (I4). O filho não recebe AZURE_DEVOPS_EXT_PAT (I3).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from . import paths
from .vault import Secret, redact

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
TOKEN_TIMEOUT = 60
TENANT_RE = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                       r"|[A-Za-z0-9][A-Za-z0-9.-]{0,252}\.[A-Za-z]{2,})$")
# Variáveis de segredo que o filho não pode herdar.
CHILD_ENV_DROP = ("AZURE_DEVOPS_EXT_PAT", "SYSTEM_ACCESSTOKEN")
DEVICE_CODE_RE = re.compile(r"(https://\S+)\S*\s.*?\b(?:code|código)\s+([A-Z0-9-]{6,})", re.I | re.S)


class AzError(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class AzToken:
    secret: Secret
    expires_on: float  # segundos desde a época
    tenant: Optional[str] = None

    def __repr__(self) -> str:
        return f"AzToken(secret=***, expires_on={self.expires_on}, tenant={self.tenant})"


def find_az(config_path: Optional[str] = None, which: Callable[[str], Optional[str]] = shutil.which,
            home: Optional[os.PathLike] = None, env: Optional[Mapping[str, str]] = None,
            platform: Optional[str] = None, exists: Callable[[str], bool] = os.path.isfile) -> Optional[str]:
    """Ordem do I4: config `az_path`; PATH; caminhos de instalação conhecidos."""
    env = os.environ if env is None else env
    h = paths.home_dir(home)
    s = paths.system(platform)
    cands: List[Optional[str]] = [config_path, which("az")]
    if s == "windows":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = env.get(var)
            if base:
                cands.append(str(Path(base) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd"))
        cands.append(str(h / ".local" / "bin" / "az.exe"))
        cands.append(str(h / ".local" / "bin" / "az.cmd"))
    else:
        cands += ["/opt/homebrew/bin/az", "/usr/local/bin/az", str(h / ".local" / "bin" / "az"), "/usr/bin/az"]
    for c in cands:
        if c and os.path.isabs(c) and exists(c):
            return os.path.abspath(c)
    return None


def child_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Ambiente do filho: o do processo, sem as variáveis de segredo."""
    src = dict(os.environ if base is None else base)
    drop = {n.upper() for n in CHILD_ENV_DROP}
    return {k: v for k, v in src.items() if k.upper() not in drop}


def validate_tenant(tenant: Optional[str]) -> Optional[str]:
    if tenant is None:
        return None
    if not isinstance(tenant, str) or not TENANT_RE.match(tenant):
        raise AzError("INVALID_TENANT", "Tenant inválido.")
    return tenant


def _parse_expiry(data: Mapping[str, object]) -> float:
    eo = data.get("expires_on")
    if isinstance(eo, (int, float)) or (isinstance(eo, str) and eo.isdigit()):
        return float(eo)
    # az antigo: hora local sem fuso
    s = data.get("expiresOn")
    if isinstance(s, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                pass
    return time.time() + 300.0


class AzCli:
    """Chamadas ao `az`. `cmd` é o argv-prefixo (caminho absoluto ou fake de teste)."""

    def __init__(self, cmd: Sequence[str], runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
                 popen: Optional[Callable[..., subprocess.Popen]] = None,
                 env: Optional[Mapping[str, str]] = None):
        if not cmd or not os.path.isabs(cmd[0]):
            raise AzError("AZ_NOT_FOUND", "O caminho do az precisa ser absoluto.")
        self.cmd = list(cmd)
        self._run = runner or subprocess.run
        self._popen = popen or subprocess.Popen
        self._env = env

    @classmethod
    def locate(cls, config_path: Optional[str] = None, **kw) -> "AzCli":
        p = find_az(config_path)
        if not p:
            raise AzError("AZ_NOT_FOUND", "Azure CLI não encontrado. Instale com `uv tool install azure-cli`.")
        return cls([p], **kw)

    def _call(self, args: Sequence[str], timeout: int = TOKEN_TIMEOUT) -> subprocess.CompletedProcess:
        try:
            # sem check=True: o CalledProcessError guardaria a saída com o token
            return self._run(self.cmd + list(args), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                             shell=False, env=child_env(self._env))
        except subprocess.TimeoutExpired:
            raise AzError("AZ_TIMEOUT", "O Azure CLI não respondeu a tempo.") from None
        except (FileNotFoundError, PermissionError, OSError):
            raise AzError("AZ_NOT_FOUND", "Não consegui executar o Azure CLI.") from None

    @staticmethod
    def _fail(cp: subprocess.CompletedProcess) -> AzError:
        err = redact(cp.stderr or "", limit=600)
        low = err.lower()
        if ("az login" in low or "aadsts" in low or "please run" in low
                or "no subscription" in low or "refresh token" in low):
            return AzError("AZ_NOT_LOGGED_IN", "Login do Azure CLI ausente ou vencido. " + err)
        return AzError("AZ_FAILED", f"O Azure CLI saiu com código {cp.returncode}. " + err)

    def get_token(self, tenant: Optional[str] = None) -> AzToken:
        args = ["account", "get-access-token", "--resource", ADO_RESOURCE, "--output", "json"]
        t = validate_tenant(tenant)
        if t:
            args += ["--tenant", t]
        cp = self._call(args)
        if cp.returncode != 0:
            raise self._fail(cp)
        try:
            data = json.loads(cp.stdout or "")
            tok = data.get("accessToken")
        except (json.JSONDecodeError, AttributeError):
            raise AzError("AZ_FAILED", "Resposta do Azure CLI ilegível.") from None
        if not isinstance(tok, str) or not tok:
            raise AzError("AZ_FAILED", "O Azure CLI não devolveu token.")
        return AzToken(Secret(tok), _parse_expiry(data), data.get("tenant") or t)

    def version(self) -> Optional[str]:
        try:
            cp = self._call(["version", "--output", "json"], timeout=30)
            return (json.loads(cp.stdout or "{}") or {}).get("azure-cli") if cp.returncode == 0 else None
        except (AzError, json.JSONDecodeError, AttributeError):
            return None

    def logged_tenants(self) -> List[str]:
        """Tenants com conta no cache do `az` (sem dados pessoais no retorno)."""
        try:
            cp = self._call(["account", "list", "--all", "--output", "json"], timeout=30)
            if cp.returncode != 0:
                return []
            rows = json.loads(cp.stdout or "[]")
        except (AzError, json.JSONDecodeError):
            return []
        out = []
        for r in rows if isinstance(rows, list) else []:
            t = isinstance(r, dict) and r.get("tenantId")
            if isinstance(t, str) and t not in out:
                out.append(t)
        return out

    def start_login(self, tenant: str, mode: str = "browser", wait_code: float = 30.0) -> "LoginHandle":
        """Inicia `az login` no host. Em device_code, espera a URL e o código no stderr."""
        if mode not in ("browser", "device_code"):
            raise AzError("INVALID_MODE", "Modo de login precisa ser browser ou device_code.")
        if not tenant:
            raise AzError("INVALID_TENANT", "Tenant obrigatório para o login.")
        args = ["login", "--tenant", validate_tenant(tenant), "--allow-no-subscriptions", "--output", "none"]
        if mode == "device_code":
            args.append("--use-device-code")
        try:
            proc = self._popen(self.cmd + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                               shell=False, env=child_env(self._env))
        except OSError:
            raise AzError("AZ_NOT_FOUND", "Não consegui executar o Azure CLI.") from None
        h = LoginHandle(proc, mode)
        if mode == "device_code":
            h.wait_device_code(wait_code)
        return h


class LoginHandle:
    """Processo de login em andamento. Lê o stderr numa thread para não travar o filho."""

    def __init__(self, proc: subprocess.Popen, mode: str):
        self.proc = proc
        self.mode = mode
        self.url: Optional[str] = None
        self.code: Optional[str] = None
        self._buf: List[str] = []
        self._found = threading.Event()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for line in stream:
            self._buf.append(line)
            if self.code is None:
                m = DEVICE_CODE_RE.search("".join(self._buf[-5:]))
                if m:
                    self.url, self.code = m.group(1).rstrip(".,"), m.group(2)
                    self._found.set()
        self._found.set()

    def wait_device_code(self, timeout: float) -> None:
        self._found.wait(timeout)

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        rc = self.proc.wait(timeout)
        self._t.join(1)
        return rc

    def status(self) -> Dict[str, object]:
        rc = self.poll()
        out: Dict[str, object] = {"mode": self.mode,
                                  "state": "running" if rc is None else ("ok" if rc == 0 else "failed")}
        if self.url:
            out["url"] = self.url
        if self.code:
            out["code"] = self.code
        if rc not in (None, 0):
            out["message"] = redact("".join(self._buf[-10:]), limit=600)
        return out

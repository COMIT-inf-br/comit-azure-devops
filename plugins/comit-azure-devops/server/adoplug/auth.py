"""Escolha da credencial por org e cache do token na memória do processo.

Modo `az` (padrão): token Entra pedido ao Azure CLI, guardado até perto do
`expires_on`. Modo `pat` (contingência): PAT lido do NordPass ou do cofre do
SO. Nunca há troca automática de modo, e nenhuma variável de ambiente é
fonte de segredo (I3). Nada aqui grava em disco (I2).
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Tuple

from . import config as cfgmod
from .azcli import AzCli, AzError, AzToken
from .vault import PatResult, Secret, VaultError, VaultTools, read_pat

REFRESH_MARGIN = 300.0  # renova o token 5 min antes de vencer


class AuthError(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class Credential:
    """Credencial pronta para o cabeçalho Authorization."""
    kind: str      # "az" | "pat"
    source: str    # "az" | nordpass | keychain | credman | secret-tool
    secret: Secret
    expires_on: Optional[float] = None

    def header(self) -> Secret:
        v = self.secret.reveal()
        if self.kind == "pat":
            return Secret("Basic " + base64.b64encode((":" + v).encode("utf-8")).decode("ascii"))
        return Secret("Bearer " + v)

    def __repr__(self) -> str:
        return f"Credential(kind={self.kind}, source={self.source}, secret=***)"


class AuthManager:
    """Entrega credenciais por org. Token do `az` por tenant; PAT por org."""

    def __init__(self, cfg: Mapping[str, object],
                 az_factory: Optional[Callable[[], AzCli]] = None,
                 vault_tools: Optional[VaultTools] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._az_factory = az_factory or (lambda: AzCli.locate(cfg.get("az_path")))  # type: ignore[arg-type]
        self._az: Optional[AzCli] = None
        self._vault = vault_tools or VaultTools()
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: Dict[Optional[str], AzToken] = {}
        self._pats: Dict[str, PatResult] = {}

    def _org(self, org: str) -> Tuple[str, Mapping[str, object]]:
        name = cfgmod.find_org(self.cfg, org)  # type: ignore[arg-type]
        if name is None:
            raise AuthError("ORG_NOT_CONFIGURED", f"A organização {org} não está configurada.")
        return name, self.cfg["orgs"][name]  # type: ignore[index]

    def mode(self, org: str) -> str:
        return str(self._org(org)[1].get("auth") or "az")

    def az(self) -> AzCli:
        if self._az is None:
            try:
                self._az = self._az_factory()
            except AzError as e:
                raise AuthError(e.reason_code, str(e)) from None
        return self._az

    def get_credential(self, org: str) -> Credential:
        name, entry = self._org(org)
        mode = str(entry.get("auth") or "az")
        with self._lock:
            if mode == "pat":
                return self._pat(name, entry)
            if mode == "az":
                return self._az_token(entry.get("tenant"))  # type: ignore[arg-type]
        raise AuthError("CONFIG_INVALID", f"Modo de autenticação desconhecido na org {name}.")

    def get_token(self, org: str) -> Secret:
        """Valor cru da credencial (token Entra ou PAT), só para uso em memória."""
        return self.get_credential(org).secret

    def authorization(self, org: str) -> Secret:
        return self.get_credential(org).header()

    def invalidate(self, org: str) -> None:
        """Esquece a credencial da org (ex.: depois de um 401)."""
        name, entry = self._org(org)
        with self._lock:
            self._pats.pop(name, None)
            self._tokens.pop(entry.get("tenant"), None)  # type: ignore[arg-type]

    def known_secrets(self) -> Tuple[Secret, ...]:
        """Segredos em memória, para o `redact` limpar saídas."""
        with self._lock:
            return tuple(t.secret for t in self._tokens.values()) + tuple(p.secret for p in self._pats.values())

    def _az_token(self, tenant: Optional[str]) -> Credential:
        tok = self._tokens.get(tenant)
        if tok is None or tok.expires_on - REFRESH_MARGIN <= self._clock():
            try:
                tok = self.az().get_token(tenant)
            except AzError as e:
                raise AuthError(e.reason_code, str(e)) from None
            self._tokens[tenant] = tok
        return Credential("az", "az", tok.secret, tok.expires_on)

    def _pat(self, name: str, entry: Mapping[str, object]) -> Credential:
        res = self._pats.get(name)
        if res is None:
            try:
                res = read_pat(name, entry.get("pat_ref") or {}, self._vault)  # type: ignore[arg-type]
            except VaultError as e:
                raise AuthError(e.reason_code, str(e)) from None
            self._pats[name] = res
        return Credential("pat", res.source, res.secret)

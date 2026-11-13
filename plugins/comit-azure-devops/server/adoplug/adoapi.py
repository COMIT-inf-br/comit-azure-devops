"""Ponte entre o `http` (S2) e quem consome JSON: descoberta, leitura e escrita.

- `AdoApiError` leva `reason_code` e o `diagnostic()` sem segredo da resposta.
- `split_url` remonta uma URL do ADO em (org, caminho, query). Aceita só
  `https://dev.azure.com/<org>/...` e `https://<org>.visualstudio.com/...`; a forma antiga
  sai como rota de `dev.azure.com`, porque o `http` só fala com esse host (I1).
- `DiscoveryClient` cumpre o contrato `get_json(url)` do `discovery` sobre `AdoHttp`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlparse

from .http import AdoHttp, HttpError, Response

VS_HOST_RE = re.compile(r"^([a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?)\.visualstudio\.com$", re.I)
WRITE_AMBIGUOUS = frozenset({"TIMEOUT", "NETWORK"})


class AdoApiError(RuntimeError):
    """Resposta fora de 2xx ou pedido recusado pelo `http`."""

    def __init__(self, reason_code: str, message: str, diagnostic: Optional[Dict[str, Any]] = None,
                 http_status: Optional[int] = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.diagnostic = diagnostic or {}
        self.http_status = http_status

    @classmethod
    def from_response(cls, resp: Response, what: str = "pedido") -> "AdoApiError":
        d = resp.diagnostic()
        return cls(resp.reason_code or "HTTP_ERROR", f"O Azure DevOps recusou o {what} (HTTP {resp.status}).",
                   d, resp.status)

    @classmethod
    def from_http(cls, e: HttpError) -> "AdoApiError":
        return cls(e.reason_code, str(e), {"reason_code": e.reason_code, "message": str(e)})


def split_url(url: str) -> Tuple[str, str, Dict[str, str]]:
    """(org, caminho já codificado, query) de uma URL do ADO. Outro host levanta `AdoApiError`."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port or u.netloc.lower() != host:
        raise AdoApiError("HOST_NOT_ALLOWED", "URL fora do Azure DevOps.")
    parts = u.path.split("/")
    if host == "dev.azure.com":
        if len(parts) < 2 or not parts[1]:
            raise AdoApiError("INVALID_PATH", "A URL não tem a organização.")
        org, rest = unquote(parts[1]), parts[2:]
    else:
        m = VS_HOST_RE.match(host)
        if not m:
            raise AdoApiError("HOST_NOT_ALLOWED", "URL fora do Azure DevOps.")
        org, rest = m.group(1), parts[1:]
        if rest and rest[0].casefold() == "defaultcollection":
            rest = rest[1:]
    return org, "/" + "/".join(rest), dict(parse_qsl(u.query, keep_blank_values=True))


def canonical_url(url: str) -> str:
    """A mesma URL na forma `https://dev.azure.com/<org>/...` (sem query)."""
    org, path, _ = split_url(url)
    return f"https://dev.azure.com/{org}{path}"


def call(http: AdoHttp, method: str, org: str, path: str, *, query: Optional[Dict[str, Any]] = None,
         json_body: Any = None, data: Optional[bytes] = None, content_type: Optional[str] = None,
         what: str = "pedido", host: Optional[str] = None) -> Any:
    """Pedido que devolve o JSON da resposta 2xx ou levanta `AdoApiError`."""
    kw: Dict[str, Any] = {"query": query, "json_body": json_body, "data": data, "content_type": content_type}
    if host:
        kw["host"] = host
    try:
        r = http.request(method, org, path, **kw)
    except HttpError as e:
        raise AdoApiError.from_http(e) from None
    if not r.ok:
        raise AdoApiError.from_response(r, what)
    try:
        return r.json()
    except ValueError:
        raise AdoApiError("BAD_RESPONSE", "Resposta do Azure DevOps sem JSON válido.", {"http_status": r.status},
                          r.status) from None


class DiscoveryClient:
    """`get_json(url)` para o `discovery`, sobre `AdoHttp`. Nunca envia escrita."""

    def __init__(self, http: AdoHttp):
        self.http = http

    def get_json(self, url: str) -> Any:
        org, path, query = split_url(url)
        return call(self.http, "GET", org, path, query=query or None, what="GET")

    def post_json(self, url: str, body: Any) -> Any:  # noqa: ARG002
        raise AdoApiError("READ_ONLY", "A descoberta só faz GET.")

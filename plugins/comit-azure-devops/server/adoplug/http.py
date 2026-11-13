"""Cliente REST do Azure DevOps sobre urllib.

I1: a credencial só vai para https://dev.azure.com/<org> de uma org
configurada; outro host, porta, userinfo ou redirect é recusado.
I6: corpo em bytes UTF-8; JSON Patch sempre array.
Mensagens de erro passam por `redact` antes de sair (I2).
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import quote, unquote, urlencode, urlparse

from .vault import Secret, redact

HOST = "dev.azure.com"
VSSPS_HOST = "vssps.dev.azure.com"
USER_AGENT = "comit-azure-devops/0.1"
JSON_PATCH = "application/json-patch+json"
TIMEOUT = 30
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$")


class HttpError(RuntimeError):
    """Pedido recusado antes do envio ou falha de rede."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # o urllib levanta HTTPError com o 3xx


def build_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes = field(repr=False)
    url: str = ""
    reason_code: Optional[str] = None
    _secrets: Sequence[Secret] = field(default_factory=tuple, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.status != 203

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def diagnostic(self) -> Dict[str, Any]:
        """Resumo do erro sem segredo: http_status, typeKey, errorCode, message (até 1000)."""
        d: Dict[str, Any] = {"http_status": self.status}
        if self.reason_code:
            d["reason_code"] = self.reason_code
        msg = ""
        try:
            j = self.json()
            if isinstance(j, dict):
                d["typeKey"] = j.get("typeKey")
                d["errorCode"] = j.get("errorCode")
                msg = str(j.get("message") or "")
        except (ValueError, UnicodeDecodeError):
            msg = "" if self.status == 203 else self.text()[:300]
        d["message"] = redact(msg, self._secrets, limit=1000)
        return d


def _scrub_body(body: bytes, secrets: Sequence[Secret]) -> bytes:
    """Troca por *** qualquer segredo em memória que a resposta tenha ecoado."""
    import base64

    for s in secrets:
        v = s.reveal()
        for f in (v, base64.b64encode((":" + v).encode()).decode()):
            body = body.replace(f.encode("utf-8"), b"***")
    return body


def classify(status: int) -> Optional[str]:
    if 200 <= status < 300 and status != 203:
        return None
    if status in (401, 203):
        return "AUTH_REJECTED"
    if 300 <= status < 400:
        return "REDIRECT_REFUSED"
    return {403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 412: "PRECONDITION_FAILED",
            429: "RATE_LIMITED"}.get(status, "SERVER_ERROR" if status >= 500 else "BAD_REQUEST")


def _check_org(org: str) -> str:
    if not isinstance(org, str) or not ORG_RE.match(org):
        raise HttpError("INVALID_ORG", "Nome de organização inválido.")
    return org


def _check_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise HttpError("INVALID_PATH", "O caminho precisa começar com uma barra.")
    if any(c in path for c in "?#\\@\r\n\t ") or "://" in path:
        raise HttpError("INVALID_PATH", "Caminho com caractere proibido.")
    dec = unquote(unquote(path))
    if any(c in dec for c in "\\@\r\n?#") or any(seg in ("..", ".") for seg in dec.split("/")):
        raise HttpError("INVALID_PATH", "Caminho com segmento proibido.")
    return path


def segment(value: Any) -> str:
    """Codifica um segmento de caminho (nome de projeto com acento, tipo com espaço)."""
    return quote(str(value), safe="")


class AdoHttp:
    """Envia pedidos ao Azure DevOps com a credencial do `auth`.

    `auth` precisa ter `get_credential(org)`, `invalidate(org)` e `known_secrets()`.
    `allowed_orgs` devolve as orgs configuradas; é chamado a cada pedido.
    """

    def __init__(self, auth: Any, allowed_orgs: Union[Callable[[], Iterable[str]], Iterable[str]],
                 opener: Optional[urllib.request.OpenerDirector] = None, timeout: int = TIMEOUT,
                 scheme_host: Optional[str] = None):
        self.auth = auth
        self._allowed = allowed_orgs if callable(allowed_orgs) else (lambda v=tuple(allowed_orgs): v)
        self.opener = opener or build_opener()
        self.timeout = timeout
        # Só os testes trocam o destino, por argumento; nunca por variável de ambiente.
        self._scheme_host = scheme_host

    # --- URL e allowlist -------------------------------------------------------

    def is_allowed(self, org: str) -> bool:
        return any(o.casefold() == org.casefold() for o in self._allowed())

    def url(self, org: str, path: str, query: Optional[Mapping[str, Any]] = None, host: str = HOST) -> str:
        _check_org(org)
        _check_path(path)
        if host not in (HOST, VSSPS_HOST):
            raise HttpError("HOST_NOT_ALLOWED", "Host fora da lista permitida.")
        base = self._scheme_host or f"https://{host}"
        u = f"{base}/{segment(org)}{path}"
        if query:
            u += "?" + urlencode({k: v for k, v in query.items() if v is not None}, safe="$,:")
        return u

    def check_url(self, url: str, allow_vssps: bool = False) -> str:
        """Confere uma URL absoluta (ex.: devolvida pelo ADO) e devolve a org."""
        u = urlparse(url)
        hosts = {HOST, VSSPS_HOST} if allow_vssps else {HOST}
        if self._scheme_host:
            t = urlparse(self._scheme_host)
            ok_host = (u.scheme, u.netloc) == (t.scheme, t.netloc)
        else:
            ok_host = (u.scheme == "https" and (u.hostname or "") in hosts and u.netloc == u.hostname)
        if not ok_host or u.username or u.password:
            raise HttpError("HOST_NOT_ALLOWED", "Host fora da lista permitida.")
        parts = u.path.split("/")
        if len(parts) < 2 or not parts[1]:
            raise HttpError("INVALID_PATH", "A URL não tem a organização.")
        org = _check_org(unquote(parts[1]))
        _check_path("/" + "/".join(parts[2:]))
        return org

    # --- envio ------------------------------------------------------------------

    def request(self, method: str, org: str, path: str, *, query: Optional[Mapping[str, Any]] = None,
                json_body: Any = None, data: Optional[bytes] = None, content_type: Optional[str] = None,
                headers: Optional[Mapping[str, str]] = None, anonymous: bool = False,
                host: str = HOST) -> Response:
        if host == VSSPS_HOST and anonymous:
            raise HttpError("HOST_NOT_ALLOWED", "vssps só com pedido autenticado explícito.")
        return self._send(method, self.url(org, path, query, host), org, json_body, data, content_type,
                          headers, anonymous)

    def request_url(self, method: str, url: str, *, json_body: Any = None, data: Optional[bytes] = None,
                    content_type: Optional[str] = None, headers: Optional[Mapping[str, str]] = None,
                    anonymous: bool = False, allow_vssps: bool = False) -> Response:
        org = self.check_url(url, allow_vssps=allow_vssps)
        return self._send(method, url, org, json_body, data, content_type, headers, anonymous)

    def get(self, org: str, path: str, **kw) -> Response:
        return self.request("GET", org, path, **kw)

    def resource_tenant(self, org: str) -> Optional[str]:
        """Tenant da org pelo cabeçalho x-vss-resourcetenant de um GET anônimo."""
        r = self.request("GET", org, "/_apis/connectionData", anonymous=True)
        t = r.headers.get("x-vss-resourcetenant")
        if t and GUID_RE.match(t) and set(t.replace("-", "")) != {"0"}:  # GUID zero = org sem tenant
            return t.lower()
        return None

    def _body(self, json_body: Any, data: Optional[bytes], content_type: Optional[str]):
        if json_body is not None and data is not None:
            raise HttpError("INVALID_BODY", "Use json_body ou data, não os dois.")
        if json_body is not None:
            ct = content_type or "application/json"
            if ct.split(";")[0].strip() == JSON_PATCH and not isinstance(json_body, list):
                raise HttpError("INVALID_BODY", "JSON Patch precisa ser um array.")
            return json.dumps(json_body, ensure_ascii=False).encode("utf-8"), ct + "; charset=utf-8"
        if data is not None:
            if not isinstance(data, (bytes, bytearray)):
                raise HttpError("INVALID_BODY", "data precisa ser bytes.")
            return bytes(data), content_type or "application/octet-stream"
        return None, None

    def _send(self, method: str, url: str, org: str, json_body: Any, data: Optional[bytes],
              content_type: Optional[str], headers: Optional[Mapping[str, str]], anonymous: bool) -> Response:
        method = method.upper()
        if method not in ("GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"):
            raise HttpError("INVALID_METHOD", "Método HTTP não suportado.")
        if not anonymous and not self.is_allowed(org):
            raise HttpError("ORG_NOT_ALLOWED", f"A organização {org} não está configurada.")
        body, ct = self._body(json_body, data, content_type)
        attempts = 1 if anonymous else 2
        resp: Optional[Response] = None
        for i in range(attempts):
            h = {"Accept": "application/json", "User-Agent": USER_AGENT,
                 "X-TFS-FedAuthRedirect": "Suppress"}
            for k, v in (headers or {}).items():
                if k.lower() in ("authorization", "cookie", "host"):
                    raise HttpError("INVALID_HEADER", f"Cabeçalho {k} não pode vir do chamador.")
                h[k] = v
            if ct:
                h["Content-Type"] = ct
            if not anonymous:
                try:
                    h["Authorization"] = self.auth.get_credential(org).header().reveal()
                except Exception as e:  # AuthError e afins: sem segredo na mensagem
                    raise HttpError(getattr(e, "reason_code", "AUTH_FAILED"),
                                    redact(str(e), self._secrets())) from None
            resp = self._open(urllib.request.Request(url, data=body, headers=h, method=method))
            if resp.reason_code == "AUTH_REJECTED" and i + 1 < attempts:
                self.auth.invalidate(org)  # token revogado: pede outro uma vez
                continue
            break
        assert resp is not None
        return resp

    def _secrets(self) -> Sequence[Secret]:
        try:
            return tuple(self.auth.known_secrets()) if self.auth is not None else ()
        except Exception:
            return ()

    def _open(self, req: urllib.request.Request) -> Response:
        secrets = self._secrets()
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                status, hdrs, body = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs = e.code, e.headers
            try:
                body = e.read()
            except Exception:
                body = b""
            finally:
                e.close()
        except (socket.timeout, TimeoutError):
            raise HttpError("TIMEOUT", "O Azure DevOps não respondeu a tempo.") from None
        except urllib.error.URLError as e:
            raise HttpError("NETWORK", "Falha de rede: " + redact(str(e.reason), secrets, limit=300)) from None
        except (ssl.SSLError, OSError) as e:
            raise HttpError("NETWORK", "Falha de rede: " + redact(type(e).__name__, secrets)) from None
        headers = {k.lower(): redact(v, secrets, limit=8000, patterns=False) for k, v in (hdrs.items() if hdrs else [])}
        headers.pop("set-cookie", None)
        return Response(status, headers, _scrub_body(body or b"", secrets), req.full_url, classify(status), secrets)

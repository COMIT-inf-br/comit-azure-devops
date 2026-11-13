"""Prévia de escrita: hash canônico, validade e journal em disco.

Uma prévia guarda o plano executável (rota, corpo, alvo) e os campos legíveis que a
pessoa aprova. O `apply` só roda com `preview_id` + `hash` iguais, dentro da validade e no
estado certo (I7). O journal fica em `previews/` da config local, sem segredo, e é apagado
depois de 7 dias.

Estados:
- `prepared`: pronta para o `apply`.
- `applying`: o envio começou. Se o processo cair aqui, só a reconciliação decide.
- `applied`: efeito confirmado.
- `partial`: anexo enviado sem relação, ou lote com passos pendentes. O `apply` continua do ponto em que parou.
- `ambiguous`: o envio pode ou não ter gerado efeito. Só a reconciliação decide.
- `failed`: o ADO recusou. Prepare de novo.
- `not_applied`: a reconciliação não achou efeito. Prepare de novo.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from . import paths

TTL_SECONDS = 30 * 60
RETENTION_SECONDS = 7 * 24 * 3600
ID_RE = re.compile(r"^[0-9a-f]{32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
STATES = ("prepared", "applying", "applied", "partial", "ambiguous", "failed", "not_applied")
APPLY_STATES = ("prepared", "partial")
RECONCILE_STATES = ("applying", "ambiguous", "partial")
# Campos legíveis que o `apply` confere contra a prévia, por operação.
READABLE_KEYS = {
    "create": ("operation", "org", "type", "title"),
    "update": ("operation", "org", "target_id", "title"),
    "comment": ("operation", "org", "target_id", "title"),
    "attach": ("operation", "org", "target_id", "file_name"),
    # Operações em lote (vários pedidos, um por passo): ver `batch.py`.
    "move": ("operation", "org", "iteration", "count"),
    "create_sprint": ("operation", "org", "title"),
    "delete": ("operation", "org", "target_id", "title"),
    "rollover": ("operation", "org", "iteration", "count"),
    "comments_batch": ("operation", "org", "count"),
    "block": ("operation", "org", "target_id", "title"),
    "unblock": ("operation", "org", "target_id", "title"),
}


class PreviewError(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def compute_hash(operation: str, org: str, plan: Mapping[str, Any]) -> str:
    """SHA-256 do JSON canônico do que será enviado."""
    return hashlib.sha256(canonical_json({"operation": operation, "org": org, "plan": plan})).hexdigest()


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, int) or isinstance(b, int):
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return False
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


class PreviewStore:
    def __init__(self, directory: Optional[Path] = None, clock: Callable[[], float] = time.time,
                 ttl: float = TTL_SECONDS, retention: float = RETENTION_SECONDS):
        self.dir = Path(directory) if directory is not None else paths.previews_dir()
        self.clock = clock
        self.ttl = ttl
        self.retention = retention

    # --- arquivo ------------------------------------------------------------------

    def _path(self, preview_id: str) -> Path:
        if not isinstance(preview_id, str) or not ID_RE.match(preview_id):
            raise PreviewError("PREVIEW_NOT_FOUND", "preview_id inválido.")
        return self.dir / f"{preview_id}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        paths.write_private(self._path(rec["preview_id"]),
                            json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))

    def load(self, preview_id: str) -> Dict[str, Any]:
        p = self._path(preview_id)
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PreviewError("PREVIEW_NOT_FOUND", "Prévia não encontrada. Prepare de novo.") from None
        except (ValueError, OSError):
            raise PreviewError("PREVIEW_INVALID", "Prévia ilegível. Prepare de novo.") from None
        if compute_hash(rec.get("operation"), rec.get("org"), rec.get("plan")) != rec.get("hash"):
            raise PreviewError("PREVIEW_TAMPERED", "A prévia gravada não confere com o hash. Prepare de novo.")
        return rec

    # --- ciclo ----------------------------------------------------------------------

    def create(self, operation: str, org: str, plan: Mapping[str, Any], readable: Mapping[str, Any],
               display: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if operation not in READABLE_KEYS:
            raise PreviewError("PREVIEW_INVALID", f"Operação desconhecida: {operation}.")
        self.cleanup()
        now = self.clock()
        rd = dict(readable)
        rd["operation"], rd["org"] = operation, org
        missing = [k for k in READABLE_KEYS[operation] if rd.get(k) in (None, "")]
        if missing:
            raise PreviewError("PREVIEW_INVALID", "Faltam campos legíveis: " + ", ".join(missing))
        rec = {
            "preview_id": uuid.uuid4().hex,
            "hash": compute_hash(operation, org, plan),
            "operation": operation,
            "org": org,
            "created_at": now,
            "expires_at": now + self.ttl,
            "state": "prepared",
            "plan": json.loads(canonical_json(plan)),
            "readable": {k: rd[k] for k in READABLE_KEYS[operation]},
            "display": dict(display or {}),
            "journal": [{"at": now, "state": "prepared"}],
            "result": {},
        }
        self._write(rec)
        return rec

    def check_apply(self, preview_id: str, hash_: str, claims: Mapping[str, Any]) -> Dict[str, Any]:
        """Confere tudo antes de qualquer pedido HTTP. Levanta `PreviewError` se algo divergir."""
        rec = self.load(preview_id)
        if not isinstance(hash_, str) or hash_.lower() != rec["hash"]:
            raise PreviewError("HASH_MISMATCH", "O hash não confere com a prévia.")
        diff = [k for k in READABLE_KEYS[rec["operation"]] if not _same(claims.get(k), rec["readable"].get(k))]
        if diff:
            raise PreviewError("PREVIEW_MISMATCH", "Campos diferentes da prévia: " + ", ".join(diff))
        state = rec["state"]
        if state == "applied":
            raise PreviewError("ALREADY_APPLIED", "Esta prévia já foi aplicada.")
        if state in ("applying", "ambiguous"):
            raise PreviewError("NEEDS_RECONCILE", "O envio anterior teve resultado incerto. Rode a reconciliação.")
        if state not in APPLY_STATES:
            raise PreviewError("PREVIEW_CLOSED", f"Prévia no estado {state}. Prepare de novo.")
        if state == "prepared" and self.clock() > rec["expires_at"]:
            raise PreviewError("PREVIEW_EXPIRED", "A prévia venceu (30 min). Prepare de novo.")
        return rec

    def transition(self, rec: Dict[str, Any], state: str, note: Optional[str] = None,
                   **result: Any) -> Dict[str, Any]:
        """Grava o novo estado antes de devolver. O journal nunca recebe segredo."""
        if state not in STATES:
            raise PreviewError("PREVIEW_INVALID", f"Estado desconhecido: {state}.")
        now = self.clock()
        rec["state"] = state
        entry = {"at": now, "state": state}
        if note:
            entry["note"] = str(note)[:300]
        rec["journal"].append(entry)
        if state == "applying":
            rec.setdefault("result", {})["applying_at"] = now
        rec.setdefault("result", {}).update(result)
        self._write(rec)
        return rec

    def cleanup(self) -> int:
        """Apaga prévias com mais de 7 dias. Devolve quantas apagou."""
        if not self.dir.is_dir():
            return 0
        limit = self.clock() - self.retention
        n = 0
        for f in self.dir.glob("*.json"):
            if not ID_RE.match(f.stem):
                continue
            try:
                created = json.loads(f.read_text(encoding="utf-8")).get("created_at", 0)
            except (ValueError, OSError):
                created = 0
            if created < limit:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        return n

"""Apoio dos testes: HOME temporário, comandos falsos, sentinelas e varredura de vazamento.

Os fakes rodam como `[sys.executable, fake_x.py, <estado>]`, igual no
Windows, no macOS e no Linux. Nada aqui toca o cofre real nem a rede.
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

FAKES = Path(__file__).resolve().parent
SERVER = FAKES.parent.parent / "server"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))


def sentinel(tag="Tk"):
    """Valor falso que não casa com os padrões do V7 (maiúsculas e hífen)."""
    return f"SENTINELA-{tag}-{uuid.uuid4().hex.upper()}-XYZ"


def leak_forms(value):
    return [value, "Bearer " + value, base64.b64encode((":" + value).encode()).decode()]


class TempHome:
    """HOME temporário com os fakes e seus estados."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="adoplug-test-"))
        self.home = self.root / "home"
        self.home.mkdir()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def state(self, name, data):
        p = self.root / f"{name}.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def cmd(self, fake, state_path):
        return [sys.executable, str(FAKES / f"fake_{fake}.py"), str(state_path)]

    def az(self, **state):
        p = self.state("az", state)
        return self.cmd("az", p), p

    def np(self, **state):
        p = self.state("np", state)
        return self.cmd("np", p), p

    def security(self, items=None):
        p = self.root / "keychain.json"
        p.write_text(json.dumps(items or {}), encoding="utf-8")
        return self.cmd("security", p), p

    def secret_tool(self, items=None):
        p = self.root / "secret_tool.json"
        p.write_text(json.dumps(items or {}), encoding="utf-8")
        return self.cmd("secret_tool", p), p

    @staticmethod
    def log(state_path):
        p = Path(str(state_path) + ".log")
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def files_text(self, exclude=()):
        """Texto de todos os arquivos do HOME temporário (fora os estados dos fakes em `exclude`)."""
        out = []
        ex = {str(Path(e)) for e in exclude}
        for p in self.home.rglob("*"):
            if p.is_file() and str(p) not in ex:
                out.append(p.read_bytes().decode("utf-8", errors="replace"))
        return "\n".join(out)


def assert_no_leak(tc, haystack, values):
    for v in values:
        for f in leak_forms(v):
            tc.assertNotIn(f, haystack)

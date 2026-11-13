#!/usr/bin/env python3
"""V7: recusa identidade pessoal, termos internos, padrões de segredo e GUIDs fora da lista.

Escaneia só os arquivos versionados (`git ls-files`). Os padrões são montados
por partes para este arquivo não casar com eles. Saída 0 = limpo; 1 = achados.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = "scripts/check_identity.py"
ALLOWLIST = "scripts/guid_allowlist.txt"

AT = "@"
GLOBAL = [
    ("email-mdb", re.compile(AT + r"mdb\.com\.br", re.I)),
    ("email-mdias", re.compile(AT + r"mdiasbranco", re.I)),
    ("email-comit", re.compile(AT + r"comit\.inf\.br", re.I)),
    ("matricula", re.compile(r"\b(?:ter|mdb|zdr|jqk|fvx|sey)\d{5}\b", re.I)),
    ("termo-interno", re.compile("|".join(
        ["data" + "-transformation", "padroes" + "_ado", "Cerv" + "ello", "mdb" + "cit"]), re.I)),
    # PAT formato antigo: 52 caracteres base32 (a-z2-7); formato atual: 84 caracteres com AZDO.
    ("pat-84", re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{75}AZDO[A-Za-z0-9]{4}(?![A-Za-z0-9])")),
    ("pat-52", re.compile(r"(?<![A-Za-z0-9])[a-z2-7]{52}(?![A-Za-z0-9])")),
]
# "squad" só em skills e docs (os perfis citam nomes de projeto).
SQUAD = ("squad", re.compile("sq" + "uad", re.I))
SQUAD_PATHS = re.compile(r"(^|/)(skills|docs)/")
GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
GUID_FREE = re.compile(r"(^|/)profiles/")


def load_allow():
    out = set()
    for line in (ROOT / ALLOWLIST).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def main():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.splitlines()
    allow = load_allow()
    hits = []
    for rel in files:
        if rel == SELF:
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        checks = list(GLOBAL)
        if SQUAD_PATHS.search(rel):
            checks.append(SQUAD)
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in checks:
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {name}")
            if rel != ALLOWLIST and not GUID_FREE.search(rel):
                for g in GUID.findall(line):
                    if g.lower() not in allow:
                        hits.append(f"{rel}:{n}: guid-fora-da-lista")
    for h in hits:
        print(h)
    print(f"V7: {'FALHOU' if hits else 'ok'} ({len(hits)} achados, {len(files)} arquivos)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

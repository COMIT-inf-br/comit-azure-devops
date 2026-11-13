#!/usr/bin/env python3
"""V7: recusa identidade pessoal, termos internos, padrões de segredo e GUIDs fora da lista.

Escaneia só os arquivos versionados (`git ls-files`). Os padrões são montados
por partes para este arquivo não casar com eles. Saída 0 = limpo; 1 = achados.
Com `--history`, escaneia também as mensagens e os diffs de `git log --all`, com as
mesmas isenções por caminho. Os metadados de autor e committer não entram.
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
    ("matricula", re.compile(r"\b[a-z]{3}\d{5}\b", re.I)),
    ("termo-interno", re.compile("|".join(
        ["data" + "-transformation", "padroes" + "_ado", "Cerv" + "ello", "mdb" + "cit"]), re.I)),
    # PAT formato antigo: 52 caracteres base32 (a-z2-7); formato atual: 84 caracteres com AZDO.
    # Caminho de home com nome de usuário. Placeholders como `<nome>`, `$USER`, `%USERNAME%` e `~` passam.
    ("caminho-home", re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\{1,2}Users\\{1,2}|[A-Za-z]:/Users/)"
                                r"(?![<{$%~*.])[^/\\\s<>{}$%*\"')]+(?:/|\\)")),
    ("pat-84", re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]{84}(?![A-Za-z0-9]))[A-Za-z0-9]{75,76}AZDO[A-Za-z0-9]{4,5}(?![A-Za-z0-9])")),
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


def scan_line(rel, line, allow):
    """Nomes dos padrões que a linha viola, com as isenções por caminho."""
    names = []
    checks = list(GLOBAL)
    if SQUAD_PATHS.search(rel):
        checks.append(SQUAD)
    for name, rx in checks:
        if rx.search(line):
            names.append(name)
    if rel != ALLOWLIST and not GUID_FREE.search(rel):
        for g in GUID.findall(line):
            if g.lower() not in allow:
                names.append("guid-fora-da-lista")
    return names


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
                          encoding="utf-8", errors="replace").stdout


def scan_tree(allow):
    files = git("ls-files").splitlines()
    hits = []
    for rel in files:
        if rel == SELF:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits += [f"{rel}:{n}: {name}" for name in scan_line(rel, line, allow)]
    return hits, len(files)


MARK = "@@COMMIT@@"


def scan_history(allow):
    """Mensagens e linhas alteradas de todos os commits. Sem os metadados de autor."""
    hits = []
    msgs = git("log", "--all", f"--format={MARK}%H%n%B")
    sha = "?"
    for line in msgs.splitlines():
        if line.startswith(MARK):
            sha = line[len(MARK):][:10]
            continue
        hits += [f"{sha} (mensagem): {name}" for name in scan_line("", line, allow)]
    log = git("log", "--all", "-p", "-U0", "--no-color", "--no-renames", f"--format={MARK}%H")
    sha, rel, count = "?", "", 0
    for line in log.splitlines():
        if line.startswith(MARK):
            sha, rel = line[len(MARK):][:10], ""
            count += 1
        elif line.startswith("diff --git "):
            rel = line.rsplit(" b/", 1)[-1]
        elif rel and rel != SELF and line[:1] in "+-" and not line.startswith(("+++", "---")):
            hits += [f"{sha} {rel}: {name}" for name in scan_line(rel, line[1:], allow)]
    return sorted(set(hits)), count


def main():
    allow = load_allow()
    hits, nfiles = scan_tree(allow)
    summary = f"{len(hits)} achados, {nfiles} arquivos"
    if "--history" in sys.argv[1:]:
        hh, ncommits = scan_history(allow)
        hits += hh
        summary = f"{len(hits)} achados, {nfiles} arquivos, {ncommits} commits"
    for h in hits:
        print(h)
    print(f"V7: {'FALHOU' if hits else 'ok'} ({summary})")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""V2: valida os perfis de organização contra o esquema e as regras de conteúdo proibido.

Sem argumentos, valida `plugins/*/profiles/*.json` (menos os `*.schema.json`).
Saída 0 = todos válidos; 1 = algum erro; 2 = nenhum perfil encontrado.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "comit-azure-devops" / "server"))
from adoplug import profile_validate  # noqa: E402


def main(argv):
    files = [Path(a) for a in argv] or sorted(
        p for p in ROOT.glob("plugins/*/profiles/*.json") if not p.name.endswith(".schema.json"))
    if not files:
        print("V2: nenhum perfil encontrado")
        return 2
    schema = profile_validate.load_schema()
    failed = 0
    for f in files:
        errors = profile_validate.validate_file(f, schema)
        name = f.resolve().relative_to(ROOT) if f.resolve().is_relative_to(ROOT) else f
        for e in errors:
            print(f"{name}: {e}")
        failed += bool(errors)
    print(f"V2: {'FALHOU' if failed else 'ok'} ({failed} com erro, {len(files)} perfis)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

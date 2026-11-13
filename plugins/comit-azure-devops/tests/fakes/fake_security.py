"""`/usr/bin/security` falso (Keychain). Uso: python fake_security.py <cofre.json> <args...>

Guarda itens num JSON {"<servico>\\t<conta>": valor}. Aceita
find-generic-password -s S -a A -w, delete-generic-password e o modo -i
(comandos pelo stdin, com aspas e escapes como o security real).
O log registra só o argv, para o teste conferir que o valor não passou por ele.
"""
import json
import os
import shlex
import sys


def load(p):
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(p, d):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def run(store_path, args):
    d = load(store_path)
    cmd = args[0] if args else ""
    if cmd == "find-generic-password":
        key = f"{opt(args, '-s')}\t{opt(args, '-a')}"
        if key not in d:
            sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found.\n")
            return 44
        if "-w" in args:
            sys.stdout.write(d[key] + "\n")
        return 0
    if cmd == "add-generic-password":
        key = f"{opt(args, '-s')}\t{opt(args, '-a')}"
        if key in d and "-U" not in args:
            return 45
        d[key] = opt(args, "-w")
        save(store_path, d)
        return 0
    if cmd == "delete-generic-password":
        key = f"{opt(args, '-s')}\t{opt(args, '-a')}"
        if key not in d:
            return 44
        del d[key]
        save(store_path, d)
        return 0
    return 1


def main():
    store_path, args = sys.argv[1], sys.argv[2:]
    with open(store_path + ".log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args}) + "\n")
    if args == ["-i"]:
        rc = 0
        for line in sys.stdin.read().splitlines():
            if line.strip():
                rc = run(store_path, shlex.split(line, posix=True)) or rc
        return rc
    return run(store_path, args)


if __name__ == "__main__":
    sys.exit(main())

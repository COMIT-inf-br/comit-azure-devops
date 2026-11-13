"""`secret-tool` falso (libsecret). Uso: python fake_secret_tool.py <cofre.json> <args...>

lookup/store/clear com atributos service e account; store lê o valor do stdin.
O log registra só o argv.
"""
import json
import os
import sys


def attrs(args):
    it = iter(a for a in args if not a.startswith("--"))
    return dict(zip(it, it))


def main():
    store_path, args = sys.argv[1], sys.argv[2:]
    with open(store_path + ".log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args}) + "\n")
    d = {}
    if os.path.exists(store_path):
        with open(store_path, encoding="utf-8") as f:
            d = json.load(f)
    cmd, rest = args[0], args[1:]
    a = attrs(rest)
    key = f"{a.get('service')}\t{a.get('account')}"
    if cmd == "lookup":
        if key not in d:
            return 1
        sys.stdout.write(d[key])
        return 0
    if cmd == "store":
        d[key] = sys.stdin.read()
    elif cmd == "clear":
        d.pop(key, None)
    else:
        return 1
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())

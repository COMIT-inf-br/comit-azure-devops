"""NordPass CLI falso. Uso: python fake_np.py <estado.json> <args do np...>

mode: ok | locked | signin. items: [{id, title, password}].
O log registra o argv e se o stdin chegou fechado (EOF imediato).
"""
import json
import sys


def err(code, message):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": message}}) + "\n")
    return 1


def main():
    state_path, args = sys.argv[1], sys.argv[2:]
    with open(state_path, encoding="utf-8") as f:
        st = json.load(f)
    try:
        stdin_eof = sys.stdin is None or sys.stdin.read(1) == ""
    except (OSError, ValueError):
        stdin_eof = True
    with open(state_path + ".log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "stdin_eof": stdin_eof}) + "\n")
    if "--field" in args or "--copy" in args:
        return err("FORBIDDEN_IN_TEST", "--field/--copy usados")
    mode = st.get("mode", "ok")
    if mode == "locked":
        return err("VAULT_LOCKED", "Vault is locked. Run `np unlock`.")
    if mode == "signin":
        return err("NOT_AUTHENTICATED", "You are not signed in")
    items = st.get("items", [])
    if args[:2] == ["item", "list"]:
        search = args[args.index("--search") + 1] if "--search" in args else None
        out = [{"id": i["id"], "title": i["title"], "type": "password"} for i in items
               if search is None or search.lower() in i["title"].lower()]
        print(json.dumps(out))
        return 0
    if args[:2] == ["item", "get"]:
        for i in items:
            if i["id"] == args[2]:
                print(json.dumps({"id": i["id"], "title": i["title"], "type": "password",
                                  "password": i["password"]}))
                return 0
        return err("NOT_FOUND", "Item not found")
    return err("UNKNOWN", "comando não simulado")


if __name__ == "__main__":
    sys.exit(main())

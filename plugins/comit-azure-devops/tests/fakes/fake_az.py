"""Azure CLI falso. Uso: python fake_az.py <estado.json> <args do az...>

O estado diz o comportamento (mode) e o token. Cada chamada acrescenta ao
log (estado + ".log") o argv e se o ambiente tinha AZURE_DEVOPS_EXT_PAT.
"""
import json
import os
import sys
import time


def main():
    state_path, args = sys.argv[1], sys.argv[2:]
    with open(state_path, encoding="utf-8") as f:
        st = json.load(f)
    with open(state_path + ".log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "pat_in_env": "AZURE_DEVOPS_EXT_PAT" in os.environ}) + "\n")
    mode = st.get("mode", "ok")
    if args[:2] == ["account", "get-access-token"]:
        if mode == "not_logged_in":
            sys.stderr.write("ERROR: Please run 'az login' to setup account.\n")
            return 1
        if mode == "fail_echo":  # stderr com o token: o código precisa limpar
            sys.stderr.write("ERROR: boom " + st["token"] + "\n")
            return 2
        if mode == "slow":
            time.sleep(5)
        out = {"accessToken": st["token"], "tokenType": "Bearer",
               "tenant": st.get("tenant"), "subscription": None}
        if st.get("old_format"):
            out["expiresOn"] = st["expiresOn"]
        else:
            out["expires_on"] = st.get("expires_on", int(time.time()) + 3600)
            out["expiresOn"] = "2000-01-01 00:00:00.000000"
        print(json.dumps(out))
        return 0
    if args[:1] == ["version"]:
        print(json.dumps({"azure-cli": "9.9.9"}))
        return 0
    if args[:2] == ["account", "list"]:
        print(json.dumps([{"tenantId": st.get("tenant"), "name": "N/A(tenant level account)"}]))
        return 0
    if args[:1] == ["login"]:
        if "--use-device-code" in args:
            sys.stderr.write("To sign in, use a web browser to open the page https://microsoft.com/devicelogin "
                             "and enter the code FAKE12345 to authenticate.\n")
            sys.stderr.flush()
            time.sleep(0.3)
        return 0 if mode != "login_fail" else 1
    sys.stderr.write("fake az: comando não simulado\n")
    return 9


if __name__ == "__main__":
    sys.exit(main())

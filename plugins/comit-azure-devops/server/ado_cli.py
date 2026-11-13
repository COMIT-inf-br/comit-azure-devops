"""CLI de terminal do plugin: diagnóstico, setup, login, PAT de contingência e descoberta.

A pessoa roda estes comandos no próprio terminal. O PAT entra só por `getpass` (entrada oculta).
Nenhum comando imprime segredo, nem o tamanho dele. Saída em linhas `chave=valor`.

  doctor|status [--org X] [--offline]
  setup-org --org X|URL [--auth az|pat]
  login --org X [--device-code]
  pat-set --org X [--nordpass-title T [--nordpass-folder F]]
  pat-check --org X
  discover --org X --project P [--save]
"""
import argparse
import getpass
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adoplug import config as cfgmod  # noqa: E402
from adoplug import tools, vault  # noqa: E402
from adoplug.adoapi import AdoApiError  # noqa: E402
from adoplug.auth import AuthError, AuthManager  # noqa: E402
from adoplug.http import AdoHttp, HttpError  # noqa: E402
from adoplug.workitems import CONNECTION_API  # noqa: E402

LOGIN_TIMEOUT = 300


def _line(out: TextIO, **kv: Any) -> None:
    out.write(" ".join(f"{k}={v}" for k, v in kv.items()) + "\n")


def _fail(out: TextIO, code: str, message: str) -> int:
    _line(out, erro=code)
    out.write(message + "\n")
    return 1


def _name(ctx: tools.Context, org: str) -> str:
    n = cfgmod.normalize_org(org)
    return cfgmod.find_org(ctx.cfg, n) or n


def cmd_doctor(ctx: tools.Context, args: argparse.Namespace, out: TextIO, **_: Any) -> int:
    a: Dict[str, Any] = {"check_remote": not args.offline}
    if args.org:
        a["org"] = args.org
    env = tools.call_tool(ctx, "ado_status", a)
    d = env["data"]
    if "errors" in d:
        return _fail(out, env["reason_code"], "; ".join(d["errors"]))
    pend = {(p.get("org"), p["code"]): p for p in d.get("pending_setup", [])}
    az = d.get("az") or {}
    mark = "ok" if az.get("found") else "!!"
    out.write(f"[{mark}] az: " + (f"versão {az.get('version')}" if az.get("found") else "não encontrado") + "\n")
    for o in d.get("orgs", []):
        bad = [c for (org, c) in pend if org == o["org"]]
        out.write(f"[{'!!' if bad else 'ok'}] org {o['org']}: auth={o['auth']} login={o['login']} "
                  f"perfil={o['profile']}" + (f" remoto={'ok' if o['remote']['ok'] else 'falhou'}"
                                                 if "remote" in o else "") + "\n")
    for p in d.get("pending_setup", []):
        out.write(f"[!!] {p['code']}" + (f" ({p['org']})" if p.get("org") else "") + f": {p['message']}\n")
    _line(out, resultado="ok" if env["status"] == "ok" else "pendencias")
    return 0 if env["status"] == "ok" else 1


def cmd_setup_org(ctx: tools.Context, args: argparse.Namespace, out: TextIO, **_: Any) -> int:
    a: Dict[str, Any] = {"org": args.org, "confirm": True}
    if args.auth:
        a["auth"] = args.auth
    env = tools.call_tool(ctx, "ado_setup_org", a)
    if env["status"] != "ok":
        return _fail(out, env["reason_code"], env["next_action"] or "")
    d = env["data"]
    _line(out, org=d["org"], tenant="descoberto" if d["tenant"] else "desconhecido", perfil=d["profile"], gravado="sim")
    if d.get("tenant_warning"):
        out.write(d["tenant_warning"] + "\n")
    return 0


def cmd_login(ctx: tools.Context, args: argparse.Namespace, out: TextIO, sleep: Callable[[float], None] = time.sleep,
              **_: Any) -> int:
    a = {"org": args.org, "mode": "device_code" if args.device_code else "browser"}
    deadline = time.monotonic() + LOGIN_TIMEOUT
    shown = False
    while True:
        env = tools.call_tool(ctx, "ado_auth_login", a)
        code = env["reason_code"]
        if code == "LOGIN_OK":
            _line(out, login="ok")
            return 0
        if code != "LOGIN_RUNNING":
            return _fail(out, code, env["next_action"] or "")
        st = env["data"]["login"]
        if not shown:
            if st.get("code"):
                out.write(f"Abra {st.get('url')} e digite o código {st['code']}.\n")
                shown = True
            elif a["mode"] == "browser":
                out.write("Conclua o login na janela do navegador.\n")
                shown = True
        if time.monotonic() > deadline:
            _line(out, login="expirou")
            return 1
        sleep(1.0)


def _http_for(ctx: tools.Context, mgr: AuthManager, name: str) -> AdoHttp:
    return AdoHttp(mgr, [name], scheme_host=ctx.scheme_host)


def check_pat(ctx: tools.Context, org: str, out: TextIO) -> int:
    """Acha o PAT na ordem configurada e faz um GET de connectionData com ele."""
    name = _name(ctx, org)
    mgr = AuthManager(cfgmod.set_org(ctx.cfg, name, auth="pat"), vault_tools=ctx.vault)
    try:
        cred = mgr.get_credential(name)
    except AuthError as e:
        _line(out, org=name, encontrado="não", motivo=e.reason_code)
        out.write(str(e) + "\n")
        return 1
    _line(out, org=name, fonte=cred.source, encontrado="sim")
    try:
        r = _http_for(ctx, mgr, name).get(name, "/_apis/connectionData", query={"api-version": CONNECTION_API})
    except HttpError as e:
        _line(out, http="erro", motivo=e.reason_code)
        return 1
    _line(out, http=r.status)
    return 0 if r.status == 200 else 1


def cmd_pat_check(ctx: tools.Context, args: argparse.Namespace, out: TextIO, **_: Any) -> int:
    return check_pat(ctx, args.org, out)


def _use_pat(ctx: tools.Context, name: str, out: TextIO) -> None:
    ctx.save_config(cfgmod.set_org(ctx.cfg, name, auth="pat"))
    _line(out, org=name, auth="pat", gravado="sim")


def cmd_pat_set(ctx: tools.Context, args: argparse.Namespace, out: TextIO,
                getpass_fn: Callable[[str], str] = getpass.getpass, is_tty: Callable[[], bool] = lambda: bool(sys.stdin and sys.stdin.isatty()),
                **_: Any) -> int:
    name = _name(ctx, args.org)
    if args.nordpass_title:
        ref = {"nordpass_title": args.nordpass_title, "nordpass_folder": args.nordpass_folder}
        ctx.save_config(cfgmod.set_org(ctx.cfg, name, pat_ref=ref))
        _line(out, org=name, referencia="nordpass", gravado="sim")
    else:
        if args.nordpass_folder:
            return _fail(out, "INVALID_ARGUMENTS", "--nordpass-folder só vale com --nordpass-title.")
        if not is_tty():
            return _fail(out, "NO_TTY", "Rode este comando num terminal interativo. O PAT entra com entrada oculta.")
        value = (getpass_fn("PAT (a digitação fica oculta): ") or "").strip()
        if not value:
            return _fail(out, "EMPTY_PAT", "Nenhum valor informado.")
        ref = (ctx.cfg.get("orgs") or {}).get(name, {}).get("pat_ref") or {}
        try:
            store = vault.write_os(str(ref.get("os_service") or cfgmod.DEFAULT_OS_SERVICE),
                                   str(ref.get("os_account") or name), value, ctx.vault)
        except vault.VaultError as e:
            return _fail(out, e.reason_code, str(e))
        finally:
            value = ""
        _line(out, org=name, gravado="sim", cofre=store)
    rc = check_pat(ctx, name, out)
    if rc == 0:
        _use_pat(ctx, name, out)
    else:
        out.write("O PAT não passou na conferência. A organização continua no modo anterior.\n")
    return rc


def cmd_discover(ctx: tools.Context, args: argparse.Namespace, out: TextIO, **_: Any) -> int:
    try:
        name = ctx.org({"org": args.org})
        found = ctx.discover(name, args.project)
    except tools.ToolError as e:
        return _fail(out, e.env["reason_code"], e.env["next_action"] or "")
    if found["errors"]:
        return _fail(out, "PROFILE_INVALID", "; ".join(found["errors"][:10]))
    out.write(json.dumps(found["profile"], ensure_ascii=False, indent=2) + "\n")
    if args.save:
        p = cfgmod.save_learned_profile(name, found["profile"], ctx.learned_dir)
        _line(out, perfil="gravado", arquivo=p)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ado_cli.py", description="CLI do comit-azure-devops.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("doctor", "status"):
        p = sub.add_parser(name, help="Diagnóstico do setup.")
        p.add_argument("--org")
        p.add_argument("--offline", action="store_true", help="Não consulta o Azure DevOps.")
        p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("setup-org", help="Configura uma organização.")
    p.add_argument("--org", required=True)
    p.add_argument("--auth", choices=["az", "pat"])
    p.set_defaults(fn=cmd_setup_org)
    p = sub.add_parser("login", help="Login do Azure CLI.")
    p.add_argument("--org", required=True)
    p.add_argument("--device-code", action="store_true")
    p.set_defaults(fn=cmd_login)
    p = sub.add_parser("pat-set", help="Grava o PAT no cofre do sistema, ou registra a referência do NordPass.")
    p.add_argument("--org", required=True)
    p.add_argument("--store", choices=["os"], default="os")
    p.add_argument("--nordpass-title")
    p.add_argument("--nordpass-folder")
    p.set_defaults(fn=cmd_pat_set)
    p = sub.add_parser("pat-check", help="Confere o PAT contra o Azure DevOps.")
    p.add_argument("--org", required=True)
    p.set_defaults(fn=cmd_pat_check)
    p = sub.add_parser("discover", help="Descobre o perfil da organização.")
    p.add_argument("--org", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--save", action="store_true", help="Grava como perfil aprendido.")
    p.set_defaults(fn=cmd_discover)
    return ap


def main(argv: Optional[List[str]] = None, ctx: Optional[tools.Context] = None, out: Optional[TextIO] = None,
         **kw: Any) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    try:
        ctx = ctx or tools.Context()
        if ctx.config_error is not None:
            return _fail(out, "CONFIG_INVALID", str(ctx.config_error))
        return args.fn(ctx, args, out, **kw)
    except (cfgmod.ConfigError, AuthError, AdoApiError, HttpError, vault.VaultError) as e:
        return _fail(out, getattr(e, "reason_code", "ERROR"), str(e))
    except KeyboardInterrupt:
        return _fail(out, "INTERRUPTED", "Interrompido.")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    sys.exit(main())

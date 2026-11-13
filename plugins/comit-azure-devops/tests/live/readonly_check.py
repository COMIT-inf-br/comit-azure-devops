#!/usr/bin/env python3
"""Verificação de leitura real contra o Azure DevOps (opt-in; fora do CI e do `unittest discover`).

Uso: uv run --python 3.12 --no-project tests/live/readonly_check.py --org <org> --project <projeto>

Usa o código do servidor (AuthManager, AdoHttp, WorkItemsApi) com o login do `az` da pessoa.
Só envia GET e o POST de leitura do WIQL: uma guarda no envio recusa qualquer outro pedido.
A config fica só na memória. A saída traz status, contagens e formatos; nunca nome, título, id ou token.
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from adoplug import config  # noqa: E402
from adoplug.adoapi import AdoApiError  # noqa: E402
from adoplug.auth import AuthError, AuthManager  # noqa: E402
from adoplug.http import GUID_RE, AdoHttp, HttpError  # noqa: E402
from adoplug.workitems import WorkItemsApi  # noqa: E402

WIQL = "SELECT [System.Id] FROM WorkItems WHERE [System.AssignedTo] = @Me ORDER BY [System.ChangedDate] DESC"


class ReadOnlyHttp(AdoHttp):
    """Recusa tudo que não seja GET ou o POST de leitura em `/_apis/wit/wiql`."""

    def _send(self, method, url, *args, **kw):
        path = urlparse(url).path
        if not (method == "GET" or (method == "POST" and path.endswith("/_apis/wit/wiql"))):
            raise HttpError("READONLY_GUARD", f"Pedido {method} recusado pela guarda de leitura.")
        return super()._send(method, url, *args, **kw)


def run(org: str, project: str, out: list) -> None:
    cfg = config.set_org(config.default_config(), org, auth="az")
    auth = AuthManager(cfg)
    api = WorkItemsApi(ReadOnlyHttp(auth, config.configured_orgs(cfg)))
    try:
        projects = api.list_projects(org)
        out.append(f"list_projects: ok, projetos={len(projects)}")
        proj = api.project(org, project)
        out.append(f"project: ok, id_guid={bool(GUID_RE.match(proj['id']))}")
        out.append(f"list_teams: ok, equipes={len(api.list_teams(org, proj['id']))}")
        ids = api.wiql_ids(org, proj["id"], WIQL, top=3)
        out.append(f"wiql @Me top3 (POST de leitura): ok, itens={len(ids)}")
        if ids:
            s = api.get(org, ids[0], "relations")
            out.append(f"get relations: ok, rev_int={isinstance(s['rev'], int)}, relations={len(s['relations'])}, "
                       f"parent={'sim' if s['parent_id'] else 'não'}, state_category={s.get('state_category')}, "
                       f"web_url={bool(s.get('web_url'))}")
            out.append(f"comments: ok, lidos={len(api.comments(org, ids[0], s['project_id'], limit=5))}")
            out.append(f"children: ok, filhos={len(api.children(org, ids[0]))}")
        else:
            out.append("get/comments/children: pulados (WIQL sem itens)")
    except (AdoApiError, AuthError, HttpError) as e:
        out.append(f"erro: {e.reason_code} http={getattr(e, 'http_status', None)}")
        out.append("RESULTADO: FALHOU")
        return
    finally:
        tokens = [t.reveal() for t in auth.known_secrets()]
        if any(t and t in "\n".join(out) for t in tokens):
            out.append("ALERTA: token na saída")
    out.append("RESULTADO: OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org", required=True)
    ap.add_argument("--project", required=True, help="nome ou GUID do projeto")
    args = ap.parse_args()
    out: list = []
    run(args.org, args.project, out)
    print("\n".join(out))
    return 0 if out[-1] == "RESULTADO: OK" else 1


if __name__ == "__main__":
    sys.exit(main())

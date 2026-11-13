"""Gera evals/mocks/ado/*.md, _tools.json e as substituições de `ado_status` por caso a partir do servidor real no modo --mock.

Uso: uv run --python 3.12 --no-project evals/gen_mocks.py [--check]
Cada mock é a resposta real da ferramenta (envelope do `tools.render`), com caminho de máquina, `preview_id`,
`proposal_id` e `hash` trocados por valores sintéticos fixos. `--check` não grava: sai 1 se algum arquivo mudaria.
O `_tools.json` vem de `tools.tool_list()`, para o eval ver os esquemas reais.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(PLUGIN / "server"))

import ado_mcp  # noqa: E402
from adoplug import tools  # noqa: E402

OUT = HERE  # os caminhos abaixo são relativos a evals/
PREVIEW = "0000000000000000000000000000aaaa"
HASH = "00000000000000000000000000000000000000000000000000000000000000bb"
PROPOSAL = "0000000000000000000000000000cccc"


def fix_machine_fields(text: str) -> str:
    """Valores que mudam com o sistema (macOS, Linux, Windows), trocados pelo JSON já lido."""
    try:
        d = json.loads(text)
    except ValueError:
        return text
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, dict):
        if isinstance(data.get("runtime"), dict):
            data["runtime"]["system"] = "mac"
        if isinstance(data.get("az"), dict) and "path" in data["az"]:
            data["az"]["path"] = "/caminho/sintetico"
        if isinstance(data.get("file"), dict):
            data["file"].update(path="/caminho/sintetico", size=11)
        if "requested_path" in data:
            data["requested_path"] = "/caminho/sintetico"
    return json.dumps(d, ensure_ascii=False)


def clean(text: str, name: str) -> str:
    """Troca o que varia entre execuções: sistema, caminhos, versão do Python, validade e identificadores."""
    text = fix_machine_fields(text)
    text = re.sub(r"(?:/(?:Users|private|var|tmp|home)/|[A-Za-z]:\\\\)[^\"\s]*", "/caminho/sintetico", text)
    text = re.sub(r"\b[0-9a-f]{64}\b", HASH, text)
    text = re.sub(r"\b[0-9a-f]{32}\b", PROPOSAL if name == "ado_discover_org" else PREVIEW, text)
    text = re.sub(r'"expires_at": [0-9.]+', '"expires_at": 1794000000.0', text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|\+00:00)", "2026-11-13T00:00:00Z", text)
    return re.sub(r'"python": "[0-9.]+"', '"python": "3.12.0"', text)


def orphan(text: str) -> str:
    d = json.loads(text)
    item = d["data"]["item"]
    item["parent_id"] = None
    item["fields"].pop("System.Parent", None)
    item["relations"] = [r for r in item["relations"] if r.get("rel") != "System.LinkTypes.Hierarchy-Reverse"]
    return json.dumps(d, ensure_ascii=False)


def run(ctx, name: str, **args) -> str:
    text, _ = tools.render(ctx, tools.call_tool(ctx, name, args))
    return clean(text, name)


def build() -> dict:
    files = {}
    ctx = ado_mcp.build_mock_context("configured")
    story = 1001  # primeiro item criado pelo mock: a User Story
    proj = "Projeto Ágil"
    files["ado_status"] = run(ctx, "ado_status")
    files["ado_whoami"] = run(ctx, "ado_whoami")
    files["ado_list_projects"] = run(ctx, "ado_list_projects")
    files["ado_list_teams"] = run(ctx, "ado_list_teams", project=proj)
    files["ado_iterations"] = run(ctx, "ado_iterations", project=proj, team="Time Ágil")
    # `ado_get` responde pelo id pedido: o mock lê fixtures/ado_get_<id>.json (1001 = story, 1002 a 1004 = tasks).
    files["ado_get"] = "{{file:mocks/ado/fixtures/ado_get_{input.id}.json}}"
    fixtures = {f"ado_get_{i}.json": run(ctx, "ado_get", id=i, expand="relations") for i in range(1001, 1005)}
    fixtures["ado_get_1004.json"] = orphan(fixtures["ado_get_1004.json"])  # task sem pai, para o caso "vincular pai"
    files["ado_query"] = run(ctx, "ado_query", project=proj, assigned_to_me=True)
    files["ado_children"] = run(ctx, "ado_children", id=story)
    files["ado_comments"] = run(ctx, "ado_comments", id=story)
    files["ado_resolve_identity"] = run(ctx, "ado_resolve_identity", project=proj, text="Ana Souza")
    files["ado_type_schema"] = run(ctx, "ado_type_schema", project=proj, type="Task")
    task = {"System.Title": "Escrever testes", "Custom.Equipe": "DADOS",
            "Microsoft.VSTS.Scheduling.OriginalEstimate": "2"}
    files["ado_prepare_create"] = run(ctx, "ado_prepare_create", project=proj, type="Task", fields=task,
                                      parent_id=story)
    files["ado_prepare_update"] = run(ctx, "ado_prepare_update", id=1002, state_intent="iniciar")
    files["ado_prepare_comment"] = run(ctx, "ado_prepare_comment", id=1002, text="Teste passou.",
                                       mentions=["Ana Souza"])
    ev = Path(tempfile.mkdtemp(prefix="ado-gen-")) / "relatorio.txt"
    ev.write_bytes("evidência\n".encode("utf-8"))  # bytes: sem troca de \n por \r\n no Windows
    files["ado_prepare_attach"] = run(ctx, "ado_prepare_attach", id=1002, file_path=str(ev), comment="Evidência")
    files["ado_set_defaults"] = run(ctx, "ado_set_defaults", project=proj, defaults={"team": "Time Ágil"})
    files["ado_reconcile"] = run(ctx, "ado_reconcile", preview_id="inexistente")
    # `ado_apply` nunca deve rodar nos evals. O mock existe só para o trace mostrar a violação.
    files["ado_apply"] = json.dumps({"status": "ok", "reason_code": "APPLIED", "data": {"id": 1999},
                                     "next_action": "Mock de eval: ado_apply não deveria ter sido chamado."},
                                    ensure_ascii=False)
    # org nova: contexto sem config
    new = ado_mcp.build_mock_context("unconfigured")
    files["ado_setup_org"] = run(new, "ado_setup_org", org="OrgA")
    new_cfg = ado_mcp.build_mock_context("unconfigured")
    tools.call_tool(new_cfg, "ado_setup_org", {"org": "OrgA", "confirm": True})
    files["ado_discover_org"] = run(new_cfg, "ado_discover_org", org="OrgA", project=proj)
    files["ado_profile_save"] = json.dumps(
        {"status": "needs_decision", "reason_code": "CONFIRM_REQUIRED",
         "data": {"org": "OrgA"}, "next_action": "Peça o sim da pessoa e chame de novo com confirm=true."},
        ensure_ascii=False)
    files["ado_auth_login"] = json.dumps(
        {"status": "ok", "reason_code": "LOGIN_RUNNING", "data": {"login": {"state": "running"}, "started": True},
         "next_action": "Peça à pessoa que conclua o login no navegador. Depois chame ado_status."},
        ensure_ascii=False)
    out = {f"mocks/ado/{k}.md": v for k, v in files.items()}
    out.update({f"mocks/ado/fixtures/{k}": v for k, v in fixtures.items()})
    # Substituições por caso: o `ado_status` de uma org sem config e o de uma org sem perfil.
    out["org-nova-sem-config/mocks/ado/ado_status.md"] = run(ado_mcp.build_mock_context("unconfigured"), "ado_status")
    ctx = ado_mcp.build_mock_context("unconfigured")
    tools.call_tool(ctx, "ado_setup_org", {"org": "OrgA", "confirm": True})
    out["perfil-ausente-descoberta/mocks/ado/ado_status.md"] = run(ctx, "ado_status")
    return out


def main() -> int:
    check = "--check" in sys.argv
    wanted = {rel: v + "\n" for rel, v in build().items()}
    wanted["mocks/ado/_tools.json"] = json.dumps({"tools": tools.tool_list()}, ensure_ascii=False, indent=1) + "\n"
    missing = {t["name"] for t in tools.tool_list()} - {Path(k).stem for k in wanted if k.startswith("mocks/ado/")}
    if missing:
        print("sem mock:", ", ".join(sorted(missing)))
        return 1
    diff = [n for n, v in wanted.items() if not (OUT / n).exists() or (OUT / n).read_text(encoding="utf-8") != v]
    if check:
        print("mocks desatualizados:", ", ".join(diff) if diff else "nenhum")
        return 1 if diff else 0
    for n in diff:
        (OUT / n).parent.mkdir(parents=True, exist_ok=True)
        (OUT / n).write_text(wanted[n], encoding="utf-8")
    print(f"{len(diff)} arquivos gravados em {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

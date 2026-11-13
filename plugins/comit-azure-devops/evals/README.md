# Evals de disparo por intenção

A suíte confere se a skill certa carrega para cada pedido e se nenhuma escrita sai sem OK. Ela roda com `claude plugin eval`.

## Casos

| Pasta | Pedido | Skill esperada |
|---|---|---|
| `criar-task-com-parent` | criar task numa US | `ado-criar` |
| `listar-atribuidos` | o que está atribuído a mim | `ado-consultar` |
| `iniciar-task` | iniciar uma task | `ado-atualizar`, `state_intent=iniciar` |
| `concluir-task` | concluir uma task | `ado-atualizar`, `state_intent=concluir` |
| `vincular-pai` | pôr uma task sob uma US | `ado-atualizar`, `add_parent` |
| `comentar-com-mencao` | comentar e mencionar | `ado-comentar` |
| `anexar-evidencia` | anexar arquivo | `ado-anexar` |
| `org-nova-sem-config` | organização sem config | `ado-configurar`, sem gravar |
| `perfil-ausente-descoberta` | `PROFILE_ABSENT` | `ado-configurar`, `ado_discover_org`, sem gravar |
| `skill-de-projeto-presente` | projeto com skill própria | tenta a skill do projeto antes |
| `fora-de-ado` | pedido sem relação com ADO | nenhuma skill |
| `../evals-sem-mcp/cowork-sem-mcp` | sem servidor MCP (Cowork em nuvem) | mensagem fixa e rascunho |

Todo caso tem o grader `sem-apply`: `ado_apply` não pode aparecer no trace.

## Como rodar

Rode na pasta do plugin. Use sempre estas opções:

```text
claude plugin eval . --no-publish --runs 1 --ablation none --output-dir <pasta-temporária> --json <arquivo>.json
claude plugin eval . --eval-dir evals-sem-mcp --no-publish --runs 1 --ablation none --output-dir <pasta-temporária>
```

- `--no-publish` impede o envio do relatório (com prompts e transcripts) ao claude.ai.
- `--ablation none` e `--runs 1` evitam rodar cada caso seis vezes.
- Não use `--mocks off` nem `--allow-real-servers`. Eles sobem o servidor real com a sua configuração e o seu `az`.
- O código de saída vale 1 se qualquer caso ficar abaixo de 1.0. Leia o placar por caso no JSON.
- A primeira execução pede confirmação de confiança no plugin. Em CI, use `--trust-plugin`.

## Como os mocks funcionam

O `claude plugin eval` não passa argumentos ao servidor MCP do plugin, então o `--mock` do servidor não serve aqui. Os evals usam o mecanismo próprio de mocks: um arquivo por ferramenta em `mocks/ado/`.

- Cada mock é a resposta real da ferramenta, gerada pelo servidor em `--mock` e com valores variáveis trocados por constantes.
- `mocks/ado/_tools.json` traz os esquemas reais de `tools/list`.
- `ado_get` lê `mocks/ado/fixtures/ado_get_<id>.json` (1001 é a story; 1002 a 1004 são tasks).
- `org-nova-sem-config/mocks/` e `perfil-ausente-descoberta/mocks/` trocam só o `ado_status`.
- `evals-sem-mcp/` não tem mocks: o servidor não sobe e as ferramentas `ado_*` não existem. Ele fica fora de `evals/` porque os mocks de `evals/` valem para todos os casos de lá.

Para regenerar depois de mudar o servidor ou o ADO falso:

```text
uv run --python 3.12 --no-project evals/gen_mocks.py
```

O teste `tests/unit/test_fake_ado_mocks_evals.py` falha se os mocks ficarem desatualizados.

## Limites

- O eval não simula turnos da pessoa. Por isso nenhum caso cobre o `ado_apply` depois do OK: os casos só provam que ele não roda sem OK.
- O eval carrega só o plugin sob teste. A skill do projeto de `skill-de-projeto-presente` vem de `append_system_prompt` e não existe de fato. O caso confere que o modelo tenta a skill do projeto primeiro.
- Os mocks são estáticos. `ado_setup_org` e `ado_profile_save` sempre pedem confirmação, e os graders recusam `confirm=true`.

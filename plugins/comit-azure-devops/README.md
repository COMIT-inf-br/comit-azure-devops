# comit-azure-devops (plugin)

Plugin do Claude Code e do Claude Cowork para operar work items do Azure DevOps em linguagem natural. Você pede em português ou inglês. O plugin escolhe a operação, mostra a prévia e só escreve depois do seu OK.

## Instalar

```text
/plugin marketplace add COMIT-inf-br/comit-azure-devops
/plugin install comit-azure-devops@comit-azure-devops
```

## O que ele faz

| Skill | Faz |
|---|---|
| `azure-devops` | Roteia o pedido para a skill certa |
| `ado-configurar` | Conduz a escolha do login (Azure CLI ou PAT), organização nova e perfil |
| `ado-consultar` | Lê, lista e filtra work items; resume os comentários de um item |
| `ado-sprint` | Backlog do time, montagem de sprint, carga por pessoa, nova sprint, virada de sprint e exclusão |
| `ado-daily` | Transforma a transcrição ou a ata de uma reunião em um comentário por item |
| `ado-criar` | Cria task, user story, bug, feature e épico |
| `ado-atualizar` | Muda estado, campos, pai e links |
| `ado-impedimento` | Registra e encerra o impedimento de um item, pelo padrão do perfil da organização |
| `ado-comentar` | Comenta e menciona pessoas, também em vários itens de uma vez |
| `ado-anexar` | Anexa arquivos e evidências |

As skills disparam pela intenção do pedido. Num projeto que tem skills próprias de Azure DevOps, as skills do plugin cedem a vez.

## Requisitos

- `uv`.
- Azure CLI (`az`).

São as únicas instalações. Veja [`docs/setup.md`](docs/setup.md).

## Autenticação

Na primeira configuração, você escolhe:

1. Login pelo Azure CLI, sem PAT (recomendado). O acesso se renova sozinho enquanto você usa.
2. PAT, com validade fixa. Para quem precisa de um acesso que não peça login, ou para organização sem Microsoft Entra.

A comparação, com as fontes da Microsoft, está em [`docs/setup.md`](docs/setup.md#escolha-do-login).

No PAT, você grava o token no seu terminal, com entrada oculta. O servidor lê o PAT do NordPass ou do cofre do sistema. Nunca de variável de ambiente, de arquivo ou do chat. Veja [`docs/pat.md`](docs/pat.md).

## Perfil de organização

O perfil diz ao plugin quais campos cada tipo exige. O plugin traz perfis embutidos de exemplo (`MDB-CIT`, `MDB-DATAPLATFORM`). Numa organização nova, ele descobre o perfil por leitura e pergunta antes de gravar. Veja [`docs/perfil.md`](docs/perfil.md).

## Claude Code e Cowork

| Superfície | Resultado |
|---|---|
| Claude Code (macOS, Windows, Linux) | Execução completa |
| Cowork, sessão local | Execução completa, com melhor esforço |
| Cowork, sessão em nuvem | Só rascunho do payload, com aviso. O servidor local não roda na nuvem |

## Segurança

- O token fica só na memória do servidor.
- O servidor só envia credencial a `https://dev.azure.com/<org>` de uma organização configurada.
- Toda escrita passa por prévia completa, OK explícito e `ado_apply`.
- A criação usa `validateOnly` antes do envio. O plugin nunca usa `bypassRules`.
- O servidor recusa anexos de pastas de credencial.

Detalhes e como relatar uma falha: [`../../SECURITY.md`](../../SECURITY.md).

## Testar localmente antes de instalar

Use um clone deste repositório. Troque `<caminho>` pelo caminho do clone.

### Carregar o plugin direto da pasta

```text
claude --plugin-dir <caminho>/plugins/comit-azure-devops
```

O plugin vale só para essa sessão. Nada fica instalado.

### Instalar pelo marketplace local

Dentro do Claude Code:

```text
/plugin marketplace add <caminho-do-repo>
/plugin install comit-azure-devops@comit-azure-devops
```

`<caminho-do-repo>` é a raiz do clone, a pasta que tem `.claude-plugin/marketplace.json`. Para remover depois: `/plugin uninstall comit-azure-devops@comit-azure-devops` e `/plugin marketplace remove comit-azure-devops`.

### Onde fica a configuração local

| Sistema | Pasta |
|---|---|
| macOS | `~/Library/Application Support/comit-azure-devops/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/comit-azure-devops/` (o `XDG_CONFIG_HOME` só vale se for caminho absoluto) |
| Windows | `%APPDATA%\comit-azure-devops\` |

A pasta tem `config.json` (organizações e padrões), `profiles/` (perfis aprendidos) e `previews/`.

### Simular o primeiro uso

1. Feche o Claude.
2. Renomeie a pasta de configuração, por exemplo para `comit-azure-devops.bak`.
3. Abra o Claude e peça "configura o Azure DevOps".
4. Para voltar, apague a pasta nova e renomeie a `.bak` de volta.

Renomear a pasta simula "sem organização" e "sem perfil aprendido". Não apaga o login do Azure CLI (`~/.azure`) nem um PAT que esteja no cofre do sistema ou no NordPass. Para simular também o login, rode `az logout`.

## Evals

A suíte de disparo por intenção fica em `evals/`. Veja [`evals/README.md`](evals/README.md).

## Permissões recomendadas

As ferramentas de leitura podem ir para `permissions.allow` do seu `settings.json`. O prefixo é `mcp__plugin_comit-azure-devops_ado__`:

- `ado_status`
- `ado_whoami`
- `ado_list_projects`
- `ado_list_teams`
- `ado_iterations`
- `ado_get`
- `ado_query`
- `ado_children`
- `ado_comments`
- `ado_resolve_identity`
- `ado_type_schema`
- `ado_discover_org`
- `ado_backlog`
- `ado_sprint_items`

Não ponha `ado_apply` em `allow`. Não use "permitir sempre" nem o modo `bypassPermissions` para essa ferramenta.

O prompt de permissão de `ado_apply` é a trava dura contra escrita indevida. O `preview_id` e o `hash` da prévia não provam o seu OK: o modelo recebe os dois no `ado_prepare_*` e os repassa no `ado_apply`. Sem o prompt de permissão, só o OK pedido no chat segura a escrita.

As ferramentas `ado_prepare_*` não gravam nada no Azure DevOps. `ado_prepare_create`, `ado_prepare_update`, `ado_prepare_move_to_sprint`, `ado_prepare_sprint_rollover`, `ado_prepare_block` e `ado_prepare_unblock` enviam a validação ao servidor com `validateOnly`, que não grava. Elas podem ficar no prompt padrão.

## Operações em lote

Mover para a sprint, criar sprint, virar a sprint, excluir com filhos, o lote de comentários e o impedimento têm uma prévia só, com um hash. O `ado_apply` envia um pedido por item, na ordem da prévia.

- Se parte do lote ficar pendente (sem login, limite de taxa, rede), chame `ado_apply` de novo. Ele envia só os passos pendentes e não repete o que já foi gravado.
- Item que mudou depois da prévia é recusado pelo `test /rev` e pede uma prévia nova.
- Sem permissão (HTTP 403), o passo volta `FORBIDDEN` com a permissão que falta.
- Criar iteração e excluir não têm `validateOnly` no Azure DevOps. A prévia e o OK são a única trava.
- A exclusão vai para a lixeira do projeto e pode ser restaurada lá. O plugin nunca exclui em definitivo.

## Impedimento

Se o perfil da organização define um padrão de impedimento (`blocking`), o plugin registra o impedimento com `ado_prepare_block`:

- Uma prévia só: os campos do impedimento e o comentário obrigatório.
- O responsável, a data e a previsão vêm da política do perfil. A prévia mostra os 4 valores para você confirmar ou trocar.
- O estado do item não muda.
- `ado_prepare_unblock` desliga a flag e grava o comentário de resolução.

Sem padrão no perfil, o plugin avisa e oferece mapear o padrão pela descoberta. Veja [`docs/perfil.md`](docs/perfil.md#padrão-de-impedimento).

## Planejado

- Indicar pessoas a procurar sobre um item, com base nos comentários, nas entregas e nas datas prometidas.

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
| `ado-configurar` | Conduz login, organização nova, perfil e PAT de contingência |
| `ado-consultar` | Lê, lista e filtra work items |
| `ado-criar` | Cria task, user story, bug, feature e épico |
| `ado-atualizar` | Muda estado, campos, pai e links |
| `ado-comentar` | Comenta e menciona pessoas |
| `ado-anexar` | Anexa arquivos e evidências |

As skills disparam pela intenção do pedido. Num projeto que tem skills próprias de Azure DevOps, as skills do plugin cedem a vez.

## Requisitos

- `uv`.
- Azure CLI (`az`).

São as únicas instalações. Veja [`docs/setup.md`](docs/setup.md).

## Autenticação

O padrão é o `az login` da própria pessoa. Não precisa de PAT.

O PAT é contingência por organização, para organização que não aceita o login do Azure CLI. Você grava o PAT no seu terminal, com entrada oculta. O servidor lê o PAT do NordPass ou do cofre do sistema. Nunca de variável de ambiente, de arquivo ou do chat. Veja [`docs/pat.md`](docs/pat.md).

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

Não ponha `ado_apply` em `allow`. O prompt de permissão do Claude Code é a segunda trava depois do seu OK no chat. As ferramentas `ado_prepare_*` não enviam nada e podem ficar no prompt padrão.

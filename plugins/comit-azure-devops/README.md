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

Não ponha `ado_apply` em `allow`. O prompt de permissão do Claude Code é a segunda trava depois do seu OK no chat. As ferramentas `ado_prepare_*` não enviam nada e podem ficar no prompt padrão.

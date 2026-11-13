---
name: ado-configurar
description: Use quando o Azure DevOps ainda não estiver configurado, o login do Azure CLI vencer, ou a pessoa quiser conectar uma organização nova, mapear os campos obrigatórios de um projeto ou usar um PAT de contingência. Use when Azure DevOps is not set up, az login expired, the user wants to connect a new organization, map a project's required fields or set a contingency PAT. Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: configuração

Conduza um passo por vez. Pule o passo cuja condição já esteja resolvida. Peça a decisão da pessoa antes de gravar qualquer coisa.

| Passo | Condição | Ação | Diga à pessoa |
|---|---|---|---|
| 1 | O servidor MCP não sobe | Mostre a instalação do `uv`. | "Instale o uv. macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh`. Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"`. Depois reinicie o Claude." |
| 2 | `az` ausente | Mostre a instalação do Azure CLI. | "Instale o Azure CLI. Sem admin: `uv tool install azure-cli`. Com admin: macOS `brew install azure-cli`; Windows `winget install Microsoft.AzureCLI`." |
| 3 | Org ausente | Pergunte. Depois do sim, chame `ado_setup_org` com `confirm=true`. | "Qual organização do Azure DevOps? Pode colar o link de um work item." |
| 4 | Sem login no tenant | Chame `ado_auth_login` com `mode=browser`. | "Vou abrir o login da Microsoft no seu navegador. Entre com a conta da organização." |
| 5 | Login ok | Chame `ado_whoami`. | "Você entrou como <displayName>. Uso <uniqueName> como responsável padrão das suas tasks?" Grave só com sim. |
| 6 | Perfil embutido existe | Chame `ado_type_schema` do primeiro projeto. | "Esta organização já tem perfil no plugin. Campos obrigatórios da Task neste projeto: ..." |
| 7 | Perfil ausente | Chame `ado_discover_org` no primeiro projeto usado. | Mostre a tabela tipo, obrigatórios, listas de valores e estados. Pergunte: "Gravo este mapeamento na sua configuração local?" Depois do sim, chame `ado_profile_save`. |
| 8 | Falta valor padrão pessoal | Pergunte só quando a operação precisar. Depois chame `ado_set_defaults`. | "Qual valor padrão de <campo> para os seus itens? Opções: ..." |
| 9 | Token Entra recusado (401 com login válido) | Siga o PAT de contingência. | "Esta organização não aceita o login do Azure CLI. Precisamos de um PAT." |

Se o login por navegador falhar, chame `ado_auth_login` com `mode=device_code`. Diga: "Abra <url> e digite o código <código>."

## PAT de contingência

Use só no passo 9. O PAT é a exceção, não o caminho padrão.

1. Peça à pessoa que siga `${CLAUDE_PLUGIN_ROOT}/docs/pat.md`.
2. Escopos mínimos: Work Items (Read & write) e Project and Team (Read). Não aceite Full access.
3. A pessoa grava o PAT no próprio terminal, com entrada oculta: `ado_cli.py pat-set`. Mostre o comando com o caminho expandido.
4. Confira com `ado_cli.py pat-check --org <org>`. O resultado esperado é `encontrado=sim` e `http=200`.

Não peça o PAT no chat. Não use variável de ambiente nem arquivo. Se a pessoa colar o PAT no chat, avise que o token vazou para o histórico da conversa. Oriente a revogar o token e criar outro.

Mais detalhes: `${CLAUDE_PLUGIN_ROOT}/docs/setup.md` e `${CLAUDE_PLUGIN_ROOT}/docs/perfil.md`.

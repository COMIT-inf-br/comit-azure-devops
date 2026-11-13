---
name: ado-configurar
description: Use quando o Azure DevOps ainda não estiver configurado, o login do Azure CLI vencer, ou a pessoa quiser conectar uma organização nova, mapear os campos obrigatórios de um projeto ou escolher entre o login pelo Azure CLI e o PAT. Use when Azure DevOps is not set up, az login expired, the user wants to connect a new organization, map a project's required fields or choose between Azure CLI login and a PAT. Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: configuração

Se as ferramentas `ado_*` não existirem por falta de servidor (por exemplo, Cowork em nuvem), mostre a mensagem fixa da skill `azure-devops` (passo 7) e ofereça o rascunho do payload. Instalar `uv` não resolve esse caso.

Conduza um passo por vez. Pule o passo cuja condição já esteja resolvida. Peça a decisão da pessoa antes de gravar qualquer coisa.

| Passo | Condição | Ação | Diga à pessoa |
|---|---|---|---|
| 1 | O servidor MCP não sobe na máquina local | Mostre a instalação do `uv`. | "Instale o uv. macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh`. Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"`. Depois reinicie o Claude." |
| 2 | `az` ausente | Mostre a instalação do Azure CLI. | "Instale o Azure CLI. Sem admin: `uv tool install azure-cli`. Com admin: macOS `brew install azure-cli`; Windows `winget install Microsoft.AzureCLI`." |
| 3 | Org ausente | Pergunte. Depois do sim, chame `ado_setup_org` com `confirm=true`. | "Qual organização do Azure DevOps? Pode colar o link de um work item." |
| 4 | Org nova, sem modo de login escolhido | Mostre as duas opções de "Escolha do login" e espere a escolha. Para o PAT, siga "PAT". | "Como você quer entrar: (1) login pelo Azure CLI, sem PAT (recomendado), ou (2) PAT?" |
| 5 | Login pelo Azure CLI escolhido e sem login no tenant | Chame `ado_auth_login` com `mode=browser`. Depois acompanhe: chame `ado_auth_login` de novo ou `ado_status` até `LOGIN_OK` (ou `login=ok` na org). | "Vou abrir o login da Microsoft no seu navegador. Entre com a conta da organização." |
| 6 | Login ok | Chame `ado_whoami`. | "Você entrou como <displayName>. Uso <uniqueName> como responsável padrão das suas tasks?" Grave só com sim. |
| 7 | Perfil embutido existe | Chame `ado_type_schema` do primeiro projeto. | "Esta organização já tem perfil no plugin. Campos obrigatórios da Task neste projeto: ..." |
| 8 | `pending_setup` traz `PROFILE_ABSENT` | Chame `ado_discover_org` no primeiro projeto usado. Mostre o `summary` e, se houver `diff`, a diferença contra o perfil atual. Se a descoberta inferiu um padrão de impedimento (`blocking`), mostre os campos dele. | Mostre a tabela tipo, obrigatórios, listas de valores, estados e impedimento. Pergunte: "Gravo este mapeamento na sua configuração local?" Depois do sim, chame `ado_profile_save` com `proposal_id` e `confirm=true`. |
| 9 | Falta valor padrão pessoal | Pergunte só quando a operação precisar. Depois chame `ado_set_defaults`. | "Qual valor padrão de <campo> para os seus itens? Opções: ..." |
| 10 | Token Entra recusado (401 com login válido) | Ofereça o PAT. | "Esta organização não aceita o login do Azure CLI. Precisamos de um PAT." |

Se o login por navegador falhar (`AZ_LOGIN_FAILED`), chame `ado_auth_login` com `mode=device_code`. O código aparece em `data.login` e em `next_action`. Se ele ainda não apareceu, chame `ado_auth_login` de novo em alguns segundos. Diga: "Abra <url> e digite o código <código>."

Troque o modo do navegador para o código de dispositivo só depois de uma falha. Enquanto um login corre, uma nova chamada devolve o mesmo login em andamento.

Se `ado_profile_save` devolver `PROPOSAL_NOT_FOUND`, o servidor reiniciou e perdeu a proposta. Rode `ado_discover_org` de novo.

## Escolha do login

Mostre as duas opções e deixe a pessoa escolher. Não escolha por ela.

1. **Login pelo Azure CLI, sem PAT (recomendado).** A pessoa entra uma vez no navegador. O Azure CLI renova o acesso sozinho enquanto ela usa o plugin. A empresa pode pedir um login novo antes, por política de acesso condicional ou troca de senha.
2. **PAT.** A pessoa cria um token no Azure DevOps com validade fixa, escolhida na criação. A política da organização pode limitar essa validade. Quando o PAT vence, a pessoa cria outro. Indique o PAT para quem precisa de um acesso que não peça login, ou para organização sem Microsoft Entra.

Depois da escolha, chame `ado_setup_org` com `auth="az"` ou `auth="pat"` e `confirm=true`. As fontes e os detalhes estão em `${CLAUDE_PLUGIN_ROOT}/docs/setup.md`.

## PAT

Use quando a pessoa escolher o PAT no passo 4 ou quando o passo 10 pedir.

1. Peça à pessoa que siga `${CLAUDE_PLUGIN_ROOT}/docs/pat.md`.
2. Escopos mínimos: Work Items (Read & write) e Project and Team (Read). Não aceite Full access.
3. Escolha a rota de gravação. A pessoa grava o PAT no próprio terminal, com entrada oculta, ou usa o NordPass:
   - Cofre do sistema: `ado_cli.py pat-set --org <org>`. Mostre o comando com o caminho expandido.
   - NordPass: a pessoa cria o item de senha no app NordPass e informa só o título. Depois do sim, chame `ado_setup_org` com `auth="pat"`, `pat_ref={nordpass_title, nordpass_folder}` e `confirm=true`. O terminal faz o mesmo com `ado_cli.py pat-set --org <org> --nordpass-title <título> [--nordpass-folder <pasta>]`.
4. Confira com `ado_cli.py pat-check --org <org>`. O resultado esperado é `encontrado=sim` e `http=200`.

O `pat-set` só muda a organização para `auth=pat` depois do `http=200`. O `ado_setup_org` muda na hora, sem conferir: rode o `pat-check` depois dele.

Não peça o PAT no chat. Não use variável de ambiente nem arquivo. Se a pessoa colar o PAT no chat, avise que o token vazou para o histórico da conversa. Oriente a revogar o token e criar outro.

Mais detalhes: `${CLAUDE_PLUGIN_ROOT}/docs/setup.md` e `${CLAUDE_PLUGIN_ROOT}/docs/perfil.md`.

---
name: azure-devops
description: Use para qualquer pedido sobre Azure DevOps (ADO) e work items: task, user story, bug, feature, épico; criar, consultar, listar o que está atribuído, mudar estado, comentar, anexar evidência, vincular pai ou link. Ex.: "cria uma task na US 123", "o que tenho em aberto no ADO", "muda a 456 para em andamento". Use for any Azure DevOps work item request: create, query, update, comment, attach. Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: roteadora

Esta skill escolhe a operação e a skill certa. Se o projeto atual tiver skill própria de Azure DevOps para o pedido, use a do projeto e pare aqui.

## Passos

1. Chame `ado_status`.
2. Se as ferramentas `ado_*` não existirem, vá ao passo 7.
3. Se `pending_setup` não estiver vazio, carregue a skill `ado-configurar` e pare.
4. Se a org colada ou padrão tiver `usage.work_items` igual a `false` no perfil, mostre o `hint` do perfil e pergunte qual org usar.
5. Extraia org, projeto e id de uma URL colada. Formato: `dev.azure.com/<org>/<projeto>/_workitems/edit/<id>`.
6. Classifique o pedido e carregue a skill da operação:

| Pedido | Skill |
|---|---|
| ler, listar, filtrar, consulta WIQL | `ado-consultar` |
| criar item | `ado-criar` |
| mudar estado, campo, pai ou link | `ado-atualizar` |
| comentar ou mencionar | `ado-comentar` |
| anexar arquivo ou evidência | `ado-anexar` |
| login, org nova, perfil, PAT | `ado-configurar` |

7. Se não houver ferramentas `ado_*`, diga o texto abaixo. Não invente resultado. Ofereça montar o rascunho do payload.

> Esta sessão não tem acesso ao Azure DevOps pela sua máquina. No Cowork em nuvem, o plugin só monta o rascunho. Para executar, use o Claude Code ou uma sessão local.

## Regras

- Toda escrita segue o ciclo: `ado_prepare_*`, prévia completa, OK explícito da pessoa, `ado_apply`.
- Nunca peça PAT, senha ou token no chat.
- Detalhes do setup: `${CLAUDE_PLUGIN_ROOT}/docs/setup.md`.

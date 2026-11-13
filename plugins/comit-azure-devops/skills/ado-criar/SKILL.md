---
name: ado-criar
description: Use para criar work item no Azure DevOps: task, user story, bug, feature ou épico. Ex.: "cria uma task de revisão na US 123", "abre uma user story para o painel X". Use to create an Azure DevOps work item: task, user story, bug, feature or epic ("create a task under story 123", "open a story for X"). Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: criar

Esta skill escreve no Azure DevOps. Ela não envia nada antes do OK explícito da pessoa.

## Passos

1. Se as ferramentas `ado_*` não existirem, mostre a mensagem fixa da skill `azure-devops` (passo 7) e ofereça montar o rascunho do payload. Se não, chame `ado_status`. Se faltar configuração, carregue `ado-configurar`.
2. Resolva org, projeto e tipo. Se houver item pai, use o projeto dele.
3. Chame `ado_type_schema`. Ele mostra os obrigatórios na criação, as listas de valores e os padrões.
4. Preencha pelo contexto: caminho de área do pai, sprint (`ado_iterations`), datas, responsável padrão e padrões do perfil.
5. Pergunte só o que faltar. Mostre as opções da lista de valores.
6. Confirme sempre sprint atual ou backlog. A pessoa decide o escopo da sprint.
7. Chame `ado_prepare_create`. A ferramenta valida com `validateOnly` e procura duplicata.
8. Mostre a prévia completa: tabela de campos, pai, aviso de duplicata e resultado da validação.
9. Se o Azure DevOps recusar a validação, mostre os campos citados e volte ao passo 5.
10. Pergunte: "Aprovar ou ajustar?" Espere o OK explícito.
11. Chame `ado_apply` com os valores de `apply_args` da prévia (`preview_id`, `hash`, `operation`, `org`, `type`, `title`).
12. Mostre id, URL, estado e pai lidos de volta.

## Regras

- Use como responsável o `uniqueName` que a API resolveu. Nunca deduza um e-mail.
- Não envie `CompletedWork` na criação.
- Não use `bypassRules`.
- Se a prévia vencer ou a ferramenta devolver `PREVIEW_MISMATCH`, refaça o `ado_prepare_create`.
- Se `ado_apply` falhar de modo ambíguo (tempo esgotado, erro 5xx), chame `ado_reconcile` antes de tentar de novo.

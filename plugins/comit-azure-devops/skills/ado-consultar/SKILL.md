---
name: ado-consultar
description: Use para ler ou listar work items do Azure DevOps: "o que está atribuído a mim", "mostra a task 123", "filhos da US 456", "tasks em andamento do projeto X", "sprint atual", consulta WIQL. Use to read or list Azure DevOps work items: "what's assigned to me", "show task 123", "children of story 456", "current sprint", WIQL. Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: consultar

Esta skill só lê. Nenhuma ferramenta daqui altera o Azure DevOps.

## Passos

1. Chame `ado_status`. Se faltar configuração, carregue `ado-configurar`.
2. Escolha a ferramenta:

| Pedido | Ferramenta |
|---|---|
| Um item | `ado_get` (`expand=relations` para ver vínculos) |
| Filhos de um item | `ado_children` |
| Atribuídos a mim, por tipo, estado ou pai | `ado_query` com filtros |
| Consulta livre | `ado_query` com `wiql` |
| Comentários de um item | `ado_comments` |
| Sprint atual ou futura | `ado_iterations` |
| Projetos ou equipes | `ado_list_projects`, `ado_list_teams` |

3. Mostre o resultado numa tabela curta: id, tipo, título, estado, responsável, data alvo.
4. Se houver data alvo, ordene por urgência. Marque atraso com um sinal simples.
5. Se a pessoa colar uma URL de item, extraia org, projeto e id dela.

A consulta funciona sem perfil de org, porque leitura não depende de campos obrigatórios.

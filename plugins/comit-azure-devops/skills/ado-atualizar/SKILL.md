---
name: ado-atualizar
description: Use para mudar estado ou campos de work item do Azure DevOps: "inicia a task 123", "conclui a 456", "cancela", "muda a data", "zera o restante", "coloca a 789 como filha da US 100", "adiciona o link na task". Use to change state or fields of an Azure DevOps work item: start, complete, cancel, reopen, set parent, add a link. Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: atualizar

Esta skill escreve no Azure DevOps. Ela não envia nada antes do OK explícito da pessoa.

## Passos

1. Se as ferramentas `ado_*` não existirem, mostre a mensagem fixa da skill `azure-devops` (passo 7) e ofereça montar o rascunho do payload. Se não, chame `ado_status`. Se faltar configuração, carregue `ado-configurar`.
2. Chame `ado_get` e leia o item.
3. Chame `ado_prepare_update` com `fields`, `state_intent`, `add_parent`, `add_hyperlinks` ou `remove_relations`.
4. Mostre a prévia completa: o que muda, de qual valor para qual valor, e o resultado do `validateOnly`.
5. Pergunte: "Aprovar ou ajustar?" Espere o OK explícito.
6. Chame `ado_apply` com os valores de `apply_args` da prévia (`preview_id`, `hash`, `operation`, `org`, `target_id`, `title`).
7. Mostre estado, pai e relações lidos de volta.

## Estado por intenção

| Pedido | `state_intent` | Categoria do estado |
|---|---|---|
| iniciar | `iniciar` | InProgress |
| concluir | `concluir` | Completed |
| cancelar | `cancelar` | Removed |
| reabrir | `reabrir` | Proposed |

- O nome real do estado vem do tipo do item no perfil da org. Se a pessoa disser um nome exato, use-o.
- Não avalie regras de transição por conta própria. O `validateOnly` decide.
- Não use a categoria Removed para trabalho feito. Não use Completed para trabalho não feito.

## Regras

- Para `System.Tags`, a ferramenta lê as tags e concatena com `;`. Nunca substitua as tags existentes.
- Ao vincular um pai, mostre antes o estado e o trabalho restante do pai. A vinculação pode mover o pai.
- Para um link, informe a URL e o texto da relação.
- A ferramenta usa a revisão lida na prévia. Se o item mudar antes do `ado_apply`, refaça a prévia.
- Se `ado_apply` falhar de modo ambíguo, chame `ado_reconcile` antes de tentar de novo.

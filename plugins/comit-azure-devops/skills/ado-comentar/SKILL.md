---
name: ado-comentar
description: Use para comentar em work item do Azure DevOps ou mencionar alguém: "comenta na task 123 que...", "avisa o QA na US 456". Use to comment on an Azure DevOps work item or @mention someone: "comment on task 123 that...", "ping QA on story 456". Se o projeto tiver skill própria de Azure DevOps para o pedido, use a do projeto.
---

# Azure DevOps: comentar

Esta skill escreve no Azure DevOps. Nada é enviado antes do OK explícito da pessoa.

## Passos

1. Chame `ado_status`. Se faltar configuração, carregue `ado-configurar`.
2. Escreva o comentário em HTML, não em Markdown. Tags aceitas: `<br>`, `<strong>`, `<code>`, `<a>`, `<ul>`, `<li>`.
3. Para cada pessoa citada, chame `ado_resolve_identity`. Se houver dois candidatos ou mais, pergunte qual.
4. Chame `ado_prepare_comment` com `html` e `mentions`.
5. Mostre a prévia completa: o texto legível e o HTML final.
6. Pergunte: "Aprovar ou ajustar?" Espere o OK explícito.
7. Chame `ado_apply` com `preview_id`, `hash`, `operation=comment`, `org`, `target_id` e `title`.
8. Confirme que o comentário apareceu na releitura.

## Regras

- A ferramenta cria a menção com o identificador da pessoa. Não monte a menção à mão.
- Não grave identidade de pessoa no perfil nem em arquivo.
- Comentário não tem validação prévia no Azure DevOps. A prévia e o OK são a única trava.
- Se `ado_apply` falhar de modo ambíguo, chame `ado_reconcile` antes de tentar de novo.

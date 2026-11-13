# Perfil de organização

O perfil descreve como uma organização do Azure DevOps usa work items. O plugin usa o perfil para preencher e validar uma criação antes de enviar.

## O que o perfil contém

- Processos e tipos de work item ativos.
- Estados de cada tipo e a categoria de cada estado (Proposed, InProgress, Resolved, Completed, Removed).
- Campos obrigatórios na criação e campos exigidos por regra.
- Listas de valores e valores padrão.
- Tipos de vínculo e versões de API.
- O padrão de impedimento de cada tipo (`blocking`), quando a organização tem um.

O perfil não contém segredo, nome de pessoa, e-mail nem identificador de pessoa.

## Chave por processo

O perfil se organiza por processo, não por projeto. Projetos do mesmo processo compartilham os tipos. Um projeto fora da lista do perfil usa o processo dele, descoberto na hora.

## Origem do perfil

| Origem | Onde fica | Quando vale |
|---|---|---|
| Embutido | `profiles/` do plugin, só leitura | Organizações pré-mapeadas (`MDB-CIT`, `MDB-DATAPLATFORM`), como exemplo |
| Aprendido | Configuração local da pessoa | Organização nova, depois da descoberta e da sua confirmação |

Precedência: o perfil aprendido vale mais que o embutido.

## Descoberta em organização nova

A skill `ado-configurar` chama `ado_discover_org`. A descoberta só lê (GET):

1. Descobre o tenant pela resposta da organização.
2. Lista processos e projetos.
3. Lista os tipos de work item do processo.
4. Lê campos, listas de valores e estados de cada tipo.
5. Lê as regras do processo que tornam um campo obrigatório.

O `ado_status` avisa `PROFILE_ABSENT` quando a organização não tem perfil. Nesse caso a skill chama `ado_discover_org`. Ela mostra o resumo da proposta e, se já houver perfil, a diferença contra ele. Depois do seu sim, `ado_profile_save(proposal_id, confirm=true)` grava o perfil na configuração local. A proposta fica só na memória do servidor: se o servidor reiniciar, rode a descoberta de novo.

## A validação final

O perfil pode ficar incompleto. Regras condicionais por valor de campo nem sempre aparecem na leitura. Por isso, o `ado_prepare_create` envia a criação com `validateOnly`. O Azure DevOps decide, sem gravar nada. Se ele recusar, o plugin devolve `VALIDATION_FAILED` com o diagnóstico do Azure DevOps, que lista os erros. O assistente mostra a lista e pede a sua decisão. O plugin não atualiza o perfil sozinho: rode a descoberta de novo para atualizá-lo.

## Padrão de impedimento

O bloco `blocking` de um tipo diz como a organização registra um impedimento. Ele é opcional. Sem ele, `ado_prepare_block` responde `BLOCKING_NOT_MAPPED`.

```json
"blocking": {
  "fields": {
    "flag": {"field": "Microsoft.VSTS.CMMI.Blocked", "value_on": "Yes", "value_off": "No"},
    "responsible": "<campo de identidade>",
    "reason": "<campo de texto>",
    "date": "<campo de data>",
    "forecast": "<campo de data>"
  },
  "policy": {
    "responsible_default": "parent_assigned_to",
    "date_default": "today",
    "forecast_days": 2,
    "kinds": {"infra": "de infraestrutura", "funcional": "funcional"},
    "comment_templates": {
      "default": "Impedimento {tipo}: {motivo}. Aguardando: {esperado} — responsável: @{responsavel}. Previsão: {previsao}.",
      "unblock": "Impedimento resolvido: {resolucao}."
    },
    "clear_on_unblock": [],
    "suggest_on_block": true
  }
}
```

- `fields.flag` é obrigatório. Os outros campos são opcionais.
- `responsible_default`: `parent_assigned_to` (responsável do pai), `item_assigned_to` (responsável do item) ou `ask` (pergunta).
- `forecast_days`: dias corridos depois de hoje para a previsão, de 0 a 60. Sem ele, o plugin pergunta a previsão.
- `comment_templates`: um modelo por tipo de impedimento e o `default`. O `unblock` é o comentário de resolução. Os marcadores são `{tipo}`, `{motivo}`, `{esperado}`, `{responsavel}`, `{previsao}` e `{resolucao}`. `@{responsavel}` vira menção.
- `clear_on_unblock`: papéis que o fim do impedimento limpa. Vazio mantém os campos como histórico.
- Sem `policy`, o plugin pergunta o responsável e a previsão e usa um comentário neutro.

A descoberta infere `fields` pela regra que exige campos quando a flag está ligada. Ela nunca infere `policy`. Ao redescobrir, o plugin mantém a `policy` do perfil atual.

## Organização sem work items

Um perfil pode marcar `usage.work_items` como `false` e trazer um `hint`. A skill `azure-devops` mostra o `hint` e pergunta qual organização usar.

## Mudou o processo da organização?

Rode a descoberta de novo. Ela mostra a diferença contra o perfil atual. Grave só depois de conferir.

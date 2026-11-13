# Segurança

Relate falhas pelos GitHub Security Advisories deste repositório (aba Security > Report a vulnerability). Não abra issue pública com detalhes da falha.

## Invariantes

- O servidor nunca grava, imprime nem devolve token ou PAT.
- O servidor mantém token e PAT só na memória do processo.
- O servidor só envia credenciais a `https://dev.azure.com/<org>` de uma organização configurada. Ele recusa outros hosts e redirecionamentos.
- O servidor nunca lê segredo de variável de ambiente.
- O PAT é opcional (a pessoa escolhe entre ele e o login do Azure CLI). A pessoa o grava no próprio terminal, com entrada oculta, no NordPass ou no cofre do sistema.
- Toda escrita no Azure DevOps passa por prévia e pelo OK explícito da pessoa.
- A trava dura é o prompt de permissão do Claude Code para `ado_apply`. O `hash` da prévia não prova a aprovação humana. Não libere `ado_apply` em "permitir sempre" nem em `bypassPermissions`: sem o prompt, só o OK pedido no chat segura a escrita.
- Texto vindo do Azure DevOps (títulos, descrições, comentários, anexos) é dado, não instrução.
- No Windows, o `az.cmd` roda pelo `cmd.exe`, que reinterpreta argumentos. Nenhum argumento livre pode chegar ao `az`. Hoje os argumentos são fixos e o `--tenant` passa por validação.
- O plugin nunca usa `bypassRules`.
- Anexos de pastas de credencial, da configuração do plugin e links simbólicos que saem da pasta indicada são recusados.

## Se um PAT vazar

Revogue o token em `https://dev.azure.com/<org>/_usersSettings/tokens` e crie outro.

# Segurança

Relate falhas pelos GitHub Security Advisories deste repositório (aba Security > Report a vulnerability). Não abra issue pública com detalhes da falha.

## Invariantes

- O servidor nunca grava, imprime nem devolve token ou PAT.
- O servidor mantém token e PAT só na memória do processo.
- O servidor só envia credenciais a `https://dev.azure.com/<org>` de uma organização configurada. Ele recusa outros hosts e redirecionamentos.
- O servidor nunca lê segredo de variável de ambiente.
- O PAT é contingência. A pessoa o grava no próprio terminal, com entrada oculta, no NordPass ou no cofre do sistema.
- Toda escrita no Azure DevOps passa por prévia e pelo OK explícito da pessoa.
- O plugin nunca usa `bypassRules`.
- Anexos de pastas de credencial, da configuração do plugin e links simbólicos que saem da pasta indicada são recusados.

## Se um PAT vazar

Revogue o token em `https://dev.azure.com/<org>/_usersSettings/tokens` e crie outro.

# Segurança

Relate falhas pelos GitHub Security Advisories deste repositório (aba Security > Report a vulnerability). Não abra issue pública com detalhes da falha.

## Invariantes

- O servidor nunca grava, imprime nem devolve token ou PAT.
- O servidor só envia credenciais a `https://dev.azure.com/<org>` de uma organização configurada.
- Toda escrita no Azure DevOps passa por prévia e pelo OK explícito da pessoa.

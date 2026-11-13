# comit-azure-devops

Plugin do Claude Code e do Claude Cowork para operar work items do Azure DevOps em linguagem natural.

Estado: esqueleto (0.0.1). O servidor MCP responde só à ferramenta `hello`.

## Instalar no Claude Code

```text
/plugin marketplace add COMIT-inf-br/comit-azure-devops
/plugin install comit-azure-devops@comit-azure-devops
```

O repositório ainda não está publicado.

## Requisitos

- `uv` (instalação sem admin).
- Azure CLI (`az`), para o login.

## Estrutura

- `.claude-plugin/marketplace.json`: marketplace.
- `plugins/comit-azure-devops/`: o plugin.
- `scripts/check_identity.py`: verificação V7 (sem identidade nem segredo no repositório).

# comit-azure-devops

Plugin do Claude Code e do Claude Cowork para operar work items do Azure DevOps em linguagem natural: criar, consultar, atualizar, comentar e anexar. Este repositório é também o marketplace do plugin.

## Instalar no Claude Code

```text
/plugin marketplace add COMIT-inf-br/comit-azure-devops
/plugin install comit-azure-devops@comit-azure-devops
```

## Versão

A versão atual é a 0.1.0 (ainda não publicada). Ela traz o servidor MCP local, as sete skills por intenção, a escrita em duas fases (prévia, OK, `ado_apply`), os perfis embutidos e a descoberta de organização nova. O histórico está em [`CHANGELOG.md`](CHANGELOG.md).

## Requisitos

- `uv` (instalação sem admin).
- Azure CLI (`az`), para o login.

Guia completo: [`plugins/comit-azure-devops/docs/setup.md`](plugins/comit-azure-devops/docs/setup.md).

## Como usar

Peça em linguagem natural. Exemplos:

- "o que tenho atribuído no Azure DevOps"
- "cria uma task de revisão na US 123"
- "muda a 456 para em andamento"
- "comenta na task 123 que o teste passou"
- "anexa o relatório na task 123"

No primeiro uso, o plugin conduz o login e a configuração da organização.

## Claude Code e Cowork

| Superfície | Resultado |
|---|---|
| Claude Code | Execução completa |
| Cowork, sessão local | Execução completa, com melhor esforço |
| Cowork, sessão em nuvem | Só rascunho do payload. O servidor local não roda na nuvem |

Para instalar no Cowork, use o diretório da organização, o marketplace por URL Git ou o envio de um zip do plugin.

## Segurança

- Login padrão: `az login` da própria pessoa. PAT só como contingência, gravado por você no terminal.
- O token fica só na memória. Nenhum segredo vai a variável de ambiente, arquivo ou chat.
- Toda escrita mostra uma prévia completa e espera o seu OK.

Veja [`SECURITY.md`](SECURITY.md) e as [permissões recomendadas](plugins/comit-azure-devops/README.md#permissões-recomendadas).

## Estrutura

- `.claude-plugin/marketplace.json`: marketplace.
- `plugins/comit-azure-devops/`: o plugin (skills, servidor MCP, perfis, docs).
- `plugins/comit-azure-devops/evals/`: suíte do `claude plugin eval` (disparo por intenção).
- `scripts/check_identity.py`: confere que o repositório não leva identidade pessoal nem segredo.
- `CHANGELOG.md`: histórico de versões.

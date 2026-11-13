# Setup

O plugin precisa de duas instalações: `uv` e Azure CLI (`az`). A autenticação padrão é o `az login` da própria pessoa. Não precisa de PAT.

## 1. Instale o uv

O `uv` roda o servidor do plugin. A instalação não pede admin.

- macOS e Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Reinicie o Claude depois da instalação. O app gráfico pode não enxergar o `PATH` do terminal. Se o servidor não subir, abra o app pelo terminal ou ponha `~/.local/bin` no `PATH` do sistema.

## 2. Instale o Azure CLI

- Sem admin, em qualquer sistema: `uv tool install azure-cli`
- Com admin, macOS: `brew install azure-cli`
- Com admin, Windows: `winget install Microsoft.AzureCLI`

O servidor procura o `az` no `PATH` e nos locais de instalação comuns. Se ele não achar, grave o caminho em `az_path` na configuração local.

## 3. Instale o plugin

```text
/plugin marketplace add COMIT-inf-br/comit-azure-devops
/plugin install comit-azure-devops@comit-azure-devops
```

## 4. Conecte uma organização

Peça no chat: "configura o Azure DevOps". A skill `ado-configurar` conduz estes passos:

1. Pergunta a organização. Cole o link de um work item, se quiser.
2. Abre o login da Microsoft no navegador. Entre com a conta da organização.
3. Mostra quem você é e pergunta se grava como responsável padrão.
4. Usa o perfil embutido da organização ou descobre o perfil (veja `perfil.md`).

Se o navegador não abrir, a skill usa o código de dispositivo: ela mostra uma URL e um código.

## Onde fica a configuração local

| Sistema | Pasta |
|---|---|
| macOS | `~/Library/Application Support/comit-azure-devops/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/comit-azure-devops/` |
| Windows | `%APPDATA%\comit-azure-devops\` |

A pasta guarda organizações, padrões pessoais e perfis aprendidos. Ela nunca guarda token nem PAT.

## Diagnóstico

Rode no terminal:

```text
uv run --python 3.12 --no-project <pasta-do-plugin>/server/ado_cli.py doctor
```

Acrescente `--org <org>` para conferir uma organização. A saída mostra cada verificação e nunca mostra segredo.

## Claude Code e Cowork

- Claude Code: execução completa.
- Cowork em sessão local: execução completa, com melhor esforço.
- Cowork em nuvem: o servidor local não roda. O plugin monta só o rascunho do payload e avisa.

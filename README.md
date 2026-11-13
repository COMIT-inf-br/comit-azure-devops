# comit-azure-devops

Plugin do Claude Code e do Claude Cowork para operar work items do Azure DevOps em linguagem natural: criar, consultar, atualizar, comentar, anexar, montar sprint, registrar impedimento e transformar reunião em comentários. Este repositório é também o marketplace do plugin.

Para instalar, mande o link deste repositório ao seu Claude e peça: "instala esse plugin para mim". A seção abaixo foi escrita para ele seguir.

## Instalação (para o seu assistente Claude seguir)

Siga os passos na ordem. Cada passo tem uma conferência. Se a conferência falhar, pare e mostre a saída à pessoa.

### 1. Confira o `uv`

1. Rode `uv --version`.
2. Se o comando não existir, instale. A instalação não pede admin.
   - macOS e Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
3. Abra um terminal novo e rode `uv --version` de novo. Se ainda não achar, ponha `~/.local/bin` no `PATH` (no Windows, `%USERPROFILE%\.local\bin`).

### 2. Confira o Azure CLI (`az`)

1. Rode `az version`.
2. Se o comando não existir, instale. Prefira a opção sem admin.
   - Sem admin, em qualquer sistema: `uv tool install azure-cli`
   - Com admin, macOS: `brew install azure-cli`
   - Com admin, Windows: `winget install Microsoft.AzureCLI`
3. Rode `az version` de novo. A saída precisa mostrar `azure-cli`.

O `az` é necessário para o login pelo Azure CLI, o modo recomendado. Quem for usar só PAT pode pular este passo.

### 3. Instale o plugin

No terminal:

```text
claude plugin marketplace add COMIT-inf-br/comit-azure-devops
claude plugin install comit-azure-devops@comit-azure-devops
```

Ou, dentro do Claude Code:

```text
/plugin marketplace add COMIT-inf-br/comit-azure-devops
/plugin install comit-azure-devops@comit-azure-devops
```

Confira com `claude plugin list`. A lista precisa mostrar `comit-azure-devops@comit-azure-devops`.

### 4. Reinicie a sessão

1. Feche o Claude Code (ou o app) e abra de novo. O servidor do plugin só sobe numa sessão nova.
2. No app gráfico (macOS), o `PATH` do terminal pode não valer. Se o servidor não subir, abra o app pelo terminal.

### 5. Confira o servidor

1. Peça no chat: "status do Azure DevOps".
2. O Claude chama a ferramenta `ado_status` (prefixo `mcp__plugin_comit-azure-devops_ado__`).
3. A resposta mostra o Python, o `az` e as organizações configuradas. Numa máquina nova, ela traz `NO_ORG`: isso é o esperado.
4. Se as ferramentas `ado_*` não aparecerem, veja o FAQ.

### 6. Primeiro uso: escolha do login

Peça no chat: "configura o Azure DevOps". A skill `ado-configurar` pergunta a organização e como a pessoa quer entrar:

| Opção | Como funciona | Para quem |
|---|---|---|
| (1) Login pelo Azure CLI, sem PAT (recomendado) | Login da Microsoft no navegador. O acesso se renova sozinho enquanto a pessoa usa o plugin. A empresa pode pedir login novo antes, por política. | Uso normal |
| (2) PAT | Token criado pela pessoa no Azure DevOps, com validade fixa escolhida na criação e limitada pela política da organização. | Quem precisa de acesso que não peça login, ou organização sem Microsoft Entra |

Deixe a pessoa escolher. Nunca peça PAT, senha ou token no chat. As fontes da Microsoft estão em [`docs/setup.md`](plugins/comit-azure-devops/docs/setup.md#escolha-do-login).

### Claude Desktop e Cowork

| Superfície | Resultado |
|---|---|
| Claude Code (terminal, macOS, Windows, Linux) | Execução completa |
| Claude Desktop, sessão de código local | Execução completa |
| Cowork, sessão local | Execução completa, com melhor esforço |
| Cowork, sessão em nuvem | Só rascunho do payload. O servidor local não roda na nuvem |

No Cowork, instale pelo diretório da organização, pelo marketplace por URL Git (`https://github.com/COMIT-inf-br/comit-azure-devops`) ou pelo envio de um zip do plugin.

### Atualizar

```text
claude plugin marketplace update comit-azure-devops
claude plugin update comit-azure-devops@comit-azure-devops
```

Reinicie a sessão depois da atualização.

### Desinstalar

```text
claude plugin uninstall comit-azure-devops@comit-azure-devops
claude plugin marketplace remove comit-azure-devops
```

A configuração local fica na máquina (organizações, padrões e perfis aprendidos). Para apagar, remova a pasta: macOS `~/Library/Application Support/comit-azure-devops/`, Linux `~/.config/comit-azure-devops/`, Windows `%APPDATA%\comit-azure-devops\`. O login do Azure CLI sai com `az logout`.

## Como usar

Peça em linguagem natural, em português ou inglês. Exemplos por cenário:

| Cenário | Exemplo de pedido |
|---|---|
| Criar história e tasks | "cria uma US 'Painel de vendas' e três tasks: extração, modelo e testes" |
| Criar task numa história | "cria uma task de revisão na US 123, 2 horas, começa hoje" |
| Consultar meus itens | "o que tenho atribuído no Azure DevOps?" |
| Mudar estado | "inicia a task 456" · "conclui a 456" |
| Resumir comentários | "resume os comentários da 456" |
| Reunião ou daily → comentários | "transforma esta transcrição do Teams em comentários nos cards" (anexe o `.vtt` ou o `.docx`) |
| Tirar do backlog e montar sprint | "o que tem no backlog do time?" · "coloca a US 123 na sprint 4, com as tasks" |
| Carga da sprint | "como está a carga por pessoa na sprint atual?" |
| Virada de sprint | "fecha a sprint e abre a próxima, com o relatório do que vai e do que fica" |
| Criar sprint | "cria a próxima sprint do time" |
| Impedimento | "a task 789 travou, aguardando o chamado de acesso" · "desimpede a 789, o acesso saiu" |
| Anexar evidência | "anexa o relatório.pdf na task 123" |
| Comentar e mencionar | "comenta na task 123 que o teste passou e avisa a Ana" |
| Excluir para a lixeira | "exclui a task 321" (vai para a lixeira do projeto e pode ser restaurada) |
| Organização nova | "configura a organização exemplo no Azure DevOps" (o plugin descobre os campos obrigatórios e pergunta antes de gravar) |

O impedimento segue o padrão do perfil da organização, quando o perfil define um. Sem padrão, o plugin avisa e oferece mapear.

## O que o plugin pergunta antes de gravar

Toda escrita tem duas fases:

1. **Prévia.** O plugin mostra tudo o que vai mudar: campos de → para, comentário com menções, arquivo com tamanho e hash, itens de um lote. Na criação e na atualização, o Azure DevOps valida a prévia com `validateOnly`, sem gravar.
2. **OK.** Só depois do seu OK explícito o plugin envia (`ado_apply`).

Lote parcial (rede, login, limite de taxa) pode ser repetido: o plugin envia só o que faltou. Item que mudou depois da prévia é recusado e pede prévia nova.

## Segurança e permissões

- O token fica só na memória do servidor. Nenhum segredo vai a variável de ambiente, arquivo ou chat.
- O PAT, quando escolhido, é gravado por você no terminal, no NordPass ou no cofre do sistema.
- O servidor só fala com `https://dev.azure.com/<org>` de uma organização configurada.
- O plugin nunca usa `bypassRules` e nunca exclui em definitivo.
- Deixe `ado_apply` sempre no prompt de permissão. Não use "permitir sempre" nem `bypassPermissions` para ele.

Detalhes: [`SECURITY.md`](SECURITY.md) e as [permissões recomendadas](plugins/comit-azure-devops/README.md#permissões-recomendadas).

## FAQ

**As ferramentas `ado_*` não aparecem.** O servidor não subiu. Rode `uv --version` e `az version` num terminal. Reinicie a sessão. No app gráfico, abra o app pelo terminal para ele herdar o `PATH`. No Cowork em nuvem, o servidor local não roda: o plugin só monta rascunhos.

**O login venceu.** Peça "refaz o login do Azure DevOps". O plugin abre o login no navegador. Se o navegador não abrir, ele mostra um código de dispositivo.

**PAT ou Azure CLI?** Use o Azure CLI: ele se renova sozinho e não guarda segredo seu. Use PAT só se precisar de acesso que não peça login, ou se a organização não usar Microsoft Entra. Veja [`docs/setup.md`](plugins/comit-azure-devops/docs/setup.md#escolha-do-login).

**Funciona no Cowork em nuvem?** Só como rascunho. O servidor precisa rodar na sua máquina. Para executar, use o Claude Code ou uma sessão local.

**A minha organização tem campos obrigatórios próprios.** O plugin descobre os campos e as regras por leitura e pergunta antes de gravar o perfil. Veja [`docs/perfil.md`](plugins/comit-azure-devops/docs/perfil.md).

## Versão

A versão atual é a 0.3.0 (ainda não publicada). Ela traz o impedimento pelo padrão do perfil e a escolha explícita entre login pelo Azure CLI e PAT. O histórico está em [`CHANGELOG.md`](CHANGELOG.md).

Organizações com perfil embutido: `MDB-CIT` e `MDB-DATAPLATFORM`. Outras organizações funcionam pela descoberta.

## Estrutura

- `.claude-plugin/marketplace.json`: marketplace.
- `plugins/comit-azure-devops/`: o plugin (skills, servidor MCP, perfis, docs).
- `plugins/comit-azure-devops/evals/`: suíte do `claude plugin eval` (disparo por intenção).
- `scripts/check_identity.py`: confere que o repositório não leva identidade pessoal nem segredo.
- `CHANGELOG.md`: histórico de versões.

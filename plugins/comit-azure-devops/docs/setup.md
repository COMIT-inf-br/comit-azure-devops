# Setup

O plugin precisa de duas instalações: `uv` e Azure CLI (`az`). Na primeira configuração, você escolhe o login: Azure CLI (recomendado, sem PAT) ou PAT.

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
2. Pergunta como você quer entrar (veja "Escolha do login").
3. No login pelo Azure CLI, abre o login da Microsoft no navegador. Entre com a conta da organização. No PAT, segue [`pat.md`](pat.md).
4. Mostra quem você é e pergunta se grava como responsável padrão.
5. Usa o perfil embutido da organização ou descobre o perfil (veja [`perfil.md`](perfil.md)).

Se o navegador não abrir, a skill usa o código de dispositivo: ela mostra uma URL e um código. A skill acompanha o login chamando `ado_auth_login` de novo ou `ado_status`.

## Escolha do login

| | (1) Azure CLI, sem PAT | (2) PAT |
|---|---|---|
| Como entra | Login da Microsoft no navegador, uma vez | Token criado por você no Azure DevOps |
| Quanto dura | Renova sozinho enquanto você usa | Validade fixa, escolhida na criação |
| Quando pede de novo | Quando a política da empresa pedir, ou depois de muito tempo sem uso | Quando o token vence ou é revogado |
| Onde fica | Cache do Azure CLI na sua máquina | NordPass ou cofre do sistema |
| Indicado para | Uso normal (recomendado) | Acesso que não pode pedir login, ou organização sem Microsoft Entra |

### Por que o login do Azure CLI dura

- O Azure CLI guarda um refresh token quando você entra com a sua conta. O access token dura pouco, e o Azure CLI troca o refresh token por um novo access token quando precisa ([Azure CLI: refresh tokens](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli#refresh-tokens)).
- O refresh token se renova a cada uso. A validade padrão dele é de 90 dias neste tipo de app ([Microsoft Entra: refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens#token-lifetime)). Na prática, a validade desliza enquanto você usa o plugin.
- O serviço de login pode revogar o refresh token antes, por exemplo quando você troca a senha ou um administrador revoga as sessões ([revogação](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens#token-revocation)).
- A empresa pode definir uma frequência de login por acesso condicional. Nesse caso, o login novo vem antes. O padrão do Microsoft Entra é uma janela de 90 dias que desliza com o uso ([frequência de login](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime#user-sign-in-frequency)).

### Por que o PAT vence

- Você escolhe a data de vencimento na criação do PAT ([criar um PAT](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate#create-a-pat)).
- O administrador pode impor um máximo de dias para PATs novos ([política de validade máxima](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/manage-pats-with-policies-for-administrators#set-maximum-lifespan-for-new-pats-tenant-policy)). A organização também pode restringir a criação de PATs.
- A Microsoft recomenda evitar PAT quando houver autenticação pelo Microsoft Entra ([aviso da Microsoft](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate)).
- Escolha a menor validade que atenda você. Antes do vencimento, crie outro PAT e grave de novo (veja [`pat.md`](pat.md)).

Crie o PAT para uma organização só. O Azure DevOps está aposentando o PAT global ([aviso da Microsoft](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate)).

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

## As ferramentas `ado_*` não aparecem

Duas causas diferentes dão o mesmo sintoma.

- No Claude Code e no Claude Desktop, o servidor do plugin não subiu. O app gráfico pode não enxergar o `uv` no PATH. Rode `uv --version` num terminal. Confira que `uv` e `az` estão no PATH. Reinicie o app ou a sessão.
- No Cowork em nuvem, o plugin só monta rascunhos. Para executar, use o Claude Code ou uma sessão local.

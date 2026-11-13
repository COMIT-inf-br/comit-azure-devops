# PAT

Use um PAT (token de acesso pessoal) se você escolheu o PAT na configuração, ou se a organização não aceita o login do Azure CLI. O caminho recomendado é o `az login`. A comparação e as fontes estão em [`setup.md`](setup.md#escolha-do-login).

Regras que valem sempre:

- Grave o PAT você mesmo, no seu terminal, com entrada oculta.
- Não cole o PAT no chat.
- Não guarde o PAT em variável de ambiente nem em arquivo.
- O servidor lê o PAT sob demanda e só o mantém na memória.

## 1. Crie o token

1. Abra `https://dev.azure.com/<org>/_usersSettings/tokens`.
2. Crie um token novo.
3. Em Organization, escolha só a organização do plugin.
4. Em Scopes, escolha Custom defined.
5. Marque só **Work Items: Read & write**. Esse escopo cobre criar, comentar e anexar.
6. Marque **Project and Team: Read**. Sem ele, a lista de equipes e membros dá erro 401.
7. Escolha a menor validade que a organização permitir.

Não marque Full access, Code, Wiki nem escopos de administração. Read sozinho dá erro 401 ao criar, comentar ou anexar.

## 2. Guarde o token

Escolha uma das duas rotas. O servidor tenta o NordPass primeiro e o cofre do sistema depois.

### Rota A: NordPass

1. No app do NordPass, crie um item do tipo senha. Use um título claro, por exemplo `ADO_PAT_<ORG>`.
2. Cole o PAT no campo de senha.
3. Registre só a referência, nunca o PAT. Use um dos dois caminhos:
   - No chat, peça ao assistente. Ele chama `ado_setup_org(auth="pat", pat_ref={nordpass_title, nordpass_folder}, confirm=true)` depois do seu sim. O `nordpass_folder` é opcional.
   - No terminal: `uv run --python 3.12 --no-project <pasta-do-plugin>/server/ado_cli.py pat-set --org <org> --nordpass-title <título> [--nordpass-folder <pasta>]`.
4. Os dois caminhos gravam a referência na configuração local. A diferença está em quando a organização passa para `auth=pat`:
   - `pat-set` lê o PAT, chama o Azure DevOps e só muda a organização para `auth=pat` depois de `http=200`. Se a conferência falhar, a organização continua no modo anterior.
   - `ado_setup_org` muda para `auth=pat` na hora, sem conferir. Rode `pat-check` logo depois (passo 3).
5. Se o NordPass estiver travado, rode `np unlock` no terminal e repita.

### Rota B: cofre do sistema

Rode no seu terminal:

```text
uv run --python 3.12 --no-project <pasta-do-plugin>/server/ado_cli.py pat-set --org <org>
```

O comando pede o PAT com entrada oculta e grava no cofre:

| Sistema | Cofre | Identificação |
|---|---|---|
| macOS | Keychain | serviço `comit-azure-devops`, conta `<org>` |
| Windows | Credential Manager | alvo `comit-azure-devops/<org>` |
| Linux | `secret-tool` | serviço `comit-azure-devops`, conta `<org>` |

O comando relê o valor e confere a gravação. Depois chama o Azure DevOps com o PAT. Se der `http=200`, ele muda a organização para `auth=pat`. Ele não imprime o PAT.

## 3. Confira

```text
uv run --python 3.12 --no-project <pasta-do-plugin>/server/ado_cli.py pat-check --org <org>
```

O resultado esperado:

```text
fonte=nordpass|keychain|credman|secret-tool
encontrado=sim
http=200
```

O comando nunca mostra o valor nem o tamanho do PAT.

## Se o PAT vazar

Se você colar o PAT no chat, ele ficou no histórico da conversa. Revogue o token em `https://dev.azure.com/<org>/_usersSettings/tokens` e crie outro.

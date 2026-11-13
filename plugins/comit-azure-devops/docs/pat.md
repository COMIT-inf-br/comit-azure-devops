# PAT de contingência

Use um PAT (token de acesso pessoal) só se a organização não aceitar o login do Azure CLI. O caminho padrão é o `az login`.

Regras que valem sempre:

- Grave o PAT você mesmo, no seu terminal, com entrada oculta.
- Não cole o PAT no chat.
- Não guarde o PAT em variável de ambiente nem em arquivo.
- O servidor lê o PAT sob demanda e só o mantém na memória.

## 1. Crie o token

1. Abra `https://dev.azure.com/<org>/_usersSettings/tokens`.
2. Crie um token novo.
3. Em Scopes, escolha Custom defined.
4. Marque só **Work Items: Read & write**. Esse escopo cobre criar, comentar e anexar.
5. Marque **Project and Team: Read**. Sem ele, a lista de equipes e membros dá erro 401.
6. Escolha a menor validade que a organização permitir.

Não marque Full access, Code, Wiki nem escopos de administração. Read sozinho dá erro 401 ao criar, comentar ou anexar.

## 2. Guarde o token

Escolha uma das duas rotas. O servidor tenta o NordPass primeiro e o cofre do sistema depois.

### Rota A: NordPass

1. No app do NordPass, crie um item do tipo senha. Use um título claro, por exemplo `ADO_PAT_<ORG>`.
2. Cole o PAT no campo de senha.
3. Informe só o título ao assistente. Ele registra o título na configuração local. O PAT não passa pelo chat.
4. Se o NordPass estiver travado, rode `np unlock` no terminal e repita.

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

O comando relê o valor e confere a gravação. Ele não imprime o PAT.

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

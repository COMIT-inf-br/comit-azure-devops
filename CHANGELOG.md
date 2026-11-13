# Changelog

## 0.2.0 (não publicado)

- Leituras do time: `ado_backlog` (itens sem sprint, nas áreas do time, pela prioridade do backlog) e `ado_sprint_items` (itens da sprint com totais de esforço por pessoa).
- `ado_prepare_move_to_sprint`: move itens para uma sprint ou de volta ao backlog. Leva as filhas não concluídas por padrão.
- `ado_prepare_create_sprint`: cria a próxima sprint pelo padrão do time e a associa ao time. Se a iteração já existe no projeto, só associa.
- `ado_prepare_sprint_rollover`: virada de sprint. Leva o que não está concluído para a próxima e cria a próxima se faltar.
- `ado_prepare_delete`: exclui para a lixeira, com os filhos só quando pedido. Nunca exclui em definitivo.
- `ado_prepare_comments_batch`: vários comentários numa prévia e um OK.
- Operações em lote: um pedido por item, relato parcial, reconciliação por passo e retry sem duplicar. HTTP 403 vira mensagem de permissão.
- Skills novas `ado-sprint` e `ado-daily`. `ado-consultar` resume comentários; `ado-comentar` aponta o lote de comentários.

### Planejado

- Indicar pessoas a procurar sobre um item, com base nos comentários, nas entregas e nas datas prometidas.

## 0.1.0 (não publicado)

- Servidor MCP local em Python (biblioteca padrão), iniciado por `uv`.
- Login pelo Azure CLI. PAT só como contingência, lido do NordPass ou do cofre do sistema.
- Skills por intenção: `azure-devops`, `ado-configurar`, `ado-consultar`, `ado-criar`, `ado-atualizar`, `ado-comentar`, `ado-anexar`.
- Escrita em duas fases: prévia completa, OK explícito e `ado_apply`. A criação valida com `validateOnly`.
- Perfis embutidos de exemplo e descoberta automática de organização nova.
- Documentação: setup, PAT de contingência e perfil. Seção para testar o plugin localmente antes de instalar.
- Evals de disparo por intenção (`claude plugin eval`) com mocks gerados do servidor, em `evals/` e `evals-sem-mcp/`.
- ADO falso dos testes serve as rotas de descoberta de perfil.

## 0.0.1 (não publicado)

- Esqueleto do repositório, manifests e servidor MCP de teste (`hello`).

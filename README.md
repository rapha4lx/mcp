# SessionDB MCP

Servidor MCP em Python para expor acesso controlado e baseado em sessão a bancos de dados SQL (Postgres, MySQL, SQLite, etc.) como tools para agentes.

## Project status and support

Este projeto está sendo preparado para uso open source. Issues e pull requests são bem-vindos, mas mudanças grandes devem começar por uma issue para alinhar escopo, segurança e compatibilidade.

## O que ele expõe

- `create_session`: cria um token configurando a URI do banco dinamicamente e definindo flags de permissões (`allow_read`, `allow_insert`, etc.)
- `list_sessions`: lista sessões ativas; por padrão os tokens retornam mascarados
- `revoke_session`: revoga um token manualmente
- `get_session_info`: mostra os detalhes de uma sessão ativa; por padrão o token também retorna mascarado
- `list_config_databases`: lê `mcp-config.json` e lista bancos disponíveis
- `connect_to_config_database`: cria uma sessão usando um banco definido em `mcp-config.json`
- `list_tables`: lista tabelas de um schema
- `list_views`: lista views de um schema
- `list_functions`: lista functions de um schema
- `list_referenced_tables`: lista tabelas referenciadas por uma tabela
- `list_referencing_tables`: lista tabelas que referenciam uma tabela
- `list_related_tables`: versão rápida, só com nomes das tabelas relacionadas
- `list_related_tables_detailed`: versão detalhada, com colunas e constraints
- `describe_table`: descreve colunas de uma tabela/view
- `query`: executa queries genéricas (SELECT, INSERT, UPDATE, etc.), respeitando as flags definidas em `create_session`

## Quick start

### 1. Instalação local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edite `.env` conforme necessário:

```bash
PG_SCHEMA=public
PG_MAX_ROWS=200
PG_STATEMENT_TIMEOUT_MS=10000
PG_SESSION_TTL_HOURS=24
SQL_MCP_AUTO_INSTALL_DRIVERS=false
SQL_MCP_EXPOSE_SESSION_TOKENS=false
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=3005
```

### 2. Subir com Docker Compose

```bash
cp .env.example .env
docker compose up --build -d
```

### 3. Teste rápido

Se o cliente aceitar MCP remoto via HTTP, use:

```text
http://localhost:3005/mcp
```

Um teste simples para verificar se a porta HTTP subiu:

```bash
curl -i http://localhost:3005/mcp
```

## Instalação de drivers de banco

O caminho recomendado é instalar os drivers explicitamente via extras do pacote, em vez de depender de instalação dinâmica em runtime.

Exemplos:

```bash
pip install -e .[postgres]
pip install -e .[postgres-psycopg2]
pip install -e .[mysql]
pip install -e .[mssql]
pip install -e .[all]
```

Extras disponíveis:

- `postgres`: instala `psycopg[binary]`
- `postgres-psycopg2`: instala `psycopg2-binary`
- `mysql`: instala `pymysql`
- `mssql`: instala `pyodbc`
- `all`: instala todos os drivers opcionais mapeados no projeto

## Instalação automática de drivers em runtime

O servidor consegue tentar instalar alguns drivers ausentes em runtime com `pip`, **mas esse comportamento vem desabilitado por padrão**.

Variável de ambiente:

```bash
SQL_MCP_AUTO_INSTALL_DRIVERS=false
```

Para habilitar explicitamente:

```bash
SQL_MCP_AUTO_INSTALL_DRIVERS=true
```

Quando a instalação automática estiver desabilitada e um driver estiver ausente, o servidor falhará com uma mensagem orientando qual pacote instalar manualmente.

### Quando usar

Use `SQL_MCP_AUTO_INSTALL_DRIVERS=true` apenas se você aceitar:

- mudanças dinâmicas no ambiente de execução
- uso de rede durante a execução do servidor
- menor previsibilidade operacional em comparação com dependências pré-instaladas

Para produção, CI e ambientes corporativos, prefira pré-instalar os drivers necessários com extras do pacote ou por gestão explícita de dependências.

## Arquivos de configuração incluídos no repositório

Arquivos rastreados pelo repositório:

- `.env.example`: exemplo de variáveis do servidor
- `mcp-config.example.json`: exemplo de configuração de bancos para o cliente
- `examples/vscode.mcp.json`: exemplo de configuração para VS Code

Arquivos que **você** deve criar localmente:

- `.env`: sua configuração local do servidor
- `mcp-config.json`: configuração local dos bancos usados pelo seu cliente/agente
- `.vscode/mcp.json`: opcional, se quiser usar configuração local do workspace no VS Code

## Configuração do cliente

O cliente não deve buscar credenciais em `.env`. O fluxo recomendado é ler um arquivo `mcp-config.json` na raiz do projeto que está sendo usado pelo chat.

Estrutura esperada:

```json
{
  "databases": [
    {
      "name": "financeiro",
      "database_url": "postgresql://usuario:senha@host:5432/financeiro_db",
      "description": "Banco financeiro com contas a pagar, faturamento e fluxo de caixa."
    },
    {
      "name": "crm",
      "database_url": "mysql+pymysql://usuario:senha@host:3306/crm_db",
      "description": "Banco de CRM com clientes, leads, interações e histórico comercial."
    }
  ]
}
```

O projeto inclui um exemplo em [mcp-config.example.json](./mcp-config.example.json).

### Fluxo recomendado para o chat

1. Ler `mcp-config.json` na raiz do projeto atual
2. Identificar quais bancos estão disponíveis em `databases`
3. Usar o campo `description` para inferir qual banco faz mais sentido para a pergunta do usuário
4. Chamar `create_session` com a `database_url` do banco escolhido
5. Guardar o `session_token` retornado em memória e reutilizar nas próximas tools
6. Se a pergunta envolver mais de um domínio, criar mais de uma sessão e consultar múltiplos bancos

## Executar localmente

```bash
source .venv/bin/activate
sessiondb-mcp
```

## Rodar testes localmente

```bash
source .venv/bin/activate
pip install -e .[test]
pytest -q
```

A pipeline do GitHub Actions executa esse mesmo fluxo básico em pushes para `main` e em pull requests.

## Executar com Docker

### Build manual

```bash
docker build -t sessiondb-mcp .
```

### Execução com `.env`

```bash
docker run --rm -i --env-file .env sessiondb-mcp
```

### Publicando a porta `3005`

```bash
docker run --rm -i -p 3005:3005 --env-file .env sessiondb-mcp
```

Se o banco estiver na sua máquina local e o container precisar acessá-lo, ajuste o host do `DATABASE_URL` para um endereço acessível do container. Em Linux, normalmente isso significa usar o IP da máquina na rede Docker em vez de `localhost`.

### Docker Compose

```bash
docker compose up --build -d
```

Para parar:

```bash
docker compose down
```

O `docker-compose.yml` foi mantido intencionalmente simples para funcionar em uma máquina limpa, sem exigir uma rede Docker externa criada previamente. As variáveis `SQL_MCP_AUTO_INSTALL_DRIVERS` e `SQL_MCP_EXPOSE_SESSION_TOKENS` também são repassadas para o container.

## Modos de transporte

O servidor suporta os seguintes modos:

- `streamable-http`: melhor opção para expor `http://localhost:3005/mcp`
- `stdio`: melhor opção quando a IDE executa o servidor como subprocesso local
- `both`: útil para cenários avançados em que você quer HTTP e stdio no mesmo processo

Exemplos:

```bash
MCP_TRANSPORT=streamable-http sessiondb-mcp
MCP_TRANSPORT=stdio sessiondb-mcp
MCP_TRANSPORT=both sessiondb-mcp
```

Se `MCP_TRANSPORT` não estiver definido e o servidor detectar execução como subprocesso de IDE, ele tenta usar `stdio`. Fora disso, o padrão é `streamable-http`.

## Timeout e escopo de schema

O comportamento de `statement_timeout_ms` e `schema` agora é explícito por backend.

### PostgreSQL

- `statement_timeout_ms` é aplicado na conexão com `SET statement_timeout`
- o schema ativo é aplicado com `SET search_path`
- `query()` também rejeita mudanças explícitas de `search_path` e referências cruzadas para outros schemas em cláusulas comuns como `FROM`, `JOIN`, `UPDATE`, `INTO`, `TABLE` e `TRUNCATE`

### MySQL

- o schema ativo é aplicado com `USE <schema>` na conexão
- `query()` rejeita tentativas explícitas de mudar o contexto com `USE ...`
- `statement_timeout_ms` é aceito na sessão, mas **não é aplicado automaticamente** no backend MySQL neste momento

### SQLite e outros backends

- não há garantia de enforcement de `statement_timeout_ms`
- o campo `schema` pode ser aceito como configuração lógica da sessão, mas não representa um sandbox completo nesses backends

### Importante

O servidor aplica guardrails de schema para o fluxo de sessão e rejeita referências cruzadas óbvias em `query()`, mas isso **não substitui** isolamento nativo do banco, permissões do usuário do banco ou um parser SQL completo.

## Modelo de segurança e confiança

Este servidor foi pensado principalmente para uso local ou em ambientes controlados por você. Ainda assim, o comportamento de exposição e descoberta agora é mais conservador.

### Metadata tools exigem `allow_read`

As ferramentas abaixo agora exigem `allow_read=true` na sessão ativa:

- `list_tables`
- `list_views`
- `list_functions`
- `list_referenced_tables`
- `list_referencing_tables`
- `list_related_tables`
- `list_related_tables_detailed`
- `describe_table`

Isso alinha a descoberta de metadata com a mesma expectativa de permissão já usada em consultas de leitura.

### Exposição de tokens de sessão

Por padrão:

- `create_session` retorna o token recém-criado, porque o cliente precisa dele
- `list_sessions` retorna sessões com token mascarado
- `get_session_info` retorna detalhes da sessão com token mascarado

Se você realmente quiser expor tokens em ferramentas de inspeção para debugging local, habilite:

```bash
SQL_MCP_EXPOSE_SESSION_TOKENS=true
```

Mesmo com essa variável habilitada, `list_sessions` só expõe tokens quando chamada com `include_tokens=true`, e `get_session_info` só expõe o token quando chamada com `include_token=true`.

### Recomendação operacional

- use usuários de banco com permissões mínimas
- trate o token de sessão como segredo transitório
- não exponha esse servidor diretamente na internet sem uma camada adicional de autenticação, autorização e rede
- prefira deixar `SQL_MCP_EXPOSE_SESSION_TOKENS=false` fora de ambientes de desenvolvimento local

## Registrar no Cursor ou Antigravity

Você pode conectar suas IDEs ao servidor de duas formas: via Python local ou via Docker.

### 1. Conectar ao container Docker em execução

Se você já subiu o container com `docker compose up -d`, pode fazer com que várias IDEs se conectem ao mesmo container.

#### Cursor

1. Vá em **Settings** > **Cursor Settings** > **Features** > **MCP**
2. Clique em **+ Add New MCP Server**
3. **Name**: `SessionDB-Active`
4. **Type**: `command`
5. **Command**:
   ```bash
   docker exec -i sessiondb-mcp sessiondb-mcp
   ```

#### Antigravity

```json
{
  "mcpServers": {
    "sessiondb-shared": {
      "command": "docker",
      "args": ["exec", "-i", "sessiondb-mcp", "sessiondb-mcp"]
    }
  }
}
```

### 2. Rodar via interpretador Python local

#### Cursor

1. Vá em **Features** > **MCP** > **+ Add New MCP Server**
2. **Name**: `SessionDB-Local`
3. **Type**: `command`
4. **Command**:
   ```bash
   /caminho/absoluto/do/projeto/.venv/bin/sessiondb-mcp
   ```

#### Antigravity

```json
{
  "mcpServers": {
    "sessiondb-local": {
      "command": "/caminho/para/seu/.venv/bin/sessiondb-mcp"
    }
  }
}
```

## Registrar no VS Code

O VS Code usa o arquivo `.vscode/mcp.json` no workspace. O repositório inclui um exemplo em [`examples/vscode.mcp.json`](./examples/vscode.mcp.json).

Exemplo:

```json
{
  "servers": {
    "sessiondb_mcp": {
      "type": "http",
      "url": "http://localhost:3005/mcp"
    }
  }
}
```

Para usar no VS Code:

1. Copie `examples/vscode.mcp.json` para `.vscode/mcp.json`
2. Suba o servidor com `docker compose up --build -d`
3. Abra a pasta do projeto no VS Code
4. Rode `MCP: List Servers` na Command Palette e confirme o trust do servidor

## Ferramentas de descoberta

Para facilitar o uso em IDEs onde o agente não lê arquivos locais automaticamente, o projeto inclui:

1. `list_config_databases`: lê o arquivo `mcp-config.json` da raiz do projeto e lista os bancos disponíveis
2. `connect_to_config_database`: cria uma sessão automaticamente usando o nome do banco encontrado no `mcp-config.json`

### Fluxo sugerido para agentes

1. Rodar `list_config_databases`
2. O usuário/agente escolhe o banco
3. Rodar `connect_to_config_database(name="nome_do_banco")`
4. Usar o `session_token` retornado para as demais operações

## Fluxo recomendado

1. Chame `create_session` com `database_url` do banco desejado
2. Guarde o `session.token` retornado
3. Use `session_token` nas chamadas de metadata e query
4. Se precisar trocar de banco, crie outra sessão com outro token

Exemplo de criação de sessão:

```json
{
  "database_url": "postgresql://usuario:senha@host:5432/financeiro_db",
  "schema": "public",
  "statement_timeout_ms": 5000,
  "max_rows": 100,
  "label": "financeiro",
  "allow_read": true,
  "allow_insert": true,
  "allow_update": false,
  "allow_delete": false,
  "allow_create": false,
  "allow_drop": false
}
```

Resposta esperada:

```json
{
  "ok": true,
  "session": {
    "token": "abc123",
    "label": "financeiro",
    "schema": "public",
    "statement_timeout_ms": 5000,
    "max_rows": 100,
    "created_at": "2026-04-13T18:00:00+00:00",
    "expires_at": "2026-04-14T18:00:00+00:00"
  },
  "database_name": "promo_db",
  "current_user": "promo_user"
}
```

Exemplo de query:

```json
{
  "session_token": "abc123",
  "sql": "select id, email from customers where created_at >= %s order by created_at desc",
  "params_json": "[\"2026-01-01\"]",
  "max_rows": 50
}
```

## Múltiplos bancos

O servidor pode manter várias sessões ao mesmo tempo. Exemplo:

- token `financeiro` apontando para o banco A
- token `crm` apontando para o banco B

Para consultar mais de um banco na mesma conversa:

1. leia `mcp-config.json`
2. crie uma sessão para cada banco necessário com `create_session`
3. mantenha os tokens em memória no cliente
4. envie o token correto em cada tool call
5. para comparar dados entre bancos, faça duas chamadas separadas e consolide o resultado no cliente

As tools de leitura aceitam `session_token`:

- `list_tables`
- `list_views`
- `list_functions`
- `list_referenced_tables`
- `list_referencing_tables`
- `list_related_tables`
- `list_related_tables_detailed`
- `describe_table`
- `query`

`database_url` deve ser usado apenas em `create_session`. Depois disso, o fluxo correto é usar `session_token` nas demais tools.

## Limitações e segurança

- O modelo agora tem recursos de controle de acesso (RBAC). Um token de sessão herda os booleans definidos durante a invocação da tool `create_session` (ex: `allow_delete`).
- Por padrão as flags de modificação e DDL sempre começam como `false`, exigindo passe explícito.
- Se o LLM alucinar uma exclusão, ele só funcionará se o script Python tiver criado a sessão com essa flag. Ele lança rejections instantâneas sem bater no banco caso violadas.
- O resultado é truncado no limite de linhas configurado.
- Em caso de erro em uma tool, a resposta retorna `ok: false` com `error` e `error_type` para o requisitante.
- As sessões ficam em memória do processo. Se o servidor reiniciar, os tokens são perdidos.
- A instalação automática de drivers pode alterar o ambiente do processo em runtime se `SQL_MCP_AUTO_INSTALL_DRIVERS=true` estiver habilitado.

> [!CAUTION]
> Dando os comandos ao LLM e o acesso a flags `allow_delete` ou `allow_drop`, ele pode de fato resetar infraestruturas inteiras no provedor em caso de problemas não supervisionados. Esteja ciente ao permitir conexões master ou conceder essas flags globais que alteram o escopo.

## License and contributions

Este projeto está licenciado sob a licença MIT. Veja [`LICENSE`](./LICENSE).

Ao contribuir, você concorda que suas contribuições serão distribuídas sob a mesma licença do projeto. Para mudanças maiores, abra uma issue antes de começar a implementação para alinhar escopo e impacto.

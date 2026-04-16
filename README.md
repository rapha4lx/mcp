# SQL MCP Server

Servidor MCP em Python para expor consultas seguras de leitura a bancos de dados SQL (Postgres, MySQL, SQLite, etc.) como tools para agentes.

## O que ele expõe

- `create_session`: valida um banco, cria um token temporário e guarda a conexão por 1 dia
- `list_sessions`: lista tokens ativos em memória
- `revoke_session`: revoga um token manualmente
- `list_tables`: lista tabelas e views de um schema
- `list_views`: lista views de um schema
- `list_functions`: lista functions de um schema
- `list_referenced_tables`: lista tabelas referenciadas por uma tabela
- `list_referencing_tables`: lista tabelas que referenciam uma tabela
- `list_related_tables`: versão rápida, só com nomes das tabelas relacionadas
- `list_related_tables_detailed`: versão detalhada, com colunas e constraints
- `describe_table`: descreve colunas de uma tabela/view
- `query`: executa apenas `SELECT`, `WITH` e `SHOW`

## Requisitos

- Python 3.11+
- Acesso a banco(s) SQL (Drivers nativos, como `psycopg` para Postgres ou `pymysql` para MySQL, são baixados automaticamente em runtime!)
- Cliente MCP compatível, como Codex/Copilot com suporte a MCP

## Configuração do Servidor

Crie um ambiente virtual e instale as dependências:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

O projeto já inclui um arquivo `.env` base na raiz. Ajuste os valores do servidor conforme necessário:

```bash
PG_SCHEMA='public'
PG_MAX_ROWS='200'
PG_STATEMENT_TIMEOUT_MS='10000'
PG_SESSION_TTL_HOURS='24'
MCP_TRANSPORT='streamable-http'
MCP_HOST='0.0.0.0'
MCP_PORT='3005'
```

O servidor carrega automaticamente esse `.env`. Se você também definir variáveis no ambiente do processo, elas continuam valendo como override.

`DATABASE_URL` é opcional. O projeto suporta dois modos:

- modo fixo: `DATABASE_URL` fica no ambiente do servidor e todas as requisições usam essa conexão
- modo dinâmico: o servidor sobe sem `DATABASE_URL` e o cliente cria sessões temporárias por token

Na inicialização, a `main` valida a conexão com o banco antes de expor o MCP somente quando `DATABASE_URL` estiver configurado. Se o servidor estiver em modo dinâmico, ele sobe sem validar banco no startup.

## Configuração do Cliente

O cliente não deve mais buscar credenciais em `.env`. O fluxo recomendado é ler um arquivo `mcp-config.json` na raiz do projeto que está sendo usado pelo chat.

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

Regras de uso para o chat:

1. ler `mcp-config.json` na raiz do projeto atual
2. identificar quais bancos estão disponíveis em `databases`
3. usar o campo `description` para inferir qual banco faz mais sentido para a pergunta do usuário
4. chamar `create_session` com a `database_url` do banco escolhido
5. guardar o `session_token` retornado em memória e reutilizar nas próximas tools
6. se a pergunta envolver mais de um domínio, criar mais de uma sessão e consultar múltiplos bancos

## Executar localmente

```bash
source .venv/bin/activate
sql-mcp-server
```

## Executar com Docker

Build da imagem:

```bash
docker build -t sql-mcp-server .
```

Execução usando o `.env` local:

```bash
docker run --rm -i --env-file .env sql-mcp-server
```

Publicando a porta `3005`:

```bash
docker run --rm -i -p 3005:3005 --env-file .env sql-mcp-server
```

Se o banco estiver na sua máquina local e o container precisar acessá-lo, ajuste o host do `DATABASE_URL` para um endereço acessível do container. Em Linux, normalmente isso significa usar o IP da máquina na rede Docker em vez de `localhost`.

Com Docker Compose:

```bash
docker compose -f docker-compose.yml run --rm sql-mcp-server
```

Para subir o serviço na porta `3005`:

```bash
docker compose -f docker-compose.yml up --build -d
```

O `docker-compose.yml` agora sobe o MCP em modo dinâmico por padrão, sem injetar `DATABASE_URL` no container. Isso evita falha no startup quando a conexão fixa não está disponível e deixa o fluxo centrado em `create_session`.

## Registrar no cliente MCP

Exemplo de configuração em `config.toml`:

```toml
[mcp_servers.sql_tools]
command = "/caminho/para/o/projeto/.venv/bin/sql-mcp-server"
```

Se preferir, você pode chamar o binário Python diretamente:

```toml
[mcp_servers.sql_tools]
command = "/caminho/para/o/projeto/.venv/bin/python"
args = ["-m", "sql_mcp_server.server"]
```

Ou registrar via Docker:

```toml
[mcp_servers.sql_tools]
command = "docker"
args = ["run", "--rm", "-i", "--env-file", ".env", "sql-mcp-server"]
```

Se o cliente aceitar MCP remoto via HTTP, use `http://localhost:3005/mcp`.

## Registrar no VS Code

O VS Code usa o arquivo `.vscode/mcp.json` no workspace. Este projeto já inclui:

```json
{
  "servers": {
    "sql_tools": {
      "type": "http",
      "url": "http://localhost:3005/mcp"
    }
  }
}
```

Para funcionar no VS Code:

1. Suba o servidor com `docker compose -f docker-compose.yml up --build -d`
2. Abra a pasta do projeto no VS Code
3. Rode `MCP: List Servers` na Command Palette e confirme o trust do servidor

## Fluxo recomendado

1. Chame `create_session` com `database_url` do banco desejado.
2. Guarde o `session.token` retornado.
3. Use `session_token` nas chamadas de metadata e query.
4. Se precisar trocar de banco, crie outra sessão com outro token.

Exemplo de criação de sessão:

```json
{
  "database_url": "postgresql://usuario:senha@host:5432/financeiro_db",
  "schema": "public",
  "statement_timeout_ms": 5000,
  "max_rows": 100,
  "label": "financeiro"
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

- O servidor rejeita comandos que não sejam de leitura.
- Apenas uma instrução SQL por chamada é aceita.
- A conexão é aberta com transação read-only.
- `statement_timeout` e `max_rows` ficam associados à sessão.
- O resultado é truncado no limite de linhas configurado.
- Em caso de erro em uma tool, a resposta retorna `ok: false` com `error` e `error_type` para o requisitante.
- As sessões ficam em memória do processo. Se o servidor reiniciar, os tokens são perdidos.
- O token evita reenviar a senha a cada chamada, mas a credencial original ainda trafega em `create_session`. Trate essa operação como sensível.
- Para produção, o modelo mais seguro continua sendo trocar login por um token emitido pelo seu backend, em vez de expor `database_url` diretamente ao MCP.
- O arquivo `mcp-config.json` contém credenciais de banco e deve ser tratado como sensível. Não publique esse arquivo em repositórios públicos.

## Próximo passo

Se você quiser expor operações de escrita, faça isso em tools separadas, com validações explícitas por ação, e nunca via SQL arbitrário.

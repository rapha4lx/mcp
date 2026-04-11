# Postgres MCP Tool

Servidor MCP em Python para expor consultas seguras de leitura ao PostgreSQL como tools para agentes.

## O que ele expõe

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
- Acesso ao banco Postgres
- Cliente MCP compatível, como Codex/Copilot com suporte a MCP

## Configuração

Crie um ambiente virtual e instale as dependências:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

O projeto já inclui um arquivo `.env` base na raiz. Ajuste os valores conforme seu banco:

```bash
DATABASE_URL='postgresql://usuario:senha@host:5432/seu_banco'
PG_SCHEMA='public'
PG_MAX_ROWS='200'
PG_STATEMENT_TIMEOUT_MS='10000'
MCP_TRANSPORT='streamable-http'
MCP_HOST='0.0.0.0'
MCP_PORT='3005'
```

O servidor carrega automaticamente esse `.env`. Se você também definir variáveis no ambiente do processo, elas continuam valendo como override.

Na inicialização, a `main` valida a conexão com o banco antes de expor o MCP. Se o Postgres não estiver acessível, o processo encerra com erro imediatamente.

## Executar localmente

```bash
source .venv/bin/activate
postgres-mcp-tool
```

## Executar com Docker

Build da imagem:

```bash
docker build -t postgres-mcp-tool /home/rferro/mcp
```

Execução usando o `.env` local:

```bash
docker run --rm -i --env-file /home/rferro/mcp/.env postgres-mcp-tool
```

Publicando a porta `3005`:

```bash
docker run --rm -i -p 3005:3005 --env-file /home/rferro/mcp/.env postgres-mcp-tool
```

Se o banco estiver na sua máquina local e o container precisar acessá-lo, ajuste o host do `DATABASE_URL` para um endereço acessível do container. Em Linux, normalmente isso significa usar o IP da máquina na rede Docker em vez de `localhost`.

Com Docker Compose:

```bash
docker compose -f /home/rferro/mcp/docker-compose.yml run --rm postgres-mcp-tool
```

Para subir o serviço na porta `3005`:

```bash
docker compose -f /home/rferro/mcp/docker-compose.yml up --build -d
```

Antes do primeiro `up`, crie a rede externa compartilhada:

```bash
docker network create mcp-shared-net
```

O `docker-compose.yml` atual sobe apenas o MCP e conecta o container na rede externa `mcp-shared-net`.

- `DATABASE_URL` continua sendo usado para execução local fora do Docker
- `DATABASE_URL_DOCKER` é usado pelo Compose e aponta para `host.docker.internal:5432`
- o endpoint MCP continua em `http://localhost:3005/mcp`

Dentro do Compose, o MCP se conecta ao banco usando `host.docker.internal`, mapeado para o host Docker com `extra_hosts`. Isso evita o problema de `localhost` dentro do container apontar para o próprio container do MCP.

Se você quiser manter comunicação por rede compartilhada entre os dois projetos, ainda pode conectar os dois na mesma rede externa. Mas, como o Postgres já está publicado em `5432`, isso deixa de ser obrigatório.

Exemplo opcional no outro `docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: promo_db
      POSTGRES_USER: promo_user
      POSTGRES_PASSWORD: promo_password
    networks:
      mcp-shared-net:
        aliases:
          - postgres-db

networks:
  mcp-shared-net:
    name: mcp-shared-net
    external: true
```

No `docker-compose.yml`, o código do servidor é montado por volume a partir de `./src`. Isso evita rebuild da imagem a cada alteração no MCP. Você só precisa rebuildar quando mudar dependências do Python ou a base da imagem.

Nesse compose, o arquivo `.env` é:

- carregado como variáveis de ambiente com `env_file`
- montado dentro do container em `/app/.env`
- o código em `src/` é montado dentro do container e executado com `PYTHONPATH=/app/src`

Com a configuração atual, o endpoint MCP HTTP fica em `http://localhost:3005/mcp`.

## Registrar no cliente MCP

Exemplo de configuração em `config.toml`:

```toml
[mcp_servers.postgres]
command = "/home/rferro/mcp/.venv/bin/postgres-mcp-tool"
```

Se preferir, você pode chamar o binário Python diretamente:

```toml
[mcp_servers.postgres]
command = "/home/rferro/mcp/.venv/bin/python"
args = ["-m", "postgres_mcp_server.server"]
```

Ou registrar via Docker:

```toml
[mcp_servers.postgres]
command = "docker"
args = ["run", "--rm", "-i", "--env-file", "/home/rferro/mcp/.env", "postgres-mcp-tool"]
```

Se o cliente aceitar MCP remoto via HTTP, use `http://localhost:3005/mcp`.

## Registrar no VS Code

O VS Code usa o arquivo `.vscode/mcp.json` no workspace. Este projeto já inclui:

```json
{
  "servers": {
    "postgres": {
      "type": "http",
      "url": "http://localhost:3005/mcp"
    }
  }
}
```

Para funcionar no VS Code:

1. Suba o servidor com `docker compose -f /home/rferro/mcp/docker-compose.yml up --build -d`
2. Abra a pasta `/home/rferro/mcp` no VS Code
3. Rode `MCP: List Servers` na Command Palette e confirme o trust do servidor

Se o VS Code não detectar automaticamente, abra `MCP: Open Workspace Folder MCP Configuration` e verifique o arquivo [mcp.json](/home/rferro/mcp/.vscode/mcp.json).

## Uso esperado pelos agentes

Fluxo recomendado:

1. Chamar `list_tables` para descobrir a estrutura disponível.
2. Chamar `describe_table` para entender as colunas.
3. Chamar `query` com SQL somente leitura e parâmetros em JSON.

Exemplo:

```json
{
  "sql": "select id, email from customers where created_at >= %s order by created_at desc",
  "params_json": "[\"2026-01-01\"]",
  "max_rows": 50
}
```

## Limitações e segurança

- O servidor rejeita comandos que não sejam de leitura.
- Apenas uma instrução SQL por chamada é aceita.
- A conexão é aberta com transação read-only.
- `statement_timeout` é configurado por ambiente.
- O resultado é truncado no limite de linhas configurado.

## Próximo passo

Se você quiser expor operações de escrita, faça isso em tools separadas, com validações explícitas por ação, e nunca via SQL arbitrário.

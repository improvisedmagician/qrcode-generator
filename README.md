# Plataforma SaaS de QR Codes Dinâmicos de Alta Disponibilidade

Plataforma corporativa para geração, personalização e gerenciamento de QR Codes Dinâmicos, desenhada para resistir a picos extremos de tráfego com garantia de alta disponibilidade e latência de redirecionamento inferior a 100ms.

---

## Estrutura do Projeto

```
.
├── docker-compose.yml              # Ambiente completo com Postgres, Redis, ClickHouse, Redirect, Worker e API
├── db/
│   ├── postgres/001_init.sql       # Schema relacional (Tenancy, RBAC, QR Codes, Regras, Outbox)
│   └── clickhouse/001_scans.sql    # Schema colunar (ReplacingMergeTree + Materialized Views)
├── services/
│   ├── redirect/                   # HOT PATH: Serviço stateless de redirecionamento (< 5ms)
│   │   ├── app/
│   │   │   ├── main.py             # Endpoint /{slug} com 302, 404/410 e deep linking
│   │   │   ├── resolver.py         # Cache multicamada (L1 -> Redis -> PG -> STALE) + Singleflight
│   │   │   ├── rules.py            # Motor de agendamento temporal, SO e deep links
│   │   │   └── capture.py          # Enfileiramento não bloqueante + Spool local em disco
│   │   └── tests/                  # 19 testes automatizados de resiliência e concorrência
│   │
│   ├── analytics_worker/           # DATA PLANE: Worker assíncrono de enriquecimento e métricas
│   │   ├── worker/
│   │   │   ├── main.py             # Leitor de Redis Streams (consumer groups, XAUTOCLAIM, DLQ)
│   │   │   └── enrich.py           # GeoIP, detecção de bots e anonimização LGPD (hash saltado)
│   │   └── tests/                  # 4 testes automatizados de classificação e integridade
│   │
│   └── api/                        # CONTROL PLANE: API REST do Dashboard Corporativo
│       ├── app/
│       │   ├── main.py             # FastAPI App com CORS e Lifespan
│       │   ├── outbox.py           # Outbox Relay loop para sincronização Postgres -> Redis
│       │   ├── qr_render.py        # Renderizador vetorial SVG, PNG (300 DPI) e PDF com ECL H automático
│       │   ├── security.py         # Argon2id + JWT curto com refresh rotativo
│       │   ├── deps.py             # RBAC por Workspace com Row-Level Security (RLS)
│       │   ├── routers/            # Auth, QR Codes, Analytics e Workspaces/Domínios
│       │   └── seed.py             # Seeder com usuário demo inicial
│       └── tests/                  # 9 testes automatizados de renderização e segurança
└── docs/
    └── architecture.md             # Documento de arquitetura detalhado
```

---

## Como Executar Localmente

### Pré-requisitos
- Docker & Docker Compose
- Python 3.12+ (caso deseje rodar testes locais)

### 1. Subindo a infraestrutura completa via Docker Compose

```bash
docker compose up --build -d
```

Serviços iniciados:
- **API do Dashboard:** `http://localhost:8000` (Docs interativos: `http://localhost:8000/docs`)
- **Serviço de Redirecionamento (Hot Path):** `http://localhost:8080/{slug}`
- **PostgreSQL:** `localhost:5432`
- **Redis:** `localhost:6379`
- **ClickHouse:** `localhost:8123`

### 2. Rodando os testes automatizados

```bash
# Rodar todos os 32 testes do ecossistema:
.\.venv\Scripts\pytest services/redirect -c services/redirect/pytest.ini --rootdir services/redirect
.\.venv\Scripts\pytest services/analytics_worker -c services/analytics_worker/pytest.ini --rootdir services/analytics_worker
.\.venv\Scripts\pytest services/api -c services/api/pytest.ini --rootdir services/api
```

---

## Guia Rápido de Uso da API

### 1. Autenticação (Login com usuário demo)
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@empresa.com", "password": "senha123"}'
```
Retorna `access_token` e a lista de workspaces com seus papéis.

### 2. Criar um QR Code Dinâmico com Agendamento e Regras de App
```bash
curl -X POST http://localhost:8000/workspaces/00000000-0000-0000-0000-000000000003/qrcodes \
  -H "Authorization: Bearer <SEU_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Campanha Black Friday 2026",
    "default_url": "https://minhaloja.com.br/produtos",
    "slug": "blackfriday",
    "tags": ["marketing", "promo"],
    "design": {
      "foreground": "#0F172A",
      "background": "#FFFFFF",
      "module_style": "rounded",
      "finder_style": "rounded",
      "frame_text": "ESCANEIE AQUI"
    },
    "rules": [
      {
        "priority": 10,
        "starts_at": "2026-11-27T00:00:00Z",
        "ends_at": "2026-11-30T23:59:59Z",
        "target_url": "https://minhaloja.com.br/blackfriday"
      },
      {
        "priority": 20,
        "os": ["ios"],
        "target_url": "https://instagram.com/minhaloja",
        "deep_link": {
          "ios": "instagram://user?username=minhaloja"
        }
      }
    ]
  }'
```

### 3. Testando o Redirecionamento em Alta Velocidade
```bash
# Redirecionamento padrão (Retorna 302 com destino e registra métricas assincronamente)
curl -I http://localhost:8080/blackfriday

# Simulando acesso de iPhone (Retorna página intermediária para abrir o App nativo)
curl -A "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" http://localhost:8080/blackfriday
```

### 4. Exportando o QR Code em Alta Resolução (SVG / PNG / PDF)
```bash
# Exportar em Vetor SVG para impressão em outdoors/banners:
curl -H "Authorization: Bearer <SEU_TOKEN>" \
  "http://localhost:8000/workspaces/00000000-0000-0000-0000-000000000003/qrcodes/<QR_ID>/export?format=svg" \
  --output qr-code.svg

# Exportar em PNG de Alta Resolução (300 DPI) com verificação de leitura:
curl -H "Authorization: Bearer <SEU_TOKEN>" \
  "http://localhost:8000/workspaces/00000000-0000-0000-0000-000000000003/qrcodes/<QR_ID>/export?format=png&size=2048&verify=true" \
  --output qr-code.png
```

---

## Recursos Corporativos Implementados

- **Alta Disponibilidade Real:** 4 camadas de resolução (L1 em memória -> Redis L2 -> Réplica Postgres L3 -> Cópia STALE resiliente).
- **Spool de Contingência:** Se a rede ou fila caírem durante um pico, os eventos de scan são gravados localmente em disco e reenviados automaticamente na recuperação.
- **Agendamento Sem Cron:** Regras de data/hora são embutidas no payload em cache e avaliadas com relógio UTC no milissegundo do scan.
- **Correção de Erros Automática:** Se um logotipo for inserido, o nível de redundância é elevado para **Nível H (30%)** automaticamente, garantindo legibilidade perfeita.
- **Conformidade LGPD:** O IP bruto nunca é persistido em banco de dados; apenas o país/cidade e um hash criptográfico com salt são armazenados para cálculo de visitantes únicos.

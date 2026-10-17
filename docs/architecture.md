# Arquitetura da Plataforma SaaS de QR Codes Dinâmicos

## 1. Visão Geral e Princípios Fundamentais

A plataforma foi desenhada especificamente para atender cenários corporativos onde os links e QR Codes impressos em milhões de embalagens, cartazes, crachás e campanhas de mídia exterior (Out of Home - OOH) **não podem falhar ou ficar fora do ar sob hipótese alguma**.

### 1.1 Desacoplamento Crítico (Hot Path vs. Control Plane)
O sistema é estritamente dividido em três planos independentes:

1. **Hot Path (Serviço de Redirecionamento):**
   - Microsserviço sem estado (stateless), somente-leitura e ultrarrápido (tempo de processamento interno `< 5ms`).
   - Não depende do banco PostgreSQL primário nem do ClickHouse para servir os redirecionamentos.
   - Cache em 4 camadas com fallback automático para cópia *stale* caso todos os bancos caiam.

2. **Data Plane (Pipeline de Analytics Assíncrono):**
   - O redirecionamento nunca espera a persistência de métricas (`put_nowait` O(1) em fila em memória).
   - Ingestão em lotes via Redis Streams com persistência de contingência em disco (spool local JSONL).
   - Worker desacoplado enriquece metadados (GeoIP e User-Agent) e insere no ClickHouse em lotes com deduplicação nativa (*at-least-once* + *ReplacingMergeTree*).

3. **Control Plane (API REST do Dashboard):**
   - Autenticação corporativa com JWT curto, refresh rotativo e suporte a SSO (SAML/OIDC).
   - RBAC por Workspace com isolamento por *Row-Level Security* (RLS) no PostgreSQL.
   - *Transactional Outbox Pattern*: qualquer alteração de link é commitada no Postgres e propagada instantaneamente para o Redis e pods de redirecionamento via Redis Pub/Sub.

---

## 2. Diagramas de Arquitetura

### 2.1 Visão Geral dos Componentes

```
+-----------------------------------------------------------------------------------+
|                                  EDGE (Cloudflare)                                |
|  - WAF / DDoS Mitigation / Rate Limiting por IP                                   |
|  - TLS Termination Anycast (<20ms global)                                         |
|  - Cloudflare for SaaS (Gerenciamento automático de TLS para domínios de clientes) |
|  - Fallback Worker (Snapshot de contingência em Workers KV se a origem falhar)    |
+-----------------------------------------+-----------------------------------------+
                                          |
                        +-----------------+-----------------+
                        | HTTP 302                          | Painel Admin / API
                        v                                   v
+-----------------------------------------+       +---------------------------------+
|         REDIRECT SERVICE (Hot Path)     |       |          DASHBOARD API          |
|  - FastAPI / uvloop (Stateless)         |       |  - FastAPI (Auth / RBAC / CRUD) |
|  - Cache L1 (Memória local TTL 30s)     |       |  - Renderizador Vetorial (SVG)  |
|  - Cópia STALE resiliente               |       |  - Outbox Relay Loop            |
|  - Circuit Breakers & Singleflight      |       +----------------+----------------+
|  - Captura assíncrona com Spool local   |                        |
+--------+------------------+-------------+                        | Outbox Relay
         |                  |                                      | & Invalidação
         | (1) Cache L2     | (2) Fallback L3                      v
         v                  v                     +----------------+----------------+
+-----------------+  +--------------------+       |                                 |
|  REDIS CLUSTER  |  | POSTGRESQL HA      | <-----+           REDIS PUB/SUB         |
|  - L2 Cache     |  | - Read Replica (L3)|       |       Canal: 'qr:invalidate'    |
|  - Redis Streams|  | - Primário (Master)|       |  (Limpa L1 de todos os pods)    |
+--------+--------+  +--------------------+       +---------------------------------+
         |
         | Stream: 'scans'
         v
+-----------------------------------------+
|        ANALYTICS WORKER                 |
|  - Consumer Groups (XREADGROUP)         |
|  - Recuperação de órfãos (XAUTOCLAIM)   |
|  - GeoIP MaxMind & Parser de UA         |
|  - Anonimização LGPD (IP -> Salt Hash)  |
+--------------------+--------------------+
                     |
                     | Ingestão em lote
                     v
+-----------------------------------------+
|           CLICKHOUSE CLUSTER            |
|  - Tabela: scans (ReplacingMergeTree)   |
|  - Materialized View: scans_daily       |
+-----------------------------------------+
```

---

## 3. Modelo de Dados

### 3.1 PostgreSQL (Fonte da Verdade)
- `organizations`: Contas corporativas e configurações de SSO/SAML.
- `users`: Usuários, credenciais (Argon2id) e chaves de MFA.
- `workspaces`: Equipes/departamentos (tenancy com RLS).
- `roles` & `workspace_members`: RBAC com granularidade de permissões (`qr:read`, `qr:create`, `qr:update`, `qr:delete`, `analytics:read`, `domains:manage`).
- `domains`: Domínios corporativos personalizados com verificação via TXT/CNAME.
- `folders` & `tags`: Organização hierárquica e categorização.
- `qrcodes`: Entidade central com soft delete, controle de versão, agendamento de expiração e fallback.
- `qr_rules`: Regras prioritárias para agendamento temporal (`starts_at`/`ends_at`), direcionamento por Sistema Operacional (`ios`/`android`) e Deep Linking.
- `qr_destination_history`: Registro imutável de todas as alterações de destino (quem alterou e quando).
- `outbox_events`: Fila transacional com garantia de entrega para o Redis.
- `audit_logs`: Trilha de auditoria para conformidade corporativa.

### 3.2 ClickHouse (Analytics de Alto Volume)
- `analytics.scans`: Ingestão at-least-once com deduplicação por `event_id` através de `ReplacingMergeTree`. Não persiste IP bruto (conforme LGPD), apenas `visitor_hash` saltado.
- `analytics.scans_daily`: Materialized View com pré-agregações (`countState` e `uniqState`) para dashboards instantâneos mesmo sob bilhões de acessos.

---

## 4. Garantia de Redirecionamento em Menos de 100ms

1. **Edge Anycast Cloudflare:** Termina o aperto de mão TLS no ponto de presença (PoP) mais próximo do usuário, reduzindo a latência inicial de rede em até 70%.
2. **Cache L1 In-Process (Microssegundos):** Os links mais acessados ficam diretamente na memória RAM do processo Python em um `TTLCache` (30s) com lookup O(1) de ~0.01ms.
3. **Cache L2 Redis (Sub-milissegundos):** Caso não esteja no L1, busca no Redis via conexão assíncrona mantida em pool (latência típica `< 1ms`).
4. **Singleflight:** Impede *cache stampede*. Se 10.000 requisições simultâneas chegarem para um slug frio, apenas 1 consulta vai ao banco enquanto as outras 9.999 aguardam o mesmo resultado em memória.
5. **Cópia STALE de Contingência:** Se tanto o Redis quanto o PostgreSQL caírem simultaneamente, o serviço serve a última versão conhecida armazenada no cache `STALE`, garantindo que o usuário nunca receba erro 500.
6. **Métricas Não-Bloqueantes:** A captura de telemetria é despachada para uma fila assíncrona em memória com tempo de execução O(1).

-- =====================================================================
-- QR Dinâmico — Schema PostgreSQL (fonte da verdade)
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ===== Identidade e tenancy =====
CREATE TABLE organizations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  plan          text NOT NULL DEFAULT 'business',
  sso_enabled   boolean NOT NULL DEFAULT false,
  sso_config    jsonb,                          -- metadados SAML/OIDC (futuro SSO)
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id),
  email           citext NOT NULL UNIQUE,
  name            text,
  password_hash   text,                         -- NULL quando login apenas via SSO (argon2id)
  mfa_secret      text,                         -- criptografado via KMS
  external_id     text,                         -- id no IdP / SCIM
  is_active       boolean NOT NULL DEFAULT true,
  last_login_at   timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (                       -- equipe / departamento
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id),
  name            text NOT NULL,
  slug            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, slug)
);

-- ===== RBAC =====
CREATE TABLE roles (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid REFERENCES organizations(id), -- NULL = papel do sistema
  name            text NOT NULL,
  permissions     text[] NOT NULL,
  UNIQUE NULLS NOT DISTINCT (organization_id, name)
);

CREATE TABLE workspace_members (
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role_id      uuid NOT NULL REFERENCES roles(id),
  PRIMARY KEY (workspace_id, user_id)
);

-- ===== Domínios (plataforma + personalizados) =====
CREATE TABLE domains (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id    uuid REFERENCES organizations(id), -- NULL = domínio da plataforma
  hostname           citext NOT NULL UNIQUE,
  verification_token text NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'),
  status             text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','verified','active','failed')),
  tls_status         text,
  verified_at        timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- ===== Organização de QR Codes =====
CREATE TABLE folders (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  parent_id    uuid REFERENCES folders(id) ON DELETE CASCADE,
  name         text NOT NULL
);

CREATE TABLE tags (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name         text NOT NULL,
  color        text,
  UNIQUE (workspace_id, name)
);

-- ===== Core =====
CREATE TABLE qrcodes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id),
  domain_id     uuid NOT NULL REFERENCES domains(id),
  slug          text NOT NULL CHECK (slug ~ '^[A-Za-z0-9_-]{4,64}$'),
  name          text NOT NULL,
  folder_id     uuid REFERENCES folders(id) ON DELETE SET NULL,
  default_url   text NOT NULL,
  fallback_url  text,                           -- usado se pausado/expirado
  status        text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','paused','expired','archived')),
  expires_at    timestamptz,
  utm_params    jsonb,
  password_hash text,
  design        jsonb NOT NULL DEFAULT '{}',    -- cores, formato, moldura, logo_key
  version       integer NOT NULL DEFAULT 1,
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  UNIQUE (domain_id, slug)                      -- slug nunca é reutilizado (soft delete)
);
CREATE INDEX qrcodes_ws_idx     ON qrcodes (workspace_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX qrcodes_search_idx ON qrcodes USING gin (to_tsvector('portuguese', name));

CREATE TABLE qrcode_tags (
  qrcode_id uuid REFERENCES qrcodes(id) ON DELETE CASCADE,
  tag_id    uuid REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (qrcode_id, tag_id)
);

-- Regras de roteamento: agendamento, SO, país, deep link, A/B
CREATE TABLE qr_rules (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  qrcode_id   uuid NOT NULL REFERENCES qrcodes(id) ON DELETE CASCADE,
  priority    integer NOT NULL DEFAULT 100,     -- menor = avaliado primeiro
  starts_at   timestamptz,
  ends_at     timestamptz,
  os          text[],
  countries   text[],
  target_url  text NOT NULL,
  deep_link   jsonb,
  weight      integer,
  CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);
CREATE INDEX qr_rules_qr_idx ON qr_rules (qrcode_id, priority);

CREATE TABLE qr_destination_history (
  id          bigserial PRIMARY KEY,
  qrcode_id   uuid NOT NULL REFERENCES qrcodes(id),
  old_url     text,
  new_url     text NOT NULL,
  changed_by  uuid REFERENCES users(id),
  changed_at  timestamptz NOT NULL DEFAULT now()
);

-- ===== Plataforma =====
CREATE TABLE api_keys (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  prefix       text NOT NULL UNIQUE,
  key_hash     text NOT NULL,
  scopes       text[] NOT NULL,
  expires_at   timestamptz,
  revoked_at   timestamptz
);

CREATE TABLE audit_logs (
  id              bigserial PRIMARY KEY,
  organization_id uuid NOT NULL,
  actor_id        uuid,
  action          text NOT NULL,
  entity_type     text NOT NULL,
  entity_id       uuid,
  diff            jsonb,
  ip              inet,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_logs_org_idx ON audit_logs (organization_id, created_at DESC);

-- Transactional Outbox
CREATE TABLE outbox_events (
  id           bigserial PRIMARY KEY,
  event_type   text NOT NULL,
  payload      jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  attempts     integer NOT NULL DEFAULT 0
);
CREATE INDEX outbox_pending_idx ON outbox_events (id) WHERE processed_at IS NULL;

-- Multi-tenancy com Row-Level Security.
-- Obs.: o dono das tabelas (e papéis BYPASSRLS, como o do redirect/relay) não é afetado.
-- Em produção, a API usa um papel sem privilégio de dono e executa
-- SET LOCAL app.workspace_id = '<uuid>' em cada transação.
ALTER TABLE qrcodes ENABLE ROW LEVEL SECURITY;
CREATE POLICY ws_isolation ON qrcodes
  USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);

-- ===== Seeds =====
INSERT INTO roles (organization_id, name, permissions) VALUES
  (NULL, 'owner',   ARRAY['*']),
  (NULL, 'admin',   ARRAY['qr:read','qr:create','qr:update','qr:delete','analytics:read','members:manage','domains:manage']),
  (NULL, 'editor',  ARRAY['qr:read','qr:create','qr:update','analytics:read']),
  (NULL, 'analyst', ARRAY['qr:read','analytics:read']),
  (NULL, 'viewer',  ARRAY['qr:read']);

-- Domínio genérico da plataforma (localhost para desenvolvimento)
INSERT INTO domains (organization_id, hostname, status, verified_at)
VALUES (NULL, 'localhost', 'active', now());

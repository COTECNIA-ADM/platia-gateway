// Formas de las respuestas de la API del panel (gateway/app/admin, docs/panel.md).
// Los importes llegan como texto decimal y las fechas en ISO UTC.

export type Me = {
  id: number
  email: string
  name: string
  role: 'admin' | 'member'
  organization_id: number
  mfa_enabled: boolean
  scope: 'full' | 'mfa_setup'
  platform_operator: boolean
}

export type List<T> = { data: T[] }
export type Page<T> = { data: T[]; next: number | null }

export type Team = { id: number; slug: string; name: string; is_active: boolean; created_at: string }

export type Application = {
  id: number
  slug: string
  name: string
  team_id: number
  status: string
  trusted: boolean
  allowed_purposes: string[]
  allowed_models: string[] | null
  personal_keys: boolean
  cache_ttl_seconds: number
  cache_purposes: string[] | null
  active_keys: number
  last_used_at: string | null
  created_at: string
}

export type ApiKey = {
  id: number
  kind: 'application' | 'user'
  prefix: string
  label: string | null
  state: 'active' | 'expired' | 'revoked'
  application_id: number | null
  user_id: number | null
  owner: string | null
  created_at: string
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
}

export type NewKey = ApiKey & { key: string }

export type User = {
  id: number
  email: string
  name: string
  role: 'admin' | 'member'
  team_id: number | null
  is_active: boolean
  mfa_enabled: boolean
  has_password: boolean
  platform_operator: boolean
  last_login_at: string | null
  active_sessions: number
  active_keys: number
  created_at: string
}

export type Limit = {
  id: number
  scope: 'organization' | 'team' | 'application' | 'end_user'
  scope_id: number | null
  scope_label: string
  subject: string | null
  metric: 'cost_usd' | 'requests' | 'tokens' | 'concurrent'
  period: 'minute' | 'day' | 'month' | 'none'
  value: string
  alert_pct: number
  is_active: boolean
  used: string | null
  updated_at: string
}

export type PolicyCell = {
  direction: 'input' | 'output'
  check: 'jailbreak' | 'classified' | 'secret' | 'pii'
  default: string
  organization: string | null
  application: string | null
  effective: string
  origin: 'default' | 'organization' | 'application'
  allowed: string[]
}

export type Overview = {
  hours: number
  since: string
  totals: {
    requests: number
    success: number
    blocked: number
    errors: number
    input_tokens: number
    output_tokens: number
    cache_read_tokens: number
    cache_write_tokens: number
    cost_usd: string
    cache_hits: number
  }
  latency_ms: { p50: number | null; p95: number | null }
  overhead_ms: { p50: number | null; p95: number | null; p99: number | null; sample: number; target_p95: number }
  by_model: { model: string; requests: number; cost_usd: string; tokens: number }[]
  by_application: { application_id: number; application: string; requests: number; cost_usd: string; failed: number }[]
  series: { at: string; requests: number; errors: number; blocked: number; cost_usd: string }[]
}

export type AuditRow = {
  id: number
  request_id: string
  created_at: string
  completed_at: string | null
  application_id: number | null
  application: string | null
  team_id: number | null
  user_id: number | null
  end_user_ref: string | null
  api_key_prefix: string | null
  purpose: string | null
  contract: string
  stream: boolean
  requested_model: string | null
  model: string | null
  provider: string | null
  deployment_id: number | null
  status: 'success' | 'error' | 'blocked'
  http_status: number | null
  error_type: string | null
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  cost_usd: string | null
  latency_ms: number | null
  overhead_ms: number | null
  attempts: number | null
  security_flags: unknown
  prompt_hash: string | null
  sealed: boolean
}

export type AdminAction = {
  id: number
  created_at: string
  user_id: number | null
  action: string
  target_type: string
  target_id: string
  changes: Record<string, unknown> | null
  ip_address: string | null
  organization_id: number | null
}

export type Webhook = {
  id: number
  name: string
  url: string
  events: string[]
  is_active: boolean
  global: boolean
  consecutive_failures: number
  disabled_reason: string | null
  created_at: string
  deliveries: Record<string, number>
}

export type Delivery = {
  id: number
  event: string
  event_id: string
  status: string
  attempts: number
  next_attempt_at: string | null
  last_status_code: number | null
  last_error: string | null
  last_duration_ms: number | null
  created_at: string
  delivered_at: string | null
}

export type Provider = {
  id: number
  slug: string
  name: string
  kind: 'anthropic' | 'openai_compatible'
  base_url: string | null
  has_api_key: boolean
  is_active: boolean
}

export type Price = {
  id: number
  effective_from: string
  input_per_mtok: string
  output_per_mtok: string
  cache_read_per_mtok: string | null
  cache_write_per_mtok: string | null
}

export type Model = {
  id: number
  model_key: string
  display_name: string
  family: 'anthropic' | 'openai'
  context_window: number | null
  max_output_tokens: number | null
  capabilities: string[]
  fallback_models: string[]
  is_active: boolean
  price: Price | null
}

export type Deployment = {
  id: number
  model_id: number
  provider_id: number
  upstream_model: string
  priority: number
  weight: number
  is_active: boolean
  cooled: boolean
}

export type Catalog = { providers: Provider[]; models: Model[]; deployments: Deployment[]; can_edit: boolean }

/** Evento en tiempo real de /events/stream (G12). */
export type LiveEvent = { id: string; type: string; created_at: string; organization_id: number | null; data: Record<string, unknown> }

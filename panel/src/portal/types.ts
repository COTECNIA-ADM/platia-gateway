// Respuestas de /me/* (gateway/app/portal.py y account_api.py).

export type PortalInfo = {
  user: {
    id: number
    email: string
    name: string
    role: string
    team: string | null
    organization: string
    mfa_enabled: boolean
    password_changed_at: string | null
    last_login_at: string | null
  }
  personal_keys: { enabled: boolean; max_keys: number; max_days: number; max_grace_hours: number; purposes: string[] }
  models: { model: string; name: string; family: string; input_per_mtok: string | null; output_per_mtok: string | null }[]
  limits: { metric: string; period: string; value: string; alert_pct: number; used: string | null; only_you: boolean }[]
}

export type Usage = {
  days: number
  since: string
  totals: { requests: number; errors: number; blocked: number; input_tokens: number; output_tokens: number; cost_usd: string }
  by_model: { model: string; requests: number; tokens: number; cost_usd: string }[]
  series: { day: string; requests: number; errors: number; cost_usd: string }[]
}

export type MyRequest = {
  id: number
  request_id: string
  created_at: string
  application: string | null
  purpose: string | null
  model: string | null
  status: string
  http_status: number | null
  error_type: string | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: string | null
  latency_ms: number | null
  api_key_prefix: string | null
}

export type MyKey = { id: number; prefix: string; label: string | null; created_at: string; expires_at: string | null; last_used_at: string | null }
export type NewMyKey = MyKey & { key: string }

export type MySession = {
  id: number
  current: boolean
  created_at: string
  last_used_at: string | null
  expires_at: string
  ip_address: string | null
  user_agent: string | null
}

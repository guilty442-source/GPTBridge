export type NativeClient = 'android_native'

export type MobilePlatformContract = {
  name: string
  contract_version: number
  clients: string[]
  transport: 'paired_json_api'
  source_of_truth: 'desktop_shared_repository'
  shared_repository: true
  mobile_write_scope: 'queue_local_ai_command_only'
  foreground_sync_seconds: number
  upgrade_compatibility: {
    application_version: string
    state_schema: {
      current: number
      minimum_supported: number
    }
    analytics_schema: {
      current: number
    }
    migration_policy: 'snapshot_then_atomic_migrate'
    future_schema_policy: 'fail_closed_without_overwrite'
  }
  capabilities: string[]
  routes: {
    platform: string
    pair: string
    state: string
    local_ai_command: string
  }
}

export type MobileHolding = {
  symbol: string
  name?: string
  market?: string
  asset_type?: string
  quantity?: number
  average_cost?: number | null
  currency?: string
}

export type RiskNotice = {
  code?: string
  title?: string
  symbol?: string
  severity?: string
  detail?: string
  action?: string
  due?: string
  risk_level_label?: string
}

export type InvestmentMobileSnapshot = {
  ok: boolean
  tool: string
  version: string
  generated_at: string
  platform: MobilePlatformContract
  sync: {
    mode?: string
    mode_label?: string
    remote_status_label?: string
    pairing_expires_at?: string
  }
  state: {
    portfolio?: {
      file_name?: string
      holding_count?: number
      imported_at?: string
    } | null
    holdings?: MobileHolding[]
    local_ai_risk_warnings?: RiskNotice[]
    local_ai_action_plan?: RiskNotice[]
    local_ai_product_status?: {
      score?: number
      state_label?: string
      quote_health_label?: string
    }
  }
  analytics?: {
    performance?: {
      current_value?: number
      unrealized_pnl?: number
    }
    risk?: {
      var_95_one_day_percent?: number
      max_drawdown_percent?: number
    }
    alerts?: {
      unacknowledged_count?: number
    }
  }
  diagnostics?: {
    local_ai?: {
      state_label?: string
      quote_health_label?: string
      warning_count?: number
      critical_count?: number
    }
  }
}

export type SavedConnection = {
  baseUrl: string
  sessionToken: string
}

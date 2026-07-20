import { cachedHttpRequest, httpRequest } from "@/lib/request";

export type AuthRole = "admin" | "user";

export type LoginResponse = {
  ok: boolean;
  version: string;
  role: AuthRole;
  subject_id: string;
  name: string;
};

export type SystemSettings = {
  poll_interval_seconds: number;
  memory_warning_percent: number;
  memory_baseline_mb: number;
  memory_per_worker_mb: number;
};

export type SystemMemorySample = {
  timestamp: string;
  process: { rss_mb: number; vms_mb: number; cpu_percent: number; threads: number; uptime_seconds: number };
  system: { total_mb: number; available_mb: number; used_mb: number; percent: number };
  prediction: {
    registration_threads: number;
    predicted_peak_mb: number;
    projected_system_percent: number;
    risk: "low" | "medium" | "high" | string;
    model: string;
  };
};

export type SystemMemorySnapshot = SystemMemorySample & { history: SystemMemorySample[] };

export type SystemCleanupStatus = {
  categories: Record<string, { bytes: number; files: number; targets: number }>;
  total_bytes: number;
  total_files: number;
};

export type SystemGitStatus = {
  is_repo: boolean;
  branch?: string;
  commit?: string;
  remote?: string;
  upstream?: string;
  dirty?: boolean;
  dirty_count?: number;
  ahead?: number;
  behind?: number;
  update_available?: boolean;
  update_supported: boolean;
  reason?: string;
};

export type SystemOverview = {
  app: { name: string; version: string };
  settings: SystemSettings;
  auth: { source: "config" | "environment" | string; rotatable: boolean };
  memory: SystemMemorySnapshot;
  cleanup: SystemCleanupStatus;
  git: SystemGitStatus;
  restart: { supported: boolean; mode: string; reason?: string };
  hot_reload: boolean;
};

export type HotReloadStatus = {
  source: string;
  revision: number;
  applied_at: string;
  running: boolean;
  preserved_running_tasks: number;
  applies_to: "new_tasks" | "next_run" | string;
  task_snapshot_mode: boolean;
};

export type RegisterConfig = {
  total: number;
  threads: number;
  mode: "total" | "quota" | "available";
  register_mode?: "free" | "k12";
  product_mode?: "partial" | "finished";
  mailbox_group?: string;
  target_quota: number;
  target_available: number;
  check_interval: number;
  k12_mode?: boolean;
  enabled: boolean;
  stats: {
    success: number;
    fail: number;
    done: number;
    running: number;
    threads: number;
    elapsed_seconds?: number;
    avg_seconds?: number;
    success_rate?: number;
    current_quota?: number;
    current_available?: number;
    registered_total?: number;
    partial_total?: number;
    stored_total?: number;
  };
  logs?: Array<{ time: string; text: string; level: string }>;
  runtime_config?: {
    revision: number;
    task_snapshot_mode: boolean;
    last_hot_reload?: HotReloadStatus;
  };
};

export type MailboxItem = {
  provider_type: string;
  email: string;
  group?: string;
  state:
    | "unused"
    | "in_use"
    | "used"
    | "token_invalid"
    | "failed"
    | "region_blocked"
    | "existing_account"
    | "flow_blocked"
    | "cloudflare_blocked"
    | string;
  state_reason?: string;
  state_updated_at?: string;
  note?: string;
  created_at?: string;
  updated_at?: string;
  client_id?: string;
  client_secret?: string;
  client_secret_preview?: string;
  has_client_secret?: boolean;
  password?: string;
  refresh_token?: string;
  refresh_token_preview?: string;
  has_refresh_token?: boolean;
  token_refreshed_at?: string;
  token_refresh_status?: "success" | "failed" | string;
  token_refresh_error?: string;
  token_refresh_mode?: string;
  line?: string;
};

export type MailProviderConfigs = {
  registration_source: "outlook_token" | "gmail_oauth" | "cloudflare_temp_email" | "ddg_mail" | "duckmail" | "openai_cpa";
  outlook_token: {
    mailbox_group: string;
    mode: "auto" | "graph" | "imap" | string;
    imap_host: string;
    message_limit: number;
    split_aliases: boolean;
    split_count_per_mailbox: number;
  };
  cloudflare_temp_email: {
    enable: boolean;
    api_base: string;
    admin_password?: string;
    domain: string;
    has_admin_password?: boolean;
    admin_password_preview?: string;
  };
  gmail_oauth: {
    enable: boolean;
    mode: string;
    imap_host: string;
    message_limit: number;
    split_aliases: boolean;
    split_count_per_mailbox: number;
  };
  ddg_mail: {
    enable: boolean;
    ddg_token?: string;
    api_base: string;
    cf_inbox_jwt?: string;
    admin_password?: string;
    cf_api_key?: string;
    cf_auth_mode: string;
    cf_domain: string;
    cf_create_path: string;
    cf_messages_path: string;
    has_ddg_token?: boolean;
    ddg_token_preview?: string;
    has_cf_inbox_jwt?: boolean;
    cf_inbox_jwt_preview?: string;
  };
  duckmail: {
    enable: boolean;
    api_key?: string;
    default_domain: string;
    has_api_key?: boolean;
    api_key_preview?: string;
  };
  openai_cpa: {
    enable: boolean;
    domains: string;
    webhook_secret?: string;
    use_original_password_flow: boolean;
    suffix_mode: string;
    suffix_len_min: number;
    suffix_len_max: number;
    has_webhook_secret?: boolean;
    webhook_secret_preview?: string;
    enable_domain_grouping?: boolean;
    domain_group_count?: number;
    domain_group_strategy?: string;
    domain_fail_threshold?: number;
    domain_fail_cooldown_sec?: number;
    disabled_domains?: string;
    enable_multi_level_subdomain?: boolean;
    multi_level_subdomain_levels?: number;
  };
};

export type InventoryPageInfo = {
  offset: number;
  limit: number;
  returned: number;
  total: number;
  partial: boolean;
};

export type MailboxPool = {
  items: MailboxItem[];
  stats: Record<string, number>;
  groups?: string[];
  group_records?: MailboxGroupRecord[];
  group_stats?: Record<string, Record<string, number>>;
  default_group?: string;
  provider_configs?: MailProviderConfigs;
  imported?: number;
  updated?: number;
  removed?: number;
  state_removed?: number;
  page_info?: InventoryPageInfo;
};

export type MailboxGroupRecord = {
  name: string;
  count: number;
  created_at?: string;
  updated_at?: string;
};

export type MailboxGroupCatalog = {
  groups: MailboxGroupRecord[];
  default_group: string;
  selected_group: string;
};

export type MailboxCodeMessage = {
  provider?: string;
  mailbox?: string;
  message_id?: string;
  subject?: string;
  sender?: string;
  received_at?: string;
  body_preview?: string;
  code?: string;
};

export type MailboxCodeResult = {
  ok: boolean;
  found: boolean;
  code: string;
  email: string;
  provider: string;
  provider_ref?: string;
  source?: string;
  scanned?: number;
  message_index?: number;
  message?: MailboxCodeMessage | null;
  latest_message?: MailboxCodeMessage | null;
  attempts?: string[];
};

export type OutlookMailboxMessagesResult = {
  ok: boolean;
  email: string;
  inbox_address?: string;
  provider: string;
  provider_ref?: string;
  group?: string;
  count: number;
  messages: MailboxCodeMessage[];
};

export type OutlookTokenRefreshResult = {
  email: string;
  ok: boolean;
  method?: "graph" | "imap" | string;
  scope?: string;
  expires_in?: number;
  rotated?: boolean;
  invalid?: boolean;
  error?: string;
};

export type AccountItem = {
  inventory_id?: string;
  inventory_key?: string;
  email: string;
  password?: string;
  access_token?: string;
  refresh_token?: string;
  id_token?: string;
  access_token_preview?: string;
  refresh_token_preview?: string;
  has_access_token?: boolean;
  has_refresh_token?: boolean;
  source_type?: string;
  status?: string;
  note?: string;
  account_type?: string;
  plan_type?: string;
  effective_plan_type?: string;
  display_plan_type?: string;
  is_plus_for_filter?: boolean;
  plan_filter_reason?: string;
  token_source?: string;
  oauth_status?: string;
  oauth_refresh_status?: string;
  oauth_refresh_error?: string;
  token_refresh_required?: boolean;
  oauth_capable?: boolean;
  oauth_token_status?: string;
  oauth_client_id?: string;
  oauth_client_kind?: string;
  refresh_token_kind?: string;
  refresh_token_label?: string;
  sub2api_healthy?: boolean;
  warehouse_status?: string;
  finished_account?: boolean;
  finished_reason?: string;
  needs_oauth?: boolean;
  client_id?: string;
  chatgpt_account_id?: string;
  chatgpt_user_id?: string;
  token_expires_at?: number | string;
  token_expires_in?: number;
  created_at?: string;
  updated_at?: string;
  k12_mode?: boolean;
  k12_team_id?: string;
  workspace_id?: string;
  k12_join_status?: string;
  sub2api_imported_at?: string;
  sub2api_import_status?: string;
  sub2api_account_id?: number | string;
  sub2api_health_status?: string;
  sub2api_health_checked_at?: string;
  sub2api_health_code?: number;
  cpa_imported_at?: string;
  cpa_import_status?: string;
  cpa_auth_file?: string;
  cpa_last_error?: string;
  cpa_health_status?: string;
  cpa_health_checked_at?: string;
  cpa_health_code?: number;
  health_status?: string;
  health_checked_at?: string;
  health_method?: string;
  health_code?: number;
  health_error?: string;
  health_latency_ms?: number;
  registration_proxy?: {
    source?: string;
    proxy?: string;
    node_id?: string;
    node_name?: string;
    label?: string;
    slot?: number;
    managed_slot?: number;
    region?: string;
    recorded_at?: string;
  };
  registration_proxy_source?: string;
  registration_proxy_node_id?: string;
  registration_proxy_label?: string;
  quota_status?: string;
  quota_used?: number;
  quota_total?: number;
  quota_remaining?: number;
  quota_reset_at?: string;
  quota_5h_used?: number;
  quota_5h_total?: number;
  quota_5h_remaining?: number;
  quota_5h_reset_at?: string;
  quota_7d_used?: number;
  quota_7d_total?: number;
  quota_7d_remaining?: number;
  quota_7d_reset_at?: string;
  quota_checked_at?: string;
  quota_source?: string;
  quota_error_status?: string;
  last_quota_error?: string;
  quota_details?: Record<string, unknown>;
  last_health_result?: Record<string, unknown>;
  k12_last_error?: string;
  k12_last_attempt_at?: string;
  mailbox_group?: string;
  [key: string]: unknown;
};

export type AccountPool = {
  accounts: AccountItem[];
  stats: Record<string, unknown>;
  imported?: number;
  updated?: number;
  removed?: number;
  skipped?: number;
  parsed?: number;
  changed_emails?: string[];
  changed_account_ids?: string[];
  page_info?: InventoryPageInfo;
};

export type PartialAccountItem = {
  id: string;
  email: string;
  password?: string;
  password_preview?: string;
  has_password?: boolean;
  access_token?: string;
  access_token_preview?: string;
  has_access_token?: boolean;
  refresh_token?: string;
  refresh_token_preview?: string;
  has_refresh_token?: boolean;
  id_token?: string;
  id_token_preview?: string;
  has_id_token?: boolean;
  session_token?: string;
  session_token_preview?: string;
  has_session_token?: boolean;
  status?: string;
  stage?: string;
  reason?: string;
  source?: string;
  register_mode?: string;
  mailbox_group?: string;
  proxy?: string;
  provider?: string;
  provider_label?: string;
  inbox_address?: string;
  name?: string;
  birthdate?: string;
  landing?: string;
  continue_url?: string;
  oauth_status?: string;
  oauth_error?: string;
  web_login_ok?: boolean;
  chat_test_method?: string;
  chat_test_error?: string;
  health_status?: string;
  health_checked_at?: string;
  health_method?: string;
  health_code?: number;
  health_latency_ms?: number;
  health_error?: string;
  health_proxy_source?: string;
  health_proxy_label?: string;
  health_proxy_node_id?: string;
  health_proxy_sticky?: boolean;
  last_health_attempt_status?: string;
  last_health_attempt_code?: number;
  last_health_attempt_error?: string;
  last_health_attempt_at?: string;
  registration_proxy?: {
    source?: string;
    proxy?: string;
    node_id?: string;
    node_name?: string;
    label?: string;
    slot?: number;
    managed_slot?: number;
    region?: string;
    recorded_at?: string;
  };
  registration_proxy_source?: string;
  registration_proxy_node_id?: string;
  registration_proxy_label?: string;
  plan_type?: string;
  account_type?: string;
  quota_status?: string;
  quota_used?: number;
  quota_total?: number;
  quota_remaining?: number;
  quota_reset_at?: string;
  quota_checked_at?: string;
  quota_source?: string;
  quota_error_status?: string;
  last_quota_error?: string;
  oauth_refresh_status?: string;
  oauth_refresh_error?: string;
  oauth_refreshed_at?: string;
  token_refresh_required?: boolean;
  token_expires_at?: number | string;
  token_expires_in?: number;
  sub2api_imported_at?: string;
  sub2api_import_status?: string;
  sub2api_account_id?: number | string;
  sub2api_last_error?: string;
  cpa_imported_at?: string;
  cpa_import_status?: string;
  cpa_auth_file?: string;
  cpa_last_error?: string;
  note?: string;
  attempt_count?: number;
  created_at?: string;
  registered_at?: string;
  died_at?: string;
  lifespan_seconds?: number;
  updated_at?: string;
  last_seen_at?: string;
  oauth_capable?: boolean;
  oauth_token_status?: string;
  oauth_client_id?: string;
  oauth_client_kind?: string;
  refresh_token_kind?: string;
  refresh_token_label?: string;
  finished_account?: boolean;
  needs_oauth?: boolean;
};

export type PartialAccountPool = {
  items: PartialAccountItem[];
  stats: Record<string, number>;
  removed?: number;
  imported?: number;
  updated?: number;
  skipped?: number;
  parsed?: number;
  changed_selectors?: string[];
  page_info?: InventoryPageInfo;
};

export type PartialAccountHealthState = {
  settings: {
    scheduled_enabled: boolean;
    interval_minutes: number;
    refresh_quota: boolean;
    method: "auto" | "local" | "sub2api" | string;
    concurrency: number;
    updated_at?: string;
  };
  schedule_running: boolean;
  last_scheduled_run_at?: string;
  next_scheduled_run_at?: string;
  last_scheduled_stats?: { total?: number; alive?: number; dead?: number; failed?: number };
  last_error?: string;
  task?: {
    enabled: boolean;
    running: boolean;
    stop_requested?: boolean;
    started_at?: string;
    finished_at?: string;
    stats?: { total?: number; alive?: number; dead?: number; failed?: number };
    error?: string;
  };
  busy?: boolean;
  message?: string;
};

export type AccountOAuthSession = {
  job_id: string;
  source: "registered" | "partial" | string;
  email: string;
  auth_url: string;
  proxy_source?: string;
  proxy_node_id?: string;
  proxy_label?: string;
  proxy_sticky?: boolean;
  expires_at: string;
};

export type AccountOAuthCompletion = {
  ok: boolean;
  account: AccountItem;
  token_profile: Record<string, unknown>;
  warehouse: {
    ok: boolean;
    import?: WarehouseImportResult | Record<string, unknown>;
    test?: Record<string, unknown> | null;
  };
  account_pool: AccountPool;
  partial_account_pool: PartialAccountPool;
};

export type AccountInventoryState = {
  enabled: boolean;
  busy?: boolean;
  message?: string;
  job_running?: boolean;
  schedule_running: boolean;
  settings: {
    scheduled_enabled: boolean;
    interval_minutes: number;
    stale_after_minutes: number;
    method: "auto" | "local" | "sub2api" | string;
    refresh_quota_on_health: boolean;
    import_auto_health_check: boolean;
    import_drop_banned: boolean;
    auto_drop_banned: boolean;
    model_id: string;
    prompt: string;
    mode: string;
    concurrency: number;
    updated_at?: string;
  };
  stats: Record<string, unknown>;
  logs: Array<{ time: string; text: string; level: string }>;
  results: Array<Record<string, unknown>>;
  last_scheduled_run_at?: string;
  account_pool?: AccountPool;
};

export type AccountHealthPayload = {
  emails?: string[];
  method?: "auto" | "local" | "sub2api" | string;
  drop_banned?: boolean;
  refresh_quota?: boolean;
  model_id?: string;
  prompt?: string;
  mode?: string;
  concurrency?: number;
  only_stale?: boolean;
  stale_after_minutes?: number;
};


export type K12Config = {
  schema_version: number;
  enabled: boolean;
  team_ids: { outlook: string[]; gmail: string[] };
  join_route: string;
  accept_invite: boolean;
  invite_timeout: number;
  migrated_from_sub2api_at?: string;
  updated_at?: string;
};

export type K12State = {
  enabled: boolean;
  options: {
    emails?: string[];
    only_unprocessed?: boolean;
    join_k12?: boolean;
    import_to_sub2api?: boolean;
    import_to_cpa?: boolean;
    health_check?: boolean;
  };
  stats: {
    success: number;
    fail: number;
    done: number;
    running: number;
    total: number;
    elapsed_seconds?: number;
    success_rate?: number;
    started_at?: string;
    updated_at?: string;
  };
  logs: Array<{ time: string; text: string; level: string }>;
  results: Array<Record<string, unknown>>;
  account_pool?: AccountPool;
  config?: K12Config;
  sub2api_config?: Sub2ApiConfig;
  cpa_config?: CpaConfig;
};

export type PlusLinkConfig = {
  engine_mode?: "local" | string;
  use_proxy_pool: boolean;
  use_residential_proxy_pool: boolean;
  residential_proxy_pool: string;
  residential_proxy_count?: number;
  residential_country_proxy_pool?: Record<string, string>;
  residential_country_proxy_count?: Record<string, number>;
  residential_checkout_proxy: string;
  residential_checkout_proxy_count?: number;
  residential_provider_proxy: string;
  residential_provider_proxy_count?: number;
  residential_approve_proxy: string;
  residential_approve_proxy_count?: number;
  residential_promotion_proxy: string;
  residential_promotion_proxy_count?: number;
  residential_single_proxy_mode: boolean;
  residential_session_ttl_minutes: number;
  residential_via_project_proxy: boolean;
  residential_parent_node_filter: string;
  residential_via_clash: boolean;
  residential_clash_proxy: string;
  proxy: string;
  target_country: string;
  checkout_country: string;
  promotion_country: string;
  payment_method_country: string;
  payment_locale: string;
  concurrency: number;
  timeout_seconds: number;
  checkout_retry_limit: number;
  provider_retry_limit: number;
  matrix_combo_retry_limit: number;
  retry_limits_version?: number;
  extraction_mode?: "matrix" | "fixed_pair" | string;
  active_retry_limit?: number;
  refresh_tokens: boolean;
  provider_same_as_target: boolean;
  approve_same_as_checkout: boolean;
  country_rotation_enabled?: boolean;
  billing_country_rotation?: string[] | string;
  proxy_country_rotation?: string[] | string;
  matrix_total?: number;
  require_zero_stripe_amount: boolean;
  strict_same_proxy_ip: boolean;
  strict_approve_same_proxy_ip: boolean;
};

export type PlusLinkResult = {
  task_id: string;
  email: string;
  status: "queued" | "running" | "success" | "failed" | string;
  stage?: string;
  engine_step?: string;
  progress_percent?: number;
  progress_label?: string;
  error?: string;
  long_url?: string;
  long_url_present?: boolean;
  long_url_hash?: string;
  stripe_hosted_url?: string;
  cs_id?: string;
  billing_country?: string;
  payment_method_country?: string;
  promotion_country?: string;
  provider_country?: string;
  matrix_index?: number;
  matrix_total?: number;
  processor_entity?: string;
  currency?: string;
  stripe_amount?: string;
  link_attempt_count?: number;
  link_attempt_limit?: number;
  extraction_mode?: "matrix" | "fixed_pair" | string;
  active_retry_limit?: number;
  eligibility_status?: string;
  eligibility_plan?: string;
  eligibility_has_email?: boolean;
  eligibility_checked_at?: string;
  eligibility_attempt?: number;
  eligibility_attempt_limit?: number;
  proxy_label?: string;
  proxy_source?: string;
  current_proxy_ip?: string;
  current_ip_config?: string;
  left_proxy_ip?: string;
  right_proxy_ip?: string;
  left_proxy_country?: string;
  right_proxy_country?: string;
  proxy_stage?: string;
  proxy_country?: string;
  retry_of?: string;
  duration_seconds?: number;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  updated_at?: string;
};

export type PlusLinkState = {
  enabled: boolean;
  config: PlusLinkConfig;
  stats: {
    success: number;
    fail: number;
    done: number;
    running: number;
    total: number;
    elapsed_seconds?: number;
    success_rate?: number;
    started_at?: string;
    updated_at?: string;
  };
  logs: Array<{ time: string; text: string; level: string }>;
  results: PlusLinkResult[];
  account_pool?: AccountPool;
  partial_account_pool?: PartialAccountPool;
  proxy_pool?: ProxyPool;
};

export type GarbageHealthConfig = {
  input_dir: string;
  output_dir: string;
  proxy: string;
  concurrency: number;
  timeout: number;
  backend: "auto" | "curl" | "requests" | string;
  refresh_tokens: boolean;
  include_archives: boolean;
  check_models: boolean;
  check_quota: boolean;
  keep_alive_only: boolean;
  keep_with_quota_only: boolean;
  realtime_import_sub2api: boolean;
  sub2api_group_id: string;
  auto_delete_dead: boolean;
  limit: number;
};

export type GarbageHealthResult = {
  ok?: boolean;
  status?: string;
  status_code?: number;
  email?: string;
  identity?: string;
  plan_type?: string;
  chatgpt_account_id?: string;
  chatgpt_user_id?: string;
  latency_ms?: number;
  refreshed?: boolean;
  refresh_error?: string;
  token_sha?: string;
  error?: string;
  source_type?: string;
  sources?: Array<{ source?: string; path?: string }>;
  checked_at?: string;
  http_backend?: string;
  models_status_code?: number;
  models_ok?: boolean;
  available_model_count?: number;
  quota_status?: string;
  quota_used?: number;
  quota_total?: number;
  quota_remaining?: number;
  quota_reset_at?: string;
  quota_5h_used?: number;
  quota_5h_total?: number;
  quota_5h_remaining?: number;
  quota_5h_reset_at?: string;
  quota_7d_used?: number;
  quota_7d_total?: number;
  quota_7d_remaining?: number;
  quota_7d_reset_at?: string;
  quota_error_status?: string;
  sub2api_import_status?: string;
  sub2api_account_id?: number | string;
  sub2api_import_error?: string;
  sub2api_group_id?: number | string;
  sub2api_imported_at?: string;
  source_delete_status?: string;
  deleted_source_objects?: number;
  delete_error?: string;
};

export type GarbageHealthState = {
  enabled: boolean;
  config: GarbageHealthConfig;
  stats: {
    total: number;
    checked: number;
    alive: number;
    failed: number;
    with_quota?: number;
    kept_accounts?: number;
    realtime_import_success?: number;
    realtime_import_failed?: number;
    realtime_import_skipped?: number;
    deleted_dead_accounts?: number;
    deleted_source_objects?: number;
    delete_failed?: number;
    delete_archive_skipped?: number;
    running: number;
    elapsed_seconds?: number;
    started_at?: string;
    updated_at?: string;
    status_counts?: Record<string, number>;
    json_documents?: number;
    raw_account_objects?: number;
    deduped_accounts?: number;
  };
  logs: Array<{ time: string; text: string; level: string }>;
  results: GarbageHealthResult[];
  reports?: Record<string, string>;
  last_scan?: Record<string, unknown>;
};

export type Sub2ApiConfig = {
  enabled: boolean;
  base_url: string;
  username: string;
  skip_ssl_verify: boolean;
  import_method: "batch" | "data";
  group_ids: number[];
  concurrency: number;
  priority: number;
  rate_multiplier: number;
  load_factor: number;
  auto_pause_on_expired: boolean;
  auto_import: boolean;
  scheduled_enabled: boolean;
  check_interval_minutes: number;
  test_concurrency: number;
  test_model_id: string;
  test_prompt: string;
  test_mode: string;
  disable_on_failure: boolean;
  delete_on_failure: boolean;
  notes: string;
  has_admin_token?: boolean;
  has_password?: boolean;
  admin_token_preview?: string;
  password_preview?: string;
  last_import?: Record<string, unknown> | null;
  schedule_status?: { running?: boolean; last_run_at?: string; last_result?: Record<string, unknown> | null };
  updated_at?: string;
};

export type CpaConfig = {
  enabled: boolean;
  base_url: string;
  skip_ssl_verify: boolean;
  auto_import: boolean;
  scheduled_enabled: boolean;
  check_interval_minutes: number;
  concurrency: number;
  quota_threshold: number;
  disable_on_failure: boolean;
  delete_dead_accounts: boolean;
  delete_quota_exhausted: boolean;
  notes: string;
  has_management_key?: boolean;
  management_key_preview?: string;
  last_import?: Record<string, unknown> | null;
  schedule_status?: { running?: boolean; last_run_at?: string; last_result?: Record<string, unknown> | null };
  updated_at?: string;
};

export type WarehouseImportItemResult = {
  email?: string;
  name?: string;
  filename?: string;
  account_id?: number | string;
  ok: boolean;
  status_code?: number;
  error?: string;
  response?: unknown;
};

export type WarehouseImportResult = {
  ok: boolean;
  partial: boolean;
  total: number;
  success: number;
  fail: number;
  results: WarehouseImportItemResult[];
  last_error?: string;
  status_code?: number;
};

export async function login(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  return httpRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: {},
    headers: { Authorization: `Bearer ${normalizedAuthKey}` },
    redirectOnUnauthorized: false,
  });
}

export async function fetchRegisterConfig() {
  return httpRequest<{ register: RegisterConfig }>("/api/register");
}

export async function updateRegisterConfig(updates: Partial<RegisterConfig>) {
  return httpRequest<{ register: RegisterConfig }>("/api/register", { method: "POST", body: updates });
}

export async function startRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/start", { method: "POST" });
}

export async function stopRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/stop", { method: "POST" });
}

export async function resetRegister() {
  return httpRequest<{ register: RegisterConfig }>("/api/register/reset", { method: "POST" });
}

export async function resetOutlookTokenPool(scope: "all" | "failed" | "unused" = "all") {
  return httpRequest<{ register: RegisterConfig }>("/api/register/outlook-pool/reset", {
    method: "POST",
    body: { scope },
  });
}

export async function fetchMailboxPool() {
  return httpRequest<{ mailboxes: MailboxPool }>("/api/mailboxes");
}

export async function fetchVersion() {
  return httpRequest<{ version: string; edition: string }>("/version");
}

export async function fetchSystemOverview() {
  return httpRequest<SystemOverview>("/api/system/overview");
}

export async function fetchSystemMetrics() {
  return httpRequest<{ memory: SystemMemorySnapshot }>("/api/system/metrics");
}

export async function updateSystemSettings(settings: Partial<SystemSettings>) {
  return httpRequest<{ settings: SystemSettings }>("/api/system/settings", { method: "PATCH", body: settings });
}

export async function rotateSystemAuthKey(currentKey: string, newKey: string, confirmKey: string) {
  return httpRequest<{ ok: boolean; auth: { source: string; rotatable: boolean } }>("/api/system/auth-key", {
    method: "POST",
    body: { current_key: currentKey, new_key: newKey, confirm_key: confirmKey },
  });
}

export async function cleanupSystem(categories: string[], dryRun = false) {
  return httpRequest<{
    ok: boolean;
    dry_run: boolean;
    categories: string[];
    removed_files: number;
    removed_bytes: number;
    errors: string[];
    remaining: SystemCleanupStatus;
  }>("/api/system/cleanup", { method: "POST", body: { categories, dry_run: dryRun } });
}

export async function resetSystemSettings() {
  return httpRequest<{ ok: boolean; settings: SystemSettings; preserved: string[] }>("/api/system/reset", {
    method: "POST",
    body: { confirm: true },
  });
}

export async function fetchSystemGitStatus() {
  return httpRequest<{ git: SystemGitStatus }>("/api/system/git/status");
}

export async function checkSystemGit() {
  return httpRequest<{ git: SystemGitStatus }>("/api/system/git/check", { method: "POST" });
}

export async function updateSystemGit() {
  return httpRequest<{ ok: boolean; updated: boolean; restart_required: boolean; git: SystemGitStatus }>("/api/system/git/update", {
    method: "POST",
  });
}

export async function restartSystem() {
  return httpRequest<{ ok: boolean; scheduled: boolean; delay_seconds: number }>("/api/system/restart", { method: "POST" });
}

export async function fetchOutlookMailboxPool(options: { force?: boolean; preview?: boolean } = {}) {
  const path = options.preview ? "/api/outlook-mailboxes?limit=100" : "/api/outlook-mailboxes";
  return cachedHttpRequest<{ mailboxes: MailboxPool }>(path, { maxAgeMs: 10_000, force: options.force });
}

export async function fetchOutlookMailboxMessages(email: string, options: { limit?: number; mode?: "auto" | "graph" | "imap"; request_timeout?: number } = {}) {
  return httpRequest<{ mailbox_messages: OutlookMailboxMessagesResult }>("/api/outlook-mailboxes/messages", {
    method: "POST",
    body: { email, limit: options.limit ?? 20, mode: options.mode || "auto", request_timeout: options.request_timeout ?? 30 },
  });
}

export async function refreshOutlookMailboxTokens(emails: string[], options: { mode?: "auto" | "graph" | "imap"; request_timeout?: number; concurrency?: number } = {}) {
  return httpRequest<{ results: OutlookTokenRefreshResult[]; summary: Record<string, number>; mailboxes: MailboxPool }>("/api/outlook-mailboxes/refresh-token", {
    method: "POST",
    body: { emails, mode: options.mode || "auto", request_timeout: options.request_timeout ?? 30, concurrency: options.concurrency ?? 5 },
  });
}

export async function importMailboxes(text: string, note = "", providerType = "outlook_token", group = "默认") {
  return httpRequest<{ mailboxes: MailboxPool; register: RegisterConfig }>("/api/mailboxes/import", {
    method: "POST",
    body: { text, note, provider_type: providerType, group },
  });
}

export async function updateMailboxGroups(emails: string[], group: string, providerType = "") {
  return httpRequest<{ mailboxes: MailboxPool; register: RegisterConfig }>("/api/mailboxes/group", {
    method: "POST",
    body: { emails, group, provider_type: providerType },
  });
}

export async function fetchMailboxGroups() {
  return httpRequest<{ mailbox_groups: MailboxGroupCatalog }>("/api/mailbox-groups");
}

export async function createMailboxGroup(name: string) {
  return httpRequest<{ mailbox_groups: MailboxGroupCatalog; register: RegisterConfig }>("/api/mailbox-groups", { method: "POST", body: { name } });
}

export async function renameMailboxGroup(name: string, newName: string) {
  return httpRequest<{ mailbox_groups: MailboxGroupCatalog; register: RegisterConfig }>("/api/mailbox-groups", { method: "PATCH", body: { name, new_name: newName } });
}

export async function deleteMailboxGroup(name: string, moveTo = "默认") {
  return httpRequest<{ mailbox_groups: MailboxGroupCatalog; register: RegisterConfig }>("/api/mailbox-groups", { method: "DELETE", body: { name, move_to: moveTo } });
}

export async function fetchMailProviderConfigs(options: { force?: boolean } = {}) {
  return cachedHttpRequest<{ provider_configs: MailProviderConfigs }>("/api/mail-provider-configs", { maxAgeMs: 15_000, force: options.force });
}

export async function updateMailProviderConfigs(updates: Partial<MailProviderConfigs>) {
  return httpRequest<{ provider_configs: MailProviderConfigs; mailboxes: MailboxPool; register: RegisterConfig }>("/api/mail-provider-configs", {
    method: "POST",
    body: updates,
  });
}

export async function resetMailboxStates(scope: "all" | "failed" = "failed") {
  return httpRequest<{ mailboxes: MailboxPool; register: RegisterConfig }>("/api/mailboxes/reset-state", {
    method: "POST",
    body: { scope },
  });
}

export async function clearMailboxPool(removeStates = false) {
  return httpRequest<{ mailboxes: MailboxPool; register: RegisterConfig }>("/api/mailboxes/clear", {
    method: "POST",
    body: { remove_states: removeStates },
  });
}

export async function deleteMailbox(email: string) {
  return httpRequest<{ mailboxes: MailboxPool; register: RegisterConfig }>(`/api/mailboxes/${encodeURIComponent(email)}`, {
    method: "DELETE",
  });
}

export async function fetchMailboxCode(email: string, providerType = "", scanRecent = true) {
  return httpRequest<{ mailbox_code: MailboxCodeResult }>("/api/mailboxes/code", {
    method: "POST",
    body: { email, provider_type: providerType, scan_recent: scanRecent },
  });
}

export async function fetchAccountPool(options: { force?: boolean; preview?: boolean } = {}) {
  const path = options.preview ? "/api/accounts?limit=100" : "/api/accounts";
  return cachedHttpRequest<{ account_pool: AccountPool }>(path, { maxAgeMs: 10_000, force: options.force });
}

export async function fetchPartialAccountPool(options: { force?: boolean; preview?: boolean } = {}) {
  const path = options.preview ? "/api/partial-accounts?limit=100" : "/api/partial-accounts";
  return cachedHttpRequest<{ partial_account_pool: PartialAccountPool }>(path, { maxAgeMs: 10_000, force: options.force });
}

export async function importPartialAccounts(text: string, options: { auto_health_check?: boolean; drop_banned?: boolean } = {}) {
  return httpRequest<{
    partial_account_pool: PartialAccountPool;
    partial_health?: Record<string, unknown> | null;
    imported: number;
    updated: number;
    skipped: number;
    parsed: number;
  }>("/api/partial-accounts/import", {
    method: "POST",
    body: { text, ...options },
  });
}

export async function updatePartialAccount(selector: string, updates: Partial<PartialAccountItem>) {
  return httpRequest<{ partial_account: PartialAccountItem; partial_account_pool: PartialAccountPool }>(`/api/partial-accounts/${encodeURIComponent(selector)}`, {
    method: "PATCH",
    body: updates,
  });
}

export async function deletePartialAccount(selector: string) {
  return httpRequest<{ partial_account_pool: PartialAccountPool }>(`/api/partial-accounts/${encodeURIComponent(selector)}`, { method: "DELETE" });
}

export async function deletePartialAccounts(selectors: string[]) {
  return httpRequest<{ partial_account_pool: PartialAccountPool }>("/api/partial-accounts/delete", {
    method: "POST",
    body: { selectors },
  });
}

export async function prunePartialAccounts() {
  return httpRequest<{ partial_account_pool: PartialAccountPool }>("/api/partial-accounts/prune-banned", { method: "POST" });
}

export async function clearPartialAccounts() {
  return httpRequest<{ partial_account_pool: PartialAccountPool }>("/api/partial-accounts/clear", { method: "POST" });
}

export async function checkPartialAccountHealth(selectors: string[] = [], refreshQuota = false, concurrency = 8, mode = "", method = "auto") {
  return httpRequest<{
    partial_health: {
      ok: boolean;
      stats: { total: number; alive: number; dead: number; failed: number; quota_ok?: number; quota_failed?: number };
      results: Array<Record<string, unknown>>;
      partial_account_pool: PartialAccountPool;
    };
  }>("/api/partial-accounts/health/check", {
    method: "POST",
    body: { selectors, refresh_quota: refreshQuota, concurrency, mode, method },
  });
}

export async function checkPartialAccountQuota(selectors: string[] = [], concurrency = 8) {
  return httpRequest<{
    partial_health: {
      ok: boolean;
      stats: { total: number; alive: number; dead: number; failed: number; quota_ok?: number; quota_failed?: number };
      results: Array<Record<string, unknown>>;
      partial_account_pool: PartialAccountPool;
    };
  }>("/api/partial-accounts/quota/check", {
    method: "POST",
    body: { selectors, concurrency },
  });
}

export async function exportPartialAccountsToSub2ApiData(selectors: string[] = []) {
  return httpRequest<{ sub2api: { count: number; data: Record<string, unknown> } }>("/api/partial-accounts/export-sub2api", {
    method: "POST",
    body: { selectors },
  });
}

export async function importPartialAccountsToSub2Api(selectors: string[] = []) {
  return httpRequest<{
    promotion: {
      ok: boolean;
      partial: boolean;
      total: number;
      success: number;
      fail: number;
      results: Array<Record<string, unknown>>;
      account_pool: AccountPool;
      partial_account_pool: PartialAccountPool;
    };
  }>("/api/partial-accounts/import-sub2api", {
    method: "POST",
    body: { selectors },
  });
}

export async function importPartialAccountsToCpa(selectors: string[] = []) {
  return httpRequest<{ cpa: WarehouseImportResult; partial_account_pool: PartialAccountPool }>("/api/partial-accounts/import-cpa", {
    method: "POST",
    body: { selectors },
  });
}

export async function refreshPartialAccountToken(selector: string) {
  return httpRequest<{
    partial_refresh: {
      ok: boolean;
      partial_account: PartialAccountItem;
      partial_account_pool: PartialAccountPool;
      proxy_source?: string;
      proxy_node_id?: string;
      proxy_label?: string;
      proxy_sticky?: boolean;
    };
  }>(`/api/partial-accounts/${encodeURIComponent(selector)}/refresh-token`, { method: "POST" });
}

export async function fetchPartialAccountHealthState() {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health");
}

export async function updatePartialAccountHealthSettings(settings: Partial<PartialAccountHealthState["settings"]>) {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health/settings", {
    method: "POST",
    body: settings,
  });
}

export async function startPartialAccountHealthSchedule(settings: Partial<PartialAccountHealthState["settings"]>) {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health/start-schedule", {
    method: "POST",
    body: settings,
  });
}

export async function stopPartialAccountHealthSchedule() {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health/stop-schedule", {
    method: "POST",
  });
}

export async function startPartialAccountHealth(options: {
  selectors?: string[];
  method?: string;
  refresh_quota?: boolean;
  concurrency?: number;
  mode?: string;
} = {}) {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health/start", {
    method: "POST",
    body: options,
  });
}

export async function stopPartialAccountHealth() {
  return httpRequest<{ partial_health: PartialAccountHealthState }>("/api/partial-accounts/health/stop", {
    method: "POST",
  });
}

export async function startAccountOAuth(source: "registered" | "partial", selector: string) {
  return httpRequest<{ oauth: AccountOAuthSession }>("/api/account-oauth/start", {
    method: "POST",
    body: { source, selector },
  });
}

export async function completeAccountOAuth(jobId: string, callback: string) {
  return httpRequest<{ oauth: AccountOAuthCompletion }>("/api/account-oauth/complete", {
    method: "POST",
    body: { job_id: jobId, callback },
  });
}

export async function acquireAccountOAuthAutomatically(source: "registered" | "partial", selector: string) {
  return httpRequest<{ oauth: AccountOAuthCompletion }>("/api/account-oauth/automatic", {
    method: "POST",
    body: { source, selector },
  });
}

export async function importPartialAccountToSub2Api(selector: string) {
  return httpRequest<{
    promotion: {
      ok: boolean;
      account: AccountItem;
      warehouse: AccountOAuthCompletion["warehouse"];
      account_pool: AccountPool;
      partial_account_pool: PartialAccountPool;
    };
  }>(`/api/partial-accounts/${encodeURIComponent(selector)}/import-sub2api`, { method: "POST" });
}

export async function importAccounts(text: string, options: { auto_health_check?: boolean; drop_banned?: boolean; health_method?: string } = {}) {
  return httpRequest<{ account_pool: AccountPool; inventory_health?: Record<string, unknown>; inventory?: AccountInventoryState; auto_health_started?: boolean }>("/api/accounts/import", {
    method: "POST",
    body: { text, ...options },
  });
}

export async function fetchAccountInventory() {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/inventory");
}

export async function fetchAccountHealth() {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health");
}

export async function updateAccountHealthSettings(updates: Partial<AccountInventoryState["settings"]>) {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health/settings", { method: "POST", body: updates });
}

export async function checkAccountHealth(payload: AccountHealthPayload = {}) {
  return httpRequest<{ inventory_health: Record<string, unknown>; account_pool: AccountPool; inventory?: AccountInventoryState }>("/api/accounts/health/check", { method: "POST", body: payload });
}

export async function startAccountHealth(payload: AccountHealthPayload = {}) {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health/start", { method: "POST", body: payload });
}

export async function stopAccountHealth() {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health/stop", { method: "POST" });
}

export async function startAccountHealthSchedule(updates: Partial<AccountInventoryState["settings"]> = {}) {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health/start-schedule", { method: "POST", body: updates });
}

export async function stopAccountHealthSchedule() {
  return httpRequest<{ inventory: AccountInventoryState }>("/api/accounts/health/stop-schedule", { method: "POST" });
}

export async function checkAccountQuota(payload: AccountHealthPayload = {}) {
  return httpRequest<{ inventory_health: Record<string, unknown>; account_pool: AccountPool; inventory?: AccountInventoryState }>("/api/accounts/quota/check", { method: "POST", body: payload });
}

export async function refreshAccountTypes(payload: AccountHealthPayload = {}) {
  return httpRequest<{ inventory_health: Record<string, unknown>; account_pool: AccountPool; inventory?: AccountInventoryState }>("/api/accounts/type/refresh", { method: "POST", body: payload });
}

export async function pruneBannedAccounts() {
  return httpRequest<{ account_pool: AccountPool; inventory: AccountInventoryState }>("/api/accounts/prune-banned", { method: "POST" });
}

export async function clearAccountPool() {
  return httpRequest<{ account_pool: AccountPool }>("/api/accounts/clear", { method: "POST" });
}

export async function deleteAccount(email: string) {
  return httpRequest<{ account_pool: AccountPool }>(`/api/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
}

export async function updateAccount(email: string, updates: Record<string, unknown>) {
  return httpRequest<{ account: AccountItem; account_pool: AccountPool }>(`/api/accounts/${encodeURIComponent(email)}`, { method: "PATCH", body: updates });
}

export async function refreshAccountToken(email: string) {
  return httpRequest<{ account: AccountItem; account_pool: AccountPool }>(`/api/accounts/${encodeURIComponent(email)}/refresh-token`, { method: "POST" });
}

export async function fetchSub2ApiConfig() {
  return httpRequest<{ sub2api_config: Sub2ApiConfig }>("/api/sub2api-config");
}

export async function updateSub2ApiConfig(updates: Partial<Omit<Sub2ApiConfig, "group_ids">> & { group_ids?: number[] | string; admin_token?: string; password?: string }) {
  return httpRequest<{ sub2api_config: Sub2ApiConfig; register: RegisterConfig }>("/api/sub2api-config", {
    method: "POST",
    body: updates,
  });
}

export async function exportAccountsToSub2ApiData(emails: string[] = []) {
  return httpRequest<{ sub2api: { count: number; data: unknown } }>("/api/accounts/export-sub2api", {
    method: "POST",
    body: { emails },
  });
}

export async function importAccountsToSub2Api(emails: string[] = []) {
  return httpRequest<{ sub2api: WarehouseImportResult; account_pool: AccountPool }>("/api/accounts/import-sub2api", {
    method: "POST",
    body: { emails },
  });
}

export async function fetchK12State() {
  return httpRequest<{ k12: K12State }>("/api/k12");
}

export async function updateK12Config(updates: Partial<K12Config>) {
  return httpRequest<{ k12: K12State; register: RegisterConfig }>("/api/k12/config", { method: "POST", body: updates });
}

export async function startK12Task(options: Partial<K12State["options"]> = {}) {
  return httpRequest<{ k12: K12State }>("/api/k12/start", { method: "POST", body: options });
}

export async function stopK12Task() {
  return httpRequest<{ k12: K12State }>("/api/k12/stop", { method: "POST" });
}

export async function resetK12Task() {
  return httpRequest<{ k12: K12State }>("/api/k12/reset", { method: "POST" });
}

export async function importAccountsToCpa(emails: string[] = []) {
  return httpRequest<{ cpa: WarehouseImportResult; account_pool: AccountPool }>("/api/accounts/import-cpa", {
    method: "POST",
    body: { emails },
  });
}

export async function fetchCpaConfig() {
  return httpRequest<{ cpa_config: CpaConfig }>("/api/cpa-config");
}

export async function updateCpaConfig(updates: Partial<CpaConfig> & { management_key?: string }) {
  return httpRequest<{ cpa_config: CpaConfig; register: RegisterConfig }>("/api/cpa-config", { method: "POST", body: updates });
}

export async function testSub2ApiConnection() {
  return httpRequest<{ sub2api_connection: Record<string, unknown> }>("/api/sub2api/connection/test", { method: "POST" });
}

export async function testCpaConnection() {
  return httpRequest<{ cpa_connection: Record<string, unknown> }>("/api/cpa/connection/test", { method: "POST" });
}

export async function fetchPlusLinkState() {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link");
}

export async function updatePlusLinkConfig(updates: Partial<PlusLinkConfig>) {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/config", { method: "POST", body: updates });
}

export async function startPlusLinkTask(payload: {
  emails?: string[];
  partial_accounts?: string[];
  manual_accounts?: Array<Record<string, unknown> | string>;
  manual_tokens?: string[];
  clear_results?: boolean;
  config?: Partial<PlusLinkConfig>;
} = {}) {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/start", { method: "POST", body: payload });
}

export async function stopPlusLinkTask() {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/stop", { method: "POST" });
}

export async function pausePlusLinkTask(taskId: string) {
  return httpRequest<{ plus_link: PlusLinkState }>(`/api/plus-link/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
}

export async function resumePlusLinkTask(taskId: string) {
  return httpRequest<{ plus_link: PlusLinkState }>(`/api/plus-link/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
}

export async function cancelPlusLinkTask(taskId: string) {
  return httpRequest<{ plus_link: PlusLinkState }>(`/api/plus-link/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

export async function retryPlusLinkTask(taskId: string) {
  return httpRequest<{ plus_link: PlusLinkState }>(`/api/plus-link/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" });
}

export async function clearPlusLinkTask() {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/clear", { method: "POST" });
}

export async function clearFailedPlusLinkTasks() {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/clear-failed", { method: "POST" });
}

export async function clearFinishedPlusLinkTasks() {
  return httpRequest<{ plus_link: PlusLinkState }>("/api/plus-link/clear-finished", { method: "POST" });
}

export async function fetchGarbageHealthState() {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health");
}

export async function updateGarbageHealthConfig(updates: Partial<GarbageHealthConfig>) {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health/config", { method: "POST", body: updates });
}

export async function scanGarbageHealth(updates: Partial<GarbageHealthConfig>) {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health/scan", { method: "POST", body: updates });
}

export async function selectGarbageHealthDirectory() {
  return httpRequest<{ directory: { selected: boolean; path: string } }>("/api/garbage-health/select-directory", { method: "POST" });
}

export async function startGarbageHealthTask(payload: { clear_results?: boolean; config?: Partial<GarbageHealthConfig> } = {}) {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health/start", { method: "POST", body: payload });
}

export async function stopGarbageHealthTask() {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health/stop", { method: "POST" });
}

export async function clearGarbageHealthTask() {
  return httpRequest<{ garbage_health: GarbageHealthState }>("/api/garbage-health/clear", { method: "POST" });
}

export async function exportGarbageHealth(format: "sub2api" | "cpa" | "full" | string = "sub2api") {
  const query = new URLSearchParams({ format });
  return httpRequest<{ garbage_export: { format: string; count: number; filename: string; data: unknown } }>(`/api/garbage-health/export?${query.toString()}`);
}

export async function importGarbageHealthToSub2Api(groupId: string | number = "") {
  return httpRequest<{ garbage_import: Record<string, unknown> }>("/api/garbage-health/import-sub2api", { method: "POST", body: { group_id: groupId || null } });
}

export async function importGarbageHealthToCpa() {
  return httpRequest<{ garbage_import: Record<string, unknown> }>("/api/garbage-health/import-cpa", { method: "POST" });
}


export type ProxyPoolNode = {
  id: string;
  name: string;
  type: string;
  server?: string;
  port?: string;
  proxy_url?: string;
  subscription_id?: string;
  enabled: boolean;
  source?: string;
  status?: string;
  latency_ms?: number;
  last_checked_at?: string;
  created_at?: string;
  updated_at?: string;
};

export type ProxyPoolSubscription = {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  allow_private?: boolean;
  last_error?: string;
  last_imported?: number;
  created_at?: string;
  updated_at?: string;
};

export type ProxyPool = {
  enabled: boolean;
  mode: string;
  strategy: string;
  node_mode: string;
  fallback_proxy: string;
  node_blacklist: string[];
  fastest_mode: boolean;
  max_retry_count: number;
  retry_delay: number;
  mihomo: {
    enabled: boolean;
    api_base: string;
    selector: string;
    proxy_scheme: string;
    proxy_host: string;
    proxy_port_base: number;
    thread_port_mode?: boolean;
    has_secret?: boolean;
    secret_preview?: string;
    secret?: string;
  };
  managed_mihomo: {
    enabled: boolean;
    executable: string;
    auto_download: boolean;
    proxy_host: string;
    proxy_port_base: number;
    api_port_base: number;
    startup_timeout: number;
    has_executable?: boolean;
  };
  subscriptions: ProxyPoolSubscription[];
  nodes: ProxyPoolNode[];
  stats?: Record<string, number>;
  imported_nodes?: number;
  removed?: number;
  errors?: Array<Record<string, string>>;
};

export type ManagedMihomoStatus = {
  installed: boolean;
  default_executable?: string;
  running: number;
  instances: Array<{
    slot: number;
    running: boolean;
    pid?: number;
    proxy_url: string;
    api_base: string;
    selected?: string;
    region?: string;
    openai_status?: number;
    probe_latency_ms?: number;
  }>;
  last_error?: string;
  last_started_at?: string;
  quarantined?: Array<{
    name: string;
    until: number;
    remaining_seconds: number;
    reason: string;
  }>;
};

export async function fetchProxyPool(options: { force?: boolean } = {}) {
  return cachedHttpRequest<{ proxy_pool: ProxyPool }>("/api/proxy-pool", { maxAgeMs: 10_000, force: options.force });
}

export async function updateProxyPool(updates: Partial<ProxyPool>) {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>("/api/proxy-pool", { method: "POST", body: updates });
}

export async function importProxySubscriptions(payload: { items?: Array<{ name?: string; url: string; allow_private?: boolean }>; text?: string; name?: string; url?: string; fetch?: boolean; allow_private?: boolean }) {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>("/api/proxy-pool/import-subscriptions", { method: "POST", body: payload });
}

export async function importProxyNodes(text: string) {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>("/api/proxy-pool/import-nodes", { method: "POST", body: { text } });
}

export async function syncProxyPoolMihomo() {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>("/api/proxy-pool/sync-mihomo", { method: "POST" });
}

export async function fetchManagedMihomoStatus() {
  return httpRequest<{ managed_mihomo: ManagedMihomoStatus }>("/api/proxy-pool/managed-mihomo/status");
}

export async function installManagedMihomo() {
  return httpRequest<{ managed_mihomo: ManagedMihomoStatus; proxy_pool: ProxyPool }>("/api/proxy-pool/managed-mihomo/install", { method: "POST" });
}

export async function startManagedMihomo(threads: number) {
  return httpRequest<{ managed_mihomo: ManagedMihomoStatus; proxy_pool: ProxyPool }>("/api/proxy-pool/managed-mihomo/start", { method: "POST", body: { threads } });
}

export async function stopManagedMihomo() {
  return httpRequest<{ managed_mihomo: ManagedMihomoStatus; proxy_pool: ProxyPool }>("/api/proxy-pool/managed-mihomo/stop", { method: "POST" });
}

export async function previewProxyAssignments(threads: number, tasks = threads * 2) {
  return httpRequest<{
    assignments: Array<{ proxy?: string; source?: string; label?: string; slot?: number; error?: string }>;
    managed_mihomo: ManagedMihomoStatus;
    proxy_pool: ProxyPool;
  }>("/api/proxy-pool/preview-assignments", { method: "POST", body: { threads, tasks } });
}

export async function testProxyPoolNode(nodeId: string, proxyUrl = "") {
  return httpRequest<{ proxy_test: Record<string, unknown>; proxy_pool: ProxyPool }>("/api/proxy-pool/test", { method: "POST", body: { node_id: nodeId, proxy_url: proxyUrl } });
}

export async function clearProxyPoolNodes() {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>("/api/proxy-pool/clear-nodes", { method: "POST" });
}

export async function deleteProxyPoolNode(nodeId: string) {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>(`/api/proxy-pool/nodes/${encodeURIComponent(nodeId)}`, { method: "DELETE" });
}

export async function deleteProxyPoolSubscription(subscriptionId: string) {
  return httpRequest<{ proxy_pool: ProxyPool; hot_reload: HotReloadStatus }>(`/api/proxy-pool/subscriptions/${encodeURIComponent(subscriptionId)}`, { method: "DELETE" });
}

export async function prefetchManagementData(section: string, view = "") {
  const requests: Array<Promise<unknown>> = [];
  if (section === "mail") {
    requests.push(view === "config" ? fetchMailProviderConfigs() : fetchOutlookMailboxPool({ preview: true }));
  } else if (section === "accounts") {
    requests.push(view === "partials" ? fetchPartialAccountPool({ preview: true }) : fetchAccountPool({ preview: true }));
  } else if (section === "proxy") {
    requests.push(fetchProxyPool());
  }
  await Promise.allSettled(requests);
}

export type Sub2ApiAccountList = {
  ok: boolean;
  page: number;
  page_size: number;
  total: number;
  items: Array<Record<string, unknown>>;
};

export type Sub2ApiGroupList = {
  ok: boolean;
  items: Array<Record<string, unknown>>;
  path?: string;
  errors?: string[];
};

export async function fetchSub2ApiGroups() {
  return httpRequest<{ sub2api_groups: Sub2ApiGroupList }>("/api/sub2api/groups");
}

export async function fetchSub2ApiAccounts(params: { page?: number; pageSize?: number; groupId?: string; search?: string; status?: string } = {}) {
  const query = new URLSearchParams();
  query.set("page", String(params.page || 1));
  query.set("page_size", String(params.pageSize || 20));
  if (params.groupId) query.set("group_id", params.groupId);
  if (params.search) query.set("search", params.search);
  if (params.status && params.status !== "all") query.set("status", params.status);
  return httpRequest<{ sub2api_accounts: Sub2ApiAccountList }>(`/api/sub2api/accounts?${query.toString()}`);
}

export async function testSub2ApiAccounts(payload: { account_ids?: Array<number | string>; group_id?: number | string; model_id?: string; prompt?: string; mode?: string; concurrency?: number; apply_policy?: boolean }) {
  return httpRequest<{ sub2api_test: Record<string, unknown> }>("/api/sub2api/accounts/test", { method: "POST", body: payload });
}

export async function fetchSub2ApiAccountsUsage(accountIds: Array<number | string>) {
  return httpRequest<{ sub2api_usage: Record<string, unknown> }>("/api/sub2api/accounts/usage", { method: "POST", body: { account_ids: accountIds } });
}

export async function actOnSub2ApiAccounts(accountIds: Array<number | string>, action: "enable" | "disable" | "delete") {
  return httpRequest<{ sub2api_action: Record<string, unknown> }>("/api/sub2api/accounts/action", { method: "POST", body: { account_ids: accountIds, action } });
}

export type CpaAccountList = {
  ok: boolean;
  page: number;
  page_size: number;
  total: number;
  items: Array<Record<string, unknown>>;
  stats?: Record<string, number>;
};

export async function fetchCpaAccounts(params: { page?: number; pageSize?: number; search?: string; status?: string } = {}) {
  const query = new URLSearchParams();
  query.set("page", String(params.page || 1));
  query.set("page_size", String(params.pageSize || 20));
  if (params.search) query.set("search", params.search);
  if (params.status) query.set("status", params.status);
  return httpRequest<{ cpa_accounts: CpaAccountList }>(`/api/cpa/accounts?${query.toString()}`);
}

export async function testCpaAccounts(names: string[] = [], applyPolicy = false) {
  return httpRequest<{ cpa_test: Record<string, unknown> }>("/api/cpa/accounts/test", { method: "POST", body: { names, apply_policy: applyPolicy } });
}

export async function actOnCpaAccounts(names: string[], action: "enable" | "disable" | "delete") {
  return httpRequest<{ cpa_action: Record<string, unknown> }>("/api/cpa/accounts/action", { method: "POST", body: { names, action } });
}

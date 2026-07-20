"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Check, CheckCircle2, ChevronDown, Copy, Database, ExternalLink, Filter, LoaderCircle, Pause, Play, RefreshCw, RotateCcw, Save, Search, Settings2, Square, Trash2, UserRound, X, Zap } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { SMSBOWER_PAYPAL_COUNTRIES } from "@/lib/paypal-country-catalog";
import {
  clearFailedPlusLinkTasks,
  clearFinishedPlusLinkTasks,
  clearPlusLinkTask,
  cancelPlusLinkTask,
  deletePlusLinkTask,
  fetchPlusLinkState,
  pausePlusLinkTask,
  resumePlusLinkTask,
  retryFailedPlusLinkTasks,
  retryPlusLinkTask,
  startPlusLinkTask,
  stopPlusLinkTask,
  updatePlusLinkConfig,
  type AccountItem,
  type PartialAccountItem,
  type PlusLinkAttemptDiagnostic,
  type PlusLinkConfig,
  type PlusLinkResult,
  type PlusLinkState,
} from "@/lib/api";

const BILLING_COUNTRIES = [
  { value: "US", label: "US 美国" },
  { value: "IN", label: "IN 印度" },
  { value: "EG", label: "EG 埃及" },
  { value: "ID", label: "ID 印度尼西亚" },
  { value: "TH", label: "TH 泰国" },
  { value: "MX", label: "MX 墨西哥" },
  { value: "CO", label: "CO 哥伦比亚" },
  { value: "ZA", label: "ZA 南非" },
  { value: "DE", label: "DE 德国" },
  { value: "FR", label: "FR 法国" },
  { value: "IT", label: "IT 意大利" },
  { value: "ES", label: "ES 西班牙" },
  { value: "NL", label: "NL 荷兰" },
  { value: "DK", label: "DK 丹麦" },
  { value: "CA", label: "CA 加拿大" },
  { value: "AU", label: "AU 澳大利亚" },
  { value: "NZ", label: "NZ 新西兰" },
  { value: "KR", label: "KR 韩国" },
  { value: "AE", label: "AE 阿联酋" },
  { value: "IL", label: "IL 以色列" },
  { value: "RO", label: "RO 罗马尼亚" },
  { value: "GB", label: "GB 英国" },
  { value: "BR", label: "BR 巴西" },
  { value: "JP", label: "JP 日本" },
  { value: "TR", label: "TR 土耳其" },
  { value: "VN", label: "VN 越南" },
] as const;
const PROVIDER_PM_COUNTRIES = SMSBOWER_PAYPAL_COUNTRIES;
const PROMO_COUNTRIES = [
  { value: "TH", label: "TH 泰国" },
  { value: "BR", label: "BR 巴西" },
  { value: "JP", label: "JP 日本" },
  { value: "TR", label: "TR 土耳其" },
  { value: "VN", label: "VN 越南" },
] as const;
const DEFAULT_BILLING_ROTATION = ["US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB"];
const DEFAULT_PROXY_ROTATION = ["JP", "TR", "BR", "VN"];
const PROXY_COUNTRY_CODES = PROVIDER_PM_COUNTRIES.map((country) => country.value);
const COUNTRY_LABEL = Object.fromEntries(PROVIDER_PM_COUNTRIES.map((country) => [country.value, country.label.slice(country.value.length).trim() || country.value])) as Record<string, string>;
const PAID_PLAN_TYPES = new Set(["plus", "pro", "team", "business", "enterprise"]);
const ACCOUNT_RENDER_BATCH = 50;
const PARTIAL_RENDER_BATCH = 50;
const TASK_RENDER_BATCH = 30;
const INVENTORY_PREVIEW_LIMIT = 100;

const defaultConfig: PlusLinkConfig = {
  engine_mode: "local",
  use_proxy_pool: false,
  use_residential_proxy_pool: true,
  residential_proxy_pool: "",
  residential_country_proxy_pool: {},
  residential_checkout_proxy: "",
  residential_provider_proxy: "",
  residential_approve_proxy: "",
  residential_promotion_proxy: "",
  residential_single_proxy_mode: false,
  residential_session_ttl_minutes: 5,
  proxy_quality_gate_enabled: true,
  proxy_quality_candidate_limit: 3,
  proxy_quality_probe_rounds: 2,
  proxy_quality_max_latency_ms: 12000,
  proxy_quality_quarantine_seconds: 300,
  residential_via_project_proxy: true,
  residential_parent_node_filter: "",
  residential_via_clash: true,
  residential_clash_proxy: "",
  proxy: "",
  target_country: "US",
  checkout_country: "US",
  provider_country: "BR",
  approve_country: "US",
  promotion_country: "BR",
  payment_method_country: "US",
  payment_locale: "en",
  checkout_ui_mode: "custom",
  post_approve_poll_timeout_seconds: 90,
  enable_playwright_approve_fallback: true,
  enable_playwright_redirect_fallback: true,
  allow_intermediate_url_fallback: false,
  concurrency: 5,
  timeout_seconds: 360,
  checkout_retry_limit: 10,
  provider_retry_limit: 15,
  matrix_combo_retry_limit: 1,
  smsbower_pm_max_price: 0.2,
  retry_limits_version: 2,
  refresh_tokens: false,
  provider_same_as_target: false,
  approve_same_as_checkout: true,
  country_rotation_enabled: true,
  billing_country_rotation: DEFAULT_BILLING_ROTATION,
  proxy_country_rotation: DEFAULT_PROXY_ROTATION,
  require_zero_stripe_amount: true,
  require_ipv6_egress: false,
  strict_same_proxy_ip: false,
  strict_approve_same_proxy_ip: false,
};

type StageProxyPreview = { checkoutCountry: string; providerCountry: string; approveCountry: string; promotionCountry: string; checkout: string; provider: string; approve: string; promotion: string };

function mergeConfig(config?: Partial<PlusLinkConfig>): PlusLinkConfig {
  const definedConfig = Object.fromEntries(Object.entries(config || {}).filter(([, value]) => value !== undefined)) as Partial<PlusLinkConfig>;
  const merged = { ...defaultConfig, ...definedConfig };
  const matrixEnabled = definedConfig.country_rotation_enabled !== false;
  const leftCountries = normalizeCodeSequence(merged.billing_country_rotation, DEFAULT_BILLING_ROTATION, PROVIDER_PM_COUNTRIES);
  const rightCountries = normalizeCodeSequence(merged.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES);
  const primaryLeft = leftCountries[0];
  const primaryRight = rightCountries[0];
  const checkoutCountry = normalizeCountry(merged.checkout_country, "US", BILLING_COUNTRIES);
  const checkoutBillingCountry = matrixEnabled ? checkoutCountry : normalizeCountry(merged.target_country, checkoutCountry, BILLING_COUNTRIES);
  const providerCountry = matrixEnabled ? primaryLeft : normalizeCountry(merged.provider_country, "BR", PROVIDER_PM_COUNTRIES);
  const approveCountry = matrixEnabled ? checkoutCountry : normalizeCountry(merged.approve_country, checkoutCountry, BILLING_COUNTRIES);
  const paymentMethodCountry = matrixEnabled ? primaryLeft : normalizeCountry(merged.payment_method_country, checkoutBillingCountry, PROVIDER_PM_COUNTRIES);
  const promotionCountry = matrixEnabled ? primaryRight : normalizeCountry(merged.promotion_country, "TR", PROMO_COUNTRIES);
  return {
    ...merged,
    target_country: checkoutBillingCountry,
    checkout_country: checkoutCountry,
    provider_country: providerCountry,
    approve_country: approveCountry,
    promotion_country: promotionCountry,
    payment_method_country: paymentMethodCountry,
    provider_retry_limit: Math.max(1, Number(merged.provider_retry_limit || defaultConfig.provider_retry_limit)),
    matrix_combo_retry_limit: Math.max(1, Number(merged.matrix_combo_retry_limit || defaultConfig.matrix_combo_retry_limit)),
    smsbower_pm_max_price: Math.max(0.001, Math.min(10, Number(merged.smsbower_pm_max_price || defaultConfig.smsbower_pm_max_price))),
    proxy_quality_gate_enabled: merged.proxy_quality_gate_enabled !== false,
    proxy_quality_candidate_limit: Math.max(1, Math.min(10, Number(merged.proxy_quality_candidate_limit || 3))),
    proxy_quality_probe_rounds: Math.max(1, Math.min(3, Number(merged.proxy_quality_probe_rounds || 2))),
    proxy_quality_max_latency_ms: Math.max(2000, Math.min(30000, Number(merged.proxy_quality_max_latency_ms || 12000))),
    proxy_quality_quarantine_seconds: Math.max(30, Math.min(3600, Number(merged.proxy_quality_quarantine_seconds || 300))),
    retry_limits_version: 2,
    country_rotation_enabled: matrixEnabled,
    billing_country_rotation: leftCountries,
    proxy_country_rotation: rightCountries,
    engine_mode: "local",
    use_proxy_pool: false,
    use_residential_proxy_pool: true,
    residential_via_project_proxy: merged.residential_via_project_proxy !== false,
    residential_via_clash: merged.residential_via_project_proxy !== false,
    residential_clash_proxy: "",
    proxy: "",
    provider_same_as_target: providerCountry !== checkoutCountry,
    approve_same_as_checkout: approveCountry === checkoutCountry,
    residential_country_proxy_pool: { ...((definedConfig.residential_country_proxy_pool || merged.residential_country_proxy_pool || {}) as Record<string, string>) },
  };
}
function lineCount(value?: string) {
  return String(value || "").replace(/\r/g, "\n").split("\n").map((line) => line.trim()).filter((line) => line && !line.startsWith("#")).length;
}
function normalizeCountryMap(value?: Record<string, string>) {
  const out: Record<string, string> = {};
  for (const code of PROXY_COUNTRY_CODES) {
    const text = String(value?.[code] || "").trim();
    if (text) out[code] = text;
  }
  return out;
}
function normalizeCodeSequence(value: unknown, defaults: string[], allowed: ReadonlyArray<{ value: string }>) {
  const allowedSet = new Set(allowed.map((item) => item.value));
  const raw = Array.isArray(value) ? value.map(String) : String(value || "").split(/[,;\s|/]+/);
  const items = raw.map((item) => item.trim().toUpperCase()).filter((item, index, all) => allowedSet.has(item) && all.indexOf(item) === index);
  return items.length ? items : defaults;
}
function normalizeCountry(value: unknown, fallback: string, allowed: ReadonlyArray<{ value: string }>) {
  return normalizeCodeSequence([String(value || "")], [fallback], allowed)[0];
}
function countryPool(map: Record<string, string> | undefined, country: string) {
  const code = String(country || "").toUpperCase();
  if (!map) return "";
  if (map[code]) return map[code];
  return "";
}
function computeStageProxies(config: PlusLinkConfig): StageProxyPreview {
  const map = normalizeCountryMap(config.residential_country_proxy_pool);
  const matrixEnabled = config.country_rotation_enabled !== false;
  const checkoutCountry = normalizeCountry(config.checkout_country, "US", BILLING_COUNTRIES);
  const providerCountry = matrixEnabled
    ? normalizeCodeSequence(config.billing_country_rotation, DEFAULT_BILLING_ROTATION, PROVIDER_PM_COUNTRIES)[0]
    : normalizeCountry(config.provider_country, "BR", PROVIDER_PM_COUNTRIES);
  const approveCountry = matrixEnabled ? checkoutCountry : normalizeCountry(config.approve_country, checkoutCountry, BILLING_COUNTRIES);
  const promotionCountry = matrixEnabled
    ? normalizeCodeSequence(config.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES)[0]
    : normalizeCountry(config.promotion_country, "TR", PROMO_COUNTRIES);
  const fallback = String(config.residential_proxy_pool || "").trim();
  const checkout = countryPool(map, checkoutCountry) || fallback;
  const provider = matrixEnabled
    ? countryPool(map, providerCountry) || fallback
    : providerCountry === checkoutCountry ? checkout : countryPool(map, providerCountry) || fallback;
  const approve = approveCountry === checkoutCountry ? checkout : approveCountry === providerCountry ? provider : countryPool(map, approveCountry) || fallback;
  const promotion = countryPool(map, promotionCountry) || fallback;
  return { checkoutCountry, providerCountry, approveCountry, promotionCountry, checkout, provider, approve, promotion };
}
function configForSave(config: PlusLinkConfig): PlusLinkConfig {
  const billingRotation = normalizeCodeSequence(config.billing_country_rotation, DEFAULT_BILLING_ROTATION, PROVIDER_PM_COUNTRIES);
  const proxyRotation = normalizeCodeSequence(config.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES);
  const normalized = mergeConfig({ ...config, billing_country_rotation: billingRotation, proxy_country_rotation: proxyRotation });
  const stage = computeStageProxies(normalized);
  const matrixComboRetryLimit = Math.max(1, Number(config.matrix_combo_retry_limit || 1));
  const viaProjectProxy = normalized.residential_via_project_proxy !== false;
  return { ...normalized, engine_mode: "local", use_proxy_pool: false, use_residential_proxy_pool: true, residential_via_project_proxy: viaProjectProxy, residential_via_clash: viaProjectProxy, residential_clash_proxy: "", proxy: "", residential_country_proxy_pool: normalizeCountryMap(config.residential_country_proxy_pool), residential_checkout_proxy: stage.checkout, residential_provider_proxy: stage.provider, residential_approve_proxy: stage.approve, residential_promotion_proxy: stage.promotion, provider_same_as_target: stage.providerCountry !== stage.checkoutCountry, approve_same_as_checkout: stage.approveCountry === stage.checkoutCountry, residential_single_proxy_mode: false, country_rotation_enabled: normalized.country_rotation_enabled !== false, billing_country_rotation: billingRotation, proxy_country_rotation: proxyRotation, checkout_retry_limit: Math.max(1, Number(config.checkout_retry_limit || 10)), provider_retry_limit: Math.max(1, Number(config.provider_retry_limit || 15)), matrix_combo_retry_limit: matrixComboRetryLimit, retry_limits_version: 2 };
}
function formatSeconds(value?: number) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0秒";
  if (n < 60) return `${n.toFixed(n % 1 ? 1 : 0)}秒`;
  return `${Math.floor(n / 60)}分${Math.round(n % 60)}秒`;
}
function statusText(status?: string) { return status === "success" ? "成功" : status === "failed" ? "失败" : status === "running" ? "运行中" : status === "queued" ? "排队中" : status === "paused" ? "已暂停" : status === "pausing" ? "暂停中" : status === "cancelling" ? "取消中" : status === "cancelled" ? "已取消" : status || "等待"; }
function statusVariant(status?: string) { return status === "success" ? "success" as const : status === "failed" || status === "cancelled" ? "danger" as const : status === "running" || status === "pausing" || status === "cancelling" ? "warning" as const : status === "queued" || status === "paused" ? "info" as const : "secondary" as const; }
function accountSelector(account: AccountItem) { return String(account.inventory_id || account.inventory_key || account.email || ""); }
function eligibleAccount(account: AccountItem) { return Boolean(account.has_access_token || account.has_refresh_token || account.access_token || account.refresh_token); }
function normalizePlanValue(value: unknown) {
  const text = String(value || "").trim().toLowerCase().replace(/[^a-z0-9_+-]+/g, "_").replace(/^_+|_+$/g, "");
  if (!text) return "";
  if (["chatgpt_plus", "paid", "premium"].includes(text)) return "plus";
  if (["teams", "edu", "education"].includes(text)) return "team";
  if (text.includes("enterprise")) return "enterprise";
  if (text.includes("business")) return "business";
  if (text.includes("team") || text.includes("edu")) return "team";
  if (text.includes("k12")) return "k12";
  if (text.includes("pro")) return "pro";
  if (text.includes("plus") || text.includes("paid") || text.includes("premium")) return "plus";
  if (["free", "free_plan", "none", "unknown"].includes(text)) return "free";
  return text;
}
function accountTypeText(value?: string) {
  const plan = normalizePlanValue(value);
  if (!plan || plan === "unknown") return "未知";
  return plan === "free" ? "Free" : plan === "plus" ? "Plus" : plan === "pro" ? "Pro" : plan === "team" ? "Team" : plan === "business" ? "Business" : plan === "enterprise" ? "Enterprise" : plan === "k12" ? "K12" : plan;
}
function tokenExpiryText(value?: number | string | null) {
  if (value === undefined || value === null || value === "") return "未知";
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(String(value));
  if (Number.isNaN(date.getTime())) return "未知";
  const formatted = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  const remainingMs = date.getTime() - Date.now();
  if (remainingMs <= 0) return `已过期 · ${formatted}`;
  const remainingDays = Math.ceil(remainingMs / 86_400_000);
  return remainingDays > 1 ? `${formatted} · ${remainingDays}天` : formatted;
}
function asRecord(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function accountHasK12Marker(account: AccountItem) {
  return normalizePlanValue(account.plan_type) === "k12" || normalizePlanValue(account.account_type) === "k12" || Boolean(account.k12_mode) || Boolean(String(account.k12_join_status || "").trim()) || Boolean(String(account.k12_team_id || "").trim());
}
function modelCount(value: unknown) { const n = Number(value || 0); return Number.isFinite(n) ? n : 0; }
function strongModelAccessPlan(account: AccountItem) {
  for (const rootKey of ["quota_details", "last_health_result"]) {
    const root = asRecord(account[rootKey]);
    const typeRefresh = asRecord(root.type_refresh);
    const typePlan = normalizePlanValue(typeRefresh.plan_type);
    if (PAID_PLAN_TYPES.has(typePlan)) return typePlan;
    const endpoints = Array.isArray(typeRefresh.endpoints) ? typeRefresh.endpoints : [];
    for (const endpointValue of endpoints) {
      const endpoint = asRecord(endpointValue);
      const inferred = normalizePlanValue(endpoint.model_inferred_plan);
      if (PAID_PLAN_TYPES.has(inferred)) return inferred;
      const endpointPlan = normalizePlanValue(endpoint.plan_type);
      if (PAID_PLAN_TYPES.has(endpointPlan)) return endpointPlan;
      if (modelCount(endpoint.available_model_count) >= 12) return "plus";
    }
    const models = asRecord(root.models);
    if (modelCount(models.available_model_count) >= 12) return "plus";
  }
  return "";
}
function accountEffectivePlan(account: AccountItem) {
  const backendPlan = normalizePlanValue(account.effective_plan_type || account.display_plan_type);
  if (backendPlan) return backendPlan;
  if (accountHasK12Marker(account)) return "k12";
  for (const value of [account.plan_type, account.account_type]) {
    const plan = normalizePlanValue(value);
    if (PAID_PLAN_TYPES.has(plan)) return plan;
  }
  const modelPlan = strongModelAccessPlan(account);
  if (modelPlan) return modelPlan;
  for (const value of [account.account_type, account.plan_type]) {
    const plan = normalizePlanValue(value);
    if (plan && !["free", "none", "unknown"].includes(plan)) return plan;
  }
  return "free";
}
function isPlusAccount(account: AccountItem) {
  if (typeof account.is_plus_for_filter === "boolean") return account.is_plus_for_filter;
  return PAID_PLAN_TYPES.has(accountEffectivePlan(account));
}
function planBadgeVariant(plan: string) {
  if (PAID_PLAN_TYPES.has(plan)) return "violet" as const;
  if (plan === "k12") return "info" as const;
  return "secondary" as const;
}
function mailboxGroupLabel(account: { mailbox_group?: unknown }) { return String(account.mailbox_group || "默认"); }
function mailboxGroupOptions<T extends { mailbox_group?: unknown }>(items: T[]) {
  return Array.from(new Set(["默认", ...items.map(mailboxGroupLabel)])).filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
}
function accountSearchText(account: AccountItem) { const effectivePlan = accountEffectivePlan(account); return [account.email, effectivePlan, account.plan_type, account.account_type, account.oauth_status, account.token_source, account.note, mailboxGroupLabel(account)].map((item) => String(item || "")).join(" ").toLowerCase(); }
function partialSearchText(account: PartialAccountItem) { return [account.email, account.status, account.stage, account.reason, account.note, mailboxGroupLabel(account)].map((item) => String(item || "")).join(" ").toLowerCase(); }
function matchesQuery(text: string, query: string) { const q = query.trim().toLowerCase(); return !q || q.split(/\s+/).every((part) => text.includes(part)); }
function partialSelector(account: PartialAccountItem) { return String(account.id || account.email || ""); }
function eligiblePartialAccount(account: PartialAccountItem) { return Boolean(account.has_access_token || account.has_refresh_token || account.has_session_token || account.access_token || account.refresh_token || account.session_token); }
function selectedToggle(list: string[], value: string, checked: boolean) { const clean = String(value || "").trim(); return !clean ? list : checked ? Array.from(new Set([...list, clean])) : list.filter((item) => item !== clean); }
function manualLines(text: string) { return String(text || "").replace(/\r/g, "\n").split("\n").map((line) => line.trim()).filter(Boolean); }
function copyText(text: string, message = "已复制") { void navigator.clipboard.writeText(text).then(() => toast.success(message), () => toast.error("复制失败")); }
function saveTextFile(name: string, text: string) { const blob = new Blob([text], { type: "text/plain;charset=utf-8" }); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url); }
function progressValue(result?: PlusLinkResult) {
  if (result?.status === "success") return 100;
  return Math.max(0, Math.min(100, Math.round(Number(result?.progress_percent || 0))));
}
function matrixCountries(result?: PlusLinkResult) {
  return {
    checkout: result?.matrix_checkout_country || result?.checkout_proxy_country || result?.billing_country || "--",
    provider: result?.matrix_provider_country || result?.provider_country || result?.provider_proxy_country || "--",
    promotion: result?.matrix_promotion_country || result?.promotion_country || result?.promotion_proxy_country || result?.right_proxy_country || "--",
  };
}
function matrixPool(result: PlusLinkResult | undefined, side: "left" | "right") {
  if (side === "left") return { country: result?.checkout_proxy_country || result?.approve_country || result?.billing_country || result?.left_proxy_country || "--", ip: result?.approve_proxy_ip || result?.checkout_proxy_ip || result?.left_proxy_ip || "等待检测" };
  return { country: result?.right_proxy_country || result?.promotion_country || "--", ip: result?.right_proxy_ip || "等待检测" };
}
function taskProxyDisplay(result: PlusLinkResult) {
  const step = String(result.engine_step || "").toLowerCase();
  const proxyStage = String(result.proxy_stage || "").toLowerCase();
  const checkoutSteps = new Set(["checkout"]);
  const providerSteps = new Set(["provider", "proxy", "stripe_init", "stripe_methods", "payment_method", "stripe_amount", "confirm", "stripe_redirect", "redirect"]);
  const approveSteps = new Set(["approve"]);
  const rightFallback = matrixPool(result, "right");
  const right = {
    country: result.promotion_proxy_country || rightFallback.country,
    ip: result.promotion_proxy_ip || rightFallback.ip,
  };
  const matrix = matrixCountries(result);

  if (step === "quality_gate") {
    const checkoutGate = result.quality_gate_results?.find((item) => item.stage === "checkout");
    const providerGate = result.quality_gate_results?.find((item) => item.stage === "provider");
    const promotionGate = result.quality_gate_results?.find((item) => item.stage === "promotion");
    return {
      primary: { label: "Checkout", country: checkoutGate?.country || matrix.checkout, ip: checkoutGate?.ip || "等待检测" },
      secondary: { label: `Provider ${providerGate?.country || matrix.provider} / 优惠`, country: promotionGate?.country || matrix.promotion, ip: `${providerGate?.ip || "等待检测"} / ${promotionGate?.ip || "等待检测"}` },
    };
  }

  if (result.extraction_mode === "matrix" && step === "preparing") {
    return {
      primary: { label: "Checkout/Approve", country: matrix.checkout, ip: "等待检测" },
      secondary: { label: `Provider ${matrix.provider} / 优惠`, country: matrix.promotion, ip: "等待检测" },
    };
  }

  if (checkoutSteps.has(step) || (!step && proxyStage === "checkout")) {
    return {
      primary: { label: "Checkout", country: result.checkout_proxy_country || result.billing_country || "--", ip: result.checkout_proxy_ip || "等待检测" },
      secondary: { label: "Promotion/Taxes", ...right },
    };
  }
  if (approveSteps.has(step) || proxyStage === "approve") {
    const left = matrixPool(result, "left");
    return { primary: { label: "Approve", country: result.approve_proxy_country || result.approve_country || left.country, ip: result.approve_proxy_ip || left.ip }, secondary: { label: "Promotion/Taxes", ...right } };
  }
  if (providerSteps.has(step) || proxyStage === "provider") {
    return {
      primary: {
        label: "Provider/PM/Poll",
        country: result.provider_proxy_country || result.provider_country || matrix.provider,
        ip: result.provider_proxy_ip || "等待检测",
      },
      secondary: { label: "Promotion/Taxes", ...right },
    };
  }
  const left = matrixPool(result, "left");
  return { primary: { label: "主链路", ...left }, secondary: { label: "Promotion/Taxes", ...right } };
}
function taskDurationSeconds(result?: PlusLinkResult) { const fixed = Number(result?.duration_seconds || 0); if (fixed > 0 || !result?.started_at) return fixed; const started = new Date(result.started_at).getTime(); return Number.isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : 0; }
function taskDurationText(result?: PlusLinkResult) { return `${taskDurationSeconds(result).toFixed(1)}s`; }
function taskStartTimeText(result?: PlusLinkResult) {
  const raw = result?.started_at || result?.created_at;
  if (!raw) return "--";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return String(raw);
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
  return result?.started_at ? formatted : `${formatted}（排队）`;
}
function shortTaskId(taskId?: string) { const value = String(taskId || ""); return value.length > 16 ? value.slice(-16) : value || "--"; }
function taskAccent(status?: string) { return status === "success" ? "border-l-emerald-500" : status === "failed" || status === "cancelled" ? "border-l-rose-500" : status === "paused" || status === "pausing" ? "border-l-amber-500" : "border-l-sky-500"; }
function taskProgressColor(status?: string) { return status === "success" ? "bg-emerald-500" : status === "failed" || status === "cancelled" ? "bg-rose-400" : status === "paused" || status === "pausing" ? "bg-amber-400" : "bg-sky-500"; }
function paypalAvailabilityText(value?: string) { return value === "supported" ? "PayPal 可用" : value === "unsupported" ? "PayPal 不可用" : value === "unknown" ? "PayPal 未确认" : ""; }
const ATTEMPT_FAILURE_LABEL: Record<string, string> = {
  checkout_blocked: "403 拦截",
  checkout_network: "网络/出口失败",
  promotion_update: "优惠更新 403",
  taxes_update: "税区更新失败",
  generic_decline: "支付通道拒绝",
  stripe_submission: "Stripe 状态失败",
  stripe_confirm: "Stripe Confirm 失败",
  stripe_payment_method: "PayPal PM 创建失败",
  stripe_init: "Stripe 初始化失败",
  stripe_amount_nonzero: "当前金额非 0",
  stripe_amount_unknown: "金额读取失败",
  chatgpt_approve: "Approve 失败",
  approve_blocked: "Approve 被拦截",
  paypal_redirect: "BA 跳转解析失败",
  paypal_unsupported: "PayPal 不可用",
  proxy_network: "代理网络异常",
  proxy_ip_mismatch: "出口 IP 不一致",
  proxy_country_mismatch: "出口国家不一致",
  proxy_ip_family_mismatch: "出口 IP 类型不符",
  proxy_quality_gate: "候选链路淘汰",
};
const ATTEMPT_STEP_LABEL: Record<string, string> = {
  quality_gate: "三出口闸门",
  checkout: "Checkout",
  sync: "优惠/税区",
  provider: "Provider",
  proxy: "出口检测",
  stripe_init: "Stripe init",
  stripe_methods: "支付方式",
  payment_method: "PayPal PM",
  stripe_amount: "金额校验",
  confirm: "Confirm",
  approve: "Approve",
  stripe_redirect: "Stripe Poll",
  redirect: "BA 跳转",
};
function attemptFailureLabel(item: PlusLinkAttemptDiagnostic) {
  let category = String(item.failure_category || "");
  if (!category || category === "unknown") {
    const error = String(item.error || "").toLowerCase();
    if (error.includes("checkout") && ["403", "forbidden", "拦截", "风控"].some((marker) => error.includes(marker))) category = "checkout_blocked";
    else if (error.includes("checkout") && ["检测失败", "tls", "网络", "timeout", "connection"].some((marker) => error.includes(marker))) category = "checkout_network";
  }
  return ATTEMPT_FAILURE_LABEL[category] || category || "未分类";
}
function attemptStepLabel(item: PlusLinkAttemptDiagnostic) { return ATTEMPT_STEP_LABEL[String(item.failure_step || "")] || String(item.failure_step || "未知步骤"); }
function attemptRouteText(item: PlusLinkAttemptDiagnostic) {
  const checkoutCountry = item.checkout_country || item.approve_country || "--";
  const providerCountry = item.provider_country || item.payment_method_country || "--";
  const promotionCountry = item.promotion_country || "--";
  const checkoutIp = item.approve_proxy_ip || item.checkout_proxy_ip || "待记录";
  const providerIp = item.provider_proxy_ip || "待记录";
  const promotionIp = item.promotion_proxy_ip || "待记录";
  return `Checkout ${checkoutCountry} ${checkoutIp} → Provider ${providerCountry} ${providerIp} → 优惠 ${promotionCountry} ${promotionIp}`;
}
function taskStageTitle(result: PlusLinkResult) {
  const left = matrixPool(result, "left").country;
  const checkout = result.checkout_proxy_country || left;
  const right = matrixPool(result, "right").country;
  const matrix = matrixCountries(result);
  const billing = result.billing_country || matrix.checkout;
  const provider = result.provider_country || matrix.provider;
  const approve = result.approve_country || matrix.checkout;
  const promotion = result.promotion_country || matrix.promotion;
  const titles: Record<string, string> = {
    queued: "等待执行",
    preparing: `Checkout ${matrix.checkout} · Provider ${matrix.provider} · 优惠 ${matrix.promotion}`,
    refresh: "刷新 OAuth 令牌",
    eligibility: "检测账号提链资格",
    quality_gate: `三出口质量检测 · 候选第 ${result.quality_gate_attempt || 1} 组`,
    auto_retry: `自动重试 · 第 ${result.auto_retry_count || 1} 轮`,
    run: "启动提链链路",
    billing: `${billing} 账单资料`,
    checkout: `${checkout} Checkout`,
    sync: `${promotion} 优惠更新`,
    link_attempt: "开始本轮提链",
    provider: `${provider} Provider 会话`,
    proxy: `${provider} + ${promotion} 出口检测`,
    stripe_init: "主链路 Stripe init",
    stripe_methods: "确认 PayPal 支付方式",
    payment_method: "创建 PayPal 支付方式",
    stripe_amount: "校验 Stripe 金额",
    confirm: "确认 Stripe Checkout",
    approve: `${approve} ChatGPT approve`,
    stripe_redirect: `${provider} Stripe poll`,
    redirect: `${provider} 最终跳转`,
    combo_result: "本轮链路结束",
    done: "提链完成",
  };
  if (result.status === "success") return "完成";
  if (result.status === "failed") return "提链失败";
  if (result.status === "cancelled") return "任务已停止";
  if (result.status === "paused" || result.status === "pausing") return `已暂停 · ${titles[result.engine_step || ""] || result.progress_label || "当前步骤"}`;
  return titles[result.engine_step || ""] || result.progress_label || result.stage || "等待开始";
}
function taskStageDescription(result: PlusLinkResult) {
  if (result.cleanup_pending) return "任务已终止；当前网络请求正在后台释放，不会再写回任务结果。";
  const left = matrixPool(result, "left");
  const checkout = result.checkout_proxy_country || left.country;
  const right = matrixPool(result, "right");
  const matrix = matrixCountries(result);
  const billing = result.billing_country || matrix.checkout;
  const provider = result.provider_country || matrix.provider;
  const approve = result.approve_country || matrix.checkout;
  const promotion = result.promotion_country || matrix.promotion;
  const attempt = Number(result.link_attempt_count || 0);
  const limit = Number(result.link_attempt_limit || 0);
  if (result.status === "success") return `第 ${Math.max(1, attempt)} 次提链成功，长链已经生成。`;
  if (result.status === "failed") return result.error || "本轮链路未能生成长链，可直接重试该任务。";
  if (result.status === "cancelled") return "任务已停止，可保留当前记录或重新发起提链。";
  if (result.status === "paused" || result.status === "pausing") return "当前任务已暂停，继续后将从当前链路检查点恢复。";
  if (result.engine_step === "eligibility" && result.eligibility_last_error) {
    return `资格检测第 ${result.eligibility_attempt || 1} 次遇到临时错误，正在自动重试：${result.eligibility_last_error}`;
  }
  if (result.engine_step === "quality_gate" && result.quality_gate_error) {
    return `候选第 ${result.quality_gate_attempt || 1} 组未通过，正在自动更换：${result.quality_gate_error}`;
  }
  const descriptions: Record<string, string> = {
    queued: "等待工作线程接管该账号。",
    preparing: `正在分配三个出口：Checkout/Approve/账单 ${matrix.checkout}，Provider/PM/Poll/Redirect ${matrix.provider}，Promotion/Taxes ${matrix.promotion}。`,
    refresh: "正在刷新账号令牌，完成后进入 Checkout。",
    eligibility: `正在检测账号资格并分配 Checkout 父代理（第 ${result.eligibility_attempt || 1} 次，临时错误会持续自动重试）。`,
    quality_gate: `正在依次验证 Checkout ${matrix.checkout}、Provider ${matrix.provider}、Promotion ${matrix.promotion} 的国家、粘性、目标可达性与延迟。`,
    auto_retry: `上一轮属于可恢复失败，正在等待自动重试第 ${result.auto_retry_count || 1} 轮；无需手动点击重新开始。`,
    run: "账号和代理已经准备完成，正在启动提链。",
    billing: `正在生成 ${billing} 随机账单资料。`,
    checkout: `正在通过 Checkout 出口 ${checkout} 创建 PayPal Checkout。`,
    sync: `保留同一 Checkout，正在通过 ${promotion} 更新优惠和税区。`,
    link_attempt: `正在执行第 ${Math.max(1, attempt)}/${Math.max(1, limit)} 次提链尝试。`,
    provider: `正在通过 ${provider} 创建 Stripe/Provider 会话。`,
    proxy: `正在验证 Provider ${provider} 与 Promotion/Taxes ${promotion} 的实际出口。`,
    stripe_init: "优惠已更新，正在回到主链路初始化 Stripe。",
    stripe_methods: "正在确认当前 Stripe 会话包含 PayPal 支付方式。",
    payment_method: "正在创建 PayPal payment method。",
    stripe_amount: "正在确认当前 Checkout 金额符合提链条件。",
    confirm: "正在绑定支付方式并确认 Stripe Checkout。",
    approve: `正在通过 ${approve} 请求 ChatGPT approve。`,
    stripe_redirect: `正在通过 Provider ${provider} 轮询 Stripe 跳转结果。`,
    redirect: `正在通过 Provider ${provider} 解析最终 PayPal BA approve 长链。`,
    combo_result: "本轮组合已结束，失败时将自动进入下一次尝试。",
    done: "已获得可复制和打开的长链。",
  };
  return descriptions[result.engine_step || ""] || "正在执行当前提链步骤。";
}

function CountrySelect({ value, options, onChange, disabled = false }: { value: string; options: ReadonlyArray<{ value: string; label: string }>; onChange: (value: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const listboxId = useId();
  const selected = options.find((item) => item.value === value) || options[0];
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const visibleOptions = normalizedQuery
    ? options.filter((item) => `${item.value} ${item.label}`.toLocaleLowerCase("zh-CN").includes(normalizedQuery))
    : options;

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePress);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePress);
  }, [open]);

  useEffect(() => { setActiveIndex(0); }, [query, open]);

  const choose = (country: string) => {
    onChange(country);
    setOpen(false);
    setQuery("");
  };
  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (!visibleOptions.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => (current + 1) % visibleOptions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => (current - 1 + visibleOptions.length) % visibleOptions.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(visibleOptions[Math.min(activeIndex, visibleOptions.length - 1)].value);
    }
  };

  return <div ref={rootRef} className="relative min-w-0">
    <Button variant="outline" className="h-11 w-full justify-between rounded-2xl px-4 font-normal" disabled={disabled} aria-haspopup="listbox" aria-expanded={open} aria-controls={listboxId} onClick={() => setOpen((current) => !current)}>
      <span className="min-w-0 truncate text-left">{selected?.label || value}</span>
      <ChevronDown className={`size-4 shrink-0 text-slate-500 transition-transform ${open ? "rotate-180" : ""}`} />
    </Button>
    {open ? <div className="absolute top-[calc(100%+0.375rem)] right-0 left-0 z-[130] overflow-hidden rounded-2xl border border-slate-200 bg-white p-2 shadow-[0_20px_60px_-30px_rgba(15,23,42,0.45)]">
      <div className="relative mb-2">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-slate-400" />
        <Input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={handleSearchKeyDown} className="pl-9" placeholder="搜索国家代码或中文名" aria-label="搜索国家代码或中文名" />
      </div>
      <div id={listboxId} role="listbox" className="max-h-72 space-y-1 overflow-y-auto pr-1">
        {visibleOptions.length ? visibleOptions.map((item, index) => <button key={item.value} type="button" role="option" aria-selected={item.value === value} className={`flex min-h-10 w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left text-sm transition ${index === activeIndex ? "bg-emerald-50 text-emerald-900" : "text-slate-700 hover:bg-slate-50"}`} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(item.value)}>
          <span className="min-w-0 break-words">{item.label}</span>
          {item.value === value ? <Check className="size-4 shrink-0" /> : null}
        </button>) : <div className="px-3 py-8 text-center text-sm text-slate-500">没有匹配国家</div>}
      </div>
    </div> : null}
  </div>;
}

function CountryMultiSelect({ value, defaults, options, onChange, disabled = false, compact = false }: { value: unknown; defaults: string[]; options: ReadonlyArray<{ value: string; label: string }>; onChange: (value: string[]) => void; disabled?: boolean; compact?: boolean }) {
  const [query, setQuery] = useState("");
  const selected = normalizeCodeSequence(value, defaults, options);
  const normalizedQuery = query.trim().toLowerCase();
  const visibleOptions = normalizedQuery
    ? options.filter((item) => `${item.value} ${item.label}`.toLowerCase().includes(normalizedQuery))
    : options;
  const toggle = (country: string, checked: boolean) => {
    const next = checked ? [...selected, country] : selected.filter((item) => item !== country);
    if (next.length) onChange(Array.from(new Set(next)));
  };
  return <div className="space-y-2">
    <Input value={query} disabled={disabled} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索 ${options.length} 个国家`} />
    <div className={`grid max-h-72 gap-2 overflow-y-auto pr-1 ${compact ? "grid-cols-2" : "grid-cols-2 sm:grid-cols-3 xl:grid-cols-4"}`}>
      {visibleOptions.map((item) => <label key={item.value} className={`flex min-h-10 min-w-0 items-center gap-2 rounded-md border px-2.5 py-2 text-xs font-semibold transition ${selected.includes(item.value) ? "border-emerald-300 bg-emerald-50 text-emerald-900" : "border-slate-200 bg-white text-slate-600"}`}><Checkbox checked={selected.includes(item.value)} disabled={disabled} onCheckedChange={(checked) => toggle(item.value, checked === true)} /><span className="min-w-0 break-words leading-4">{item.label}</span></label>)}
    </div>
  </div>;
}
function FieldLabel({ children, hint }: { children: ReactNode; hint?: string }) { return <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>{children}</span>{hint ? <span className="block text-xs font-normal text-slate-500">{hint}</span> : null}</label>; }

function mergePlusLinkState(current: PlusLinkState | null, next: PlusLinkState): PlusLinkState {
  if (!current) return next;
  return {
    ...current,
    ...next,
    account_pool: next.account_pool ?? current.account_pool,
    partial_account_pool: next.partial_account_pool ?? current.partial_account_pool,
    proxy_pool: next.proxy_pool ?? current.proxy_pool,
  };
}

export function PlusLinkCard({ active = true, activityRef }: { active?: boolean; activityRef?: { current: boolean } }) {
  const [state, setState] = useState<PlusLinkState | null>(null);
  const [draft, setDraft] = useState<PlusLinkConfig>(() => mergeConfig());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [restartingAll, setRestartingAll] = useState(false);
  const [selectedEmails, setSelectedEmails] = useState<string[]>([]);
  const [selectedPartials, setSelectedPartials] = useState<string[]>([]);
  const [manualText, setManualText] = useState("");
  const [accountQuery, setAccountQuery] = useState("");
  const [partialQuery, setPartialQuery] = useState("");
  const [accountGroupFilter, setAccountGroupFilter] = useState("all");
  const [partialGroupFilter, setPartialGroupFilter] = useState("all");
  const [showPlusAccounts, setShowPlusAccounts] = useState(false);
  const [accountVisibleLimit, setAccountVisibleLimit] = useState(ACCOUNT_RENDER_BATCH);
  const [partialVisibleLimit, setPartialVisibleLimit] = useState(PARTIAL_RENDER_BATCH);
  const [taskVisibleLimit, setTaskVisibleLimit] = useState(TASK_RENDER_BATCH);
  const [taskActionId, setTaskActionId] = useState("");
  const [temporaryConfigOpen, setTemporaryConfigOpen] = useState(false);
  const [temporaryCountryMode, setTemporaryCountryMode] = useState<"last" | "default" | "custom">("default");
  const [temporaryExtractionMode, setTemporaryExtractionMode] = useState<"fixed" | "matrix">("fixed");
  const [temporaryCheckoutCountry, setTemporaryCheckoutCountry] = useState("US");
  const [temporaryLeftCountries, setTemporaryLeftCountries] = useState<string[]>([]);
  const [temporaryRightCountries, setTemporaryRightCountries] = useState<string[]>([]);
  const [temporarySmsPriceLimit, setTemporarySmsPriceLimit] = useState("0.2");
  const draftDirtyRef = useRef(false);
  const audioContextRef = useRef<AudioContext | null>(null);
  const successSoundArmedRef = useRef(false);
  const knownSuccessTaskIdsRef = useRef<Set<string>>(new Set());
  const loadInFlightRef = useRef<Promise<void> | null>(null);

  const stats = state?.stats || { success: 0, fail: 0, done: 0, running: 0, total: 0, elapsed_seconds: 0 };
  const accounts = useMemo(() => state?.account_pool?.accounts || [], [state]);
  const partialAccounts = useMemo(() => state?.partial_account_pool?.items || [], [state]);
  const eligibleAccounts = useMemo(() => accounts.filter(eligibleAccount), [accounts]);
  const accountGroups = useMemo(() => mailboxGroupOptions(eligibleAccounts), [eligibleAccounts]);
  const filteredEligibleAccounts = useMemo(() => eligibleAccounts.filter((account) => {
    const group = mailboxGroupLabel(account);
    return (accountGroupFilter === "all" || group === accountGroupFilter) && (showPlusAccounts || !isPlusAccount(account)) && matchesQuery(accountSearchText(account), accountQuery);
  }), [accountGroupFilter, eligibleAccounts, showPlusAccounts, accountQuery]);
  const hiddenPaidAccounts = useMemo(() => eligibleAccounts.filter(isPlusAccount).length, [eligibleAccounts]);
  const eligiblePartials = useMemo(() => partialAccounts.filter(eligiblePartialAccount), [partialAccounts]);
  const partialGroups = useMemo(() => mailboxGroupOptions(eligiblePartials), [eligiblePartials]);
  const filteredEligiblePartials = useMemo(() => eligiblePartials.filter((account) => {
    const group = mailboxGroupLabel(account);
    return (partialGroupFilter === "all" || group === partialGroupFilter) && matchesQuery(partialSearchText(account), partialQuery);
  }), [eligiblePartials, partialGroupFilter, partialQuery]);
  const visibleEligibleAccounts = useMemo(() => filteredEligibleAccounts.slice(0, accountVisibleLimit), [accountVisibleLimit, filteredEligibleAccounts]);
  const visibleEligiblePartials = useMemo(() => filteredEligiblePartials.slice(0, partialVisibleLimit), [filteredEligiblePartials, partialVisibleLimit]);
  const selectedEmailSet = useMemo(() => new Set(selectedEmails), [selectedEmails]);
  const selectedPartialSet = useMemo(() => new Set(selectedPartials), [selectedPartials]);
  const leftCountries = useMemo(() => normalizeCodeSequence(draft.billing_country_rotation, DEFAULT_BILLING_ROTATION, PROVIDER_PM_COUNTRIES), [draft.billing_country_rotation]);
  const smsbowerPmPriceLimit = Math.max(0.001, Math.min(10, Number(draft.smsbower_pm_max_price || 0.2)));
  const smsbowerPriceCountries = useMemo(
    () => PROVIDER_PM_COUNTRIES.filter((country) => country.smsAvailable > 0 && country.smsPrice <= smsbowerPmPriceLimit),
    [smsbowerPmPriceLimit],
  );
  const rightCountries = useMemo(() => normalizeCodeSequence(draft.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES), [draft.proxy_country_rotation]);
  const matrixEnabled = draft.country_rotation_enabled !== false;
  const comboTotal = matrixEnabled ? leftCountries.length * rightCountries.length : 1;
  const normalizedTemporarySmsPriceLimit = Math.max(0.001, Math.min(10, Number(temporarySmsPriceLimit || smsbowerPmPriceLimit)));
  const temporarySmsPriceCountries = useMemo(
    () => PROVIDER_PM_COUNTRIES.filter((country) => country.smsAvailable > 0 && country.smsPrice <= normalizedTemporarySmsPriceLimit),
    [normalizedTemporarySmsPriceLimit],
  );
  const temporaryComboTotal = temporaryExtractionMode === "matrix" ? temporaryLeftCountries.length * temporaryRightCountries.length : 1;
  const stagePreview = useMemo(() => computeStageProxies(draft), [draft]);
  const visibleProxyCountries = useMemo(() => {
    const activeCountries = new Set(matrixEnabled
      ? [stagePreview.checkoutCountry, ...leftCountries, ...rightCountries]
      : [stagePreview.checkoutCountry, stagePreview.providerCountry, stagePreview.approveCountry, stagePreview.promotionCountry]);
    return PROXY_COUNTRY_CODES.filter((code) => activeCountries.has(code) || Boolean(String(draft.residential_country_proxy_pool?.[code] || "").trim()));
  }, [draft.residential_country_proxy_pool, leftCountries, matrixEnabled, rightCountries, stagePreview]);
  const manualTokenLines = useMemo(() => manualLines(manualText), [manualText]);
  const selectedCount = selectedEmails.length + selectedPartials.length + manualTokenLines.length;
  const successLinks = useMemo(() => (state?.results || []).filter((item) => item.status === "success" && item.long_url), [state]);
  const restartableTaskCount = useMemo(() => (state?.results || []).filter((item) => (
    (item.status === "failed" || item.status === "cancelled")
    && item.retryable !== false
    && !item.cleanup_pending
  )).length, [state?.results]);
  const latestTaskCountryConfig = useMemo(() => {
    const latest = [...(state?.results || [])]
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
      .find((item) => Array.isArray(item.task_left_countries) && item.task_left_countries.length && Array.isArray(item.task_right_countries) && item.task_right_countries.length);
    if (!latest) return null;
    const extractionMode = latest.extraction_mode === "matrix" ? "matrix" as const : "fixed" as const;
    return {
      extractionMode,
      checkout: normalizeCountry(latest.task_checkout_country || latest.checkout_proxy_country || latest.billing_country, "US", BILLING_COUNTRIES),
      provider: normalizeCountry(latest.provider_country || latest.payment_method_country, "US", PROVIDER_PM_COUNTRIES),
      promotion: normalizeCountry(latest.promotion_country, "TR", PROMO_COUNTRIES),
      left: normalizeCodeSequence(latest.task_left_countries, [], PROVIDER_PM_COUNTRIES),
      right: normalizeCodeSequence(latest.task_right_countries, [], PROMO_COUNTRIES),
    };
  }, [state?.results]);

  const armSuccessSound = () => {
    const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) return;
    const context = audioContextRef.current || new AudioContextCtor();
    audioContextRef.current = context;
    successSoundArmedRef.current = true;
    void context.resume();
  };
  const playSuccessSound = () => {
    const context = audioContextRef.current;
    if (!context) return;
    const start = context.currentTime;
    for (const [offset, frequency] of [[0, 660], [0.13, 880]] as const) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(frequency, start + offset);
      gain.gain.setValueAtTime(0.0001, start + offset);
      gain.gain.exponentialRampToValueAtTime(0.18, start + offset + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.18);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(start + offset);
      oscillator.stop(start + offset + 0.2);
    }
  };

  const load = async (silent = false, includeInventory = true, previewInventory = false) => {
    if (loadInFlightRef.current) {
      if (silent) return;
      await loadInFlightRef.current;
    }
    if (!silent) setLoading(true);
    const pending = (async () => {
      try {
        const payload = await fetchPlusLinkState({ includeInventory, inventoryLimit: previewInventory ? INVENTORY_PREVIEW_LIMIT : 0, compact: !includeInventory });
        setState((current) => mergePlusLinkState(current, payload.plus_link));
        if (!draftDirtyRef.current) setDraft(mergeConfig(payload.plus_link.config));
        if (includeInventory) {
          const validEmailSelectors = new Set((payload.plus_link.account_pool?.accounts || []).map(accountSelector));
          setSelectedEmails((items) => items.filter((item) => validEmailSelectors.has(item)));
          const validPartialSelectors = new Set((payload.plus_link.partial_account_pool?.items || []).map(partialSelector));
          setSelectedPartials((items) => items.filter((item) => validPartialSelectors.has(item)));
        }
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Plus 提链状态刷新失败");
      } finally {
        if (!silent) setLoading(false);
      }
    })();
    loadInFlightRef.current = pending;
    try {
      await pending;
    } finally {
      if (loadInFlightRef.current === pending) loadInFlightRef.current = null;
    }
  };

  useEffect(() => {
    void load(false, true, true);
  }, []);

  useEffect(() => {
    const intervalMs = state?.runner_alive ? 2500 : 15000;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      const isActive = activityRef ? activityRef.current : active;
      if (isActive) await load(true, false);
      if (!cancelled) timer = window.setTimeout(() => void poll(), isActive ? intervalMs : 250);
    };
    timer = window.setTimeout(() => void poll(), (activityRef ? activityRef.current : active) ? intervalMs : 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [active, activityRef, state?.runner_alive]);

  useEffect(() => setAccountVisibleLimit(ACCOUNT_RENDER_BATCH), [accountGroupFilter, accountQuery, showPlusAccounts]);
  useEffect(() => setPartialVisibleLimit(PARTIAL_RENDER_BATCH), [partialGroupFilter, partialQuery]);

  useEffect(() => {
    if (!successSoundArmedRef.current) return;
    const successIds = new Set((state?.results || []).filter((item) => item.status === "success").map((item) => item.task_id));
    const hasNewSuccess = Array.from(successIds).some((taskId) => !knownSuccessTaskIdsRef.current.has(taskId));
    knownSuccessTaskIdsRef.current = successIds;
    if (hasNewSuccess) playSuccessSound();
  }, [state?.results]);

  const patchDraft = (updates: Partial<PlusLinkConfig>) => { draftDirtyRef.current = true; setDraft((current) => mergeConfig({ ...current, ...updates })); };
  const applySmsbowerPriceCountries = () => {
    if (!smsbowerPriceCountries.length) {
      toast.warning(`当前 SMS 价格快照中没有不高于 $${smsbowerPmPriceLimit.toFixed(3)} 的 PayPal 国家`);
      return;
    }
    patchDraft({ billing_country_rotation: smsbowerPriceCountries.map((country) => country.value) });
    toast.success(`已将 ${smsbowerPriceCountries.length} 个国家应用为矩阵 Provider/PM 国家`);
  };
  const applyTemporarySmsbowerPriceCountries = () => {
    if (!temporarySmsPriceCountries.length) {
      toast.warning(`当前 SMS 价格快照中没有不高于 $${normalizedTemporarySmsPriceLimit.toFixed(3)} 的 PayPal 国家`);
      return;
    }
    setTemporaryCountryMode("custom");
    setTemporaryExtractionMode("matrix");
    setTemporaryLeftCountries(temporarySmsPriceCountries.map((country) => country.value));
    toast.success(`本次新增任务已选择 ${temporarySmsPriceCountries.length} 个 Provider/PM 国家`);
  };
  const patchCountryProxy = (code: string, value: string) => {
    draftDirtyRef.current = true;
    setDraft((current) => {
      const map = { ...normalizeCountryMap(current.residential_country_proxy_pool), [code]: value };
      if (!value.trim()) delete map[code];
      return mergeConfig({ ...current, use_residential_proxy_pool: true, residential_country_proxy_pool: map });
    });
  };
  const patchCommonResidentialProxy = (value: string) => {
    draftDirtyRef.current = true;
    setDraft((current) => mergeConfig({
      ...current,
      residential_proxy_pool: value,
      residential_country_proxy_pool: {},
      residential_checkout_proxy: "",
      residential_provider_proxy: "",
      residential_approve_proxy: "",
      residential_promotion_proxy: "",
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload = await updatePlusLinkConfig(configForSave(draft));
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      setDraft(mergeConfig(payload.plus_link.config));
      draftDirtyRef.current = false;
      toast.success("Plus 提链配置已保存");
    } catch (error) { toast.error(error instanceof Error ? error.message : "保存配置失败"); }
    finally { setSaving(false); }
  };
  const submitSelectedTasks = async (taskConfig: PlusLinkConfig, temporary = false) => {
    if (!selectedCount) { toast.warning("请先选择库存/半成品账号，或粘贴 access_token/session"); return; }
    armSuccessSound();
    knownSuccessTaskIdsRef.current = new Set();
    setStarting(true);
    try {
      const adding = Boolean(state?.runner_alive);
      const payload = await startPlusLinkTask({ emails: selectedEmails, partial_accounts: selectedPartials, manual_tokens: manualTokenLines, clear_results: false, persist_config: !temporary, config: configForSave(taskConfig) });
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      if (!temporary) setDraft(mergeConfig(payload.plus_link.config));
      setSelectedEmails([]);
      setSelectedPartials([]);
      setManualText("");
      setTemporaryConfigOpen(false);
      if (!temporary) draftDirtyRef.current = false;
      toast.success(`Plus 提链${adding ? "已添加" : "已启动"}：${selectedCount} 个任务`);
    } catch (error) { toast.error(error instanceof Error ? error.message : "启动提链失败"); }
    finally { setStarting(false); }
  };
  const openTemporaryConfig = () => {
    const initialMode = latestTaskCountryConfig ? "last" : "default";
    const extractionMode = latestTaskCountryConfig?.extractionMode || (matrixEnabled ? "matrix" : "fixed");
    setTemporaryCountryMode(initialMode);
    setTemporaryExtractionMode(extractionMode);
    setTemporaryCheckoutCountry(latestTaskCountryConfig?.checkout || draft.checkout_country);
    setTemporaryLeftCountries(extractionMode === "matrix"
      ? [...(latestTaskCountryConfig?.left || leftCountries)]
      : [latestTaskCountryConfig?.provider || draft.provider_country]);
    setTemporaryRightCountries(extractionMode === "matrix"
      ? [...(latestTaskCountryConfig?.right || rightCountries)]
      : [latestTaskCountryConfig?.promotion || draft.promotion_country]);
    setTemporarySmsPriceLimit(String(smsbowerPmPriceLimit));
    setTemporaryConfigOpen(true);
  };
  const handleStart = () => {
    if (!selectedCount) { toast.warning("请先选择库存/半成品账号，或粘贴 access_token/session"); return; }
    if (state?.runner_alive) {
      openTemporaryConfig();
      return;
    }
    void submitSelectedTasks(draft, Boolean(state?.runner_alive));
  };
  const handleTemporaryStart = () => {
    const temporaryConfig = mergeConfig({
      ...draft,
      country_rotation_enabled: temporaryExtractionMode === "matrix",
      checkout_country: temporaryCheckoutCountry,
      target_country: temporaryCheckoutCountry,
      approve_country: temporaryCheckoutCountry,
      provider_country: temporaryLeftCountries[0],
      payment_method_country: temporaryLeftCountries[0],
      promotion_country: temporaryRightCountries[0],
      smsbower_pm_max_price: normalizedTemporarySmsPriceLimit,
      billing_country_rotation: temporaryLeftCountries,
      proxy_country_rotation: temporaryRightCountries,
    });
    void submitSelectedTasks(temporaryConfig, true);
  };
  const selectTemporaryCountryMode = (mode: "last" | "default" | "custom") => {
    setTemporaryCountryMode(mode);
    if (mode === "last" && latestTaskCountryConfig) {
      setTemporaryExtractionMode(latestTaskCountryConfig.extractionMode);
      setTemporaryCheckoutCountry(latestTaskCountryConfig.checkout);
      setTemporaryLeftCountries(latestTaskCountryConfig.extractionMode === "matrix" ? [...latestTaskCountryConfig.left] : [latestTaskCountryConfig.provider]);
      setTemporaryRightCountries(latestTaskCountryConfig.extractionMode === "matrix" ? [...latestTaskCountryConfig.right] : [latestTaskCountryConfig.promotion]);
    } else if (mode === "default") {
      setTemporaryExtractionMode(matrixEnabled ? "matrix" : "fixed");
      setTemporaryCheckoutCountry(draft.checkout_country);
      setTemporaryLeftCountries(matrixEnabled ? [...leftCountries] : [draft.provider_country]);
      setTemporaryRightCountries(matrixEnabled ? [...rightCountries] : [draft.promotion_country]);
      setTemporarySmsPriceLimit(String(smsbowerPmPriceLimit));
    } else if (!temporaryLeftCountries.length || !temporaryRightCountries.length) {
      setTemporaryLeftCountries([...leftCountries]);
      setTemporaryRightCountries([...rightCountries]);
    }
  };
  const selectTemporaryExtractionMode = (mode: "fixed" | "matrix") => {
    setTemporaryCountryMode("custom");
    setTemporaryExtractionMode(mode);
    if (mode === "fixed") {
      setTemporaryLeftCountries([temporaryLeftCountries[0] || draft.provider_country]);
      setTemporaryRightCountries([temporaryRightCountries[0] || draft.promotion_country]);
    } else {
      if (!temporaryLeftCountries.length) setTemporaryLeftCountries([...leftCountries]);
      if (!temporaryRightCountries.length) setTemporaryRightCountries([...rightCountries]);
    }
  };
  const handleStop = async () => {
    setStopping(true);
    try {
      const payload = await stopPlusLinkTask();
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      toast.success("全部任务已终止，进行中的网络请求正在后台收尾");
    } catch (error) { toast.error(error instanceof Error ? error.message : "终止失败"); }
    finally { setStopping(false); }
  };
  const handleClear = async () => { try { const payload = await clearPlusLinkTask(); setState((current) => mergePlusLinkState(current, payload.plus_link)); toast.success("已清空提链日志和结果"); } catch (error) { toast.error(error instanceof Error ? error.message : "清空失败"); } };
  const handleBulkClear = async (scope: "failed" | "finished") => {
    try {
      const payload = await (scope === "failed" ? clearFailedPlusLinkTasks() : clearFinishedPlusLinkTasks());
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      toast.success(scope === "failed" ? "已清除失败和停止任务" : "已清理全部结束任务");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清理任务失败");
    }
  };
  const handleRestartFailed = async () => {
    setRestartingAll(true);
    try {
      armSuccessSound();
      knownSuccessTaskIdsRef.current = new Set();
      const payload = await retryFailedPlusLinkTasks();
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      const summary = payload.plus_link.bulk_retry;
      toast.success(`已重新排队 ${summary?.restarted || restartableTaskCount} 个任务${summary?.skipped ? `，跳过 ${summary.skipped} 个` : ""}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "一键重启失败");
    } finally {
      setRestartingAll(false);
    }
  };
  const handleTaskAction = async (taskId: string, action: "pause" | "resume" | "cancel" | "retry" | "delete") => {
    setTaskActionId(`${taskId}:${action}`);
    try {
      if (action === "retry") armSuccessSound();
      const request = action === "pause" ? pausePlusLinkTask : action === "resume" ? resumePlusLinkTask : action === "cancel" ? cancelPlusLinkTask : action === "delete" ? deletePlusLinkTask : retryPlusLinkTask;
      const payload = await request(taskId);
      setState((current) => mergePlusLinkState(current, payload.plus_link));
      toast.success(action === "pause" ? "任务将在当前步骤结束后暂停" : action === "resume" ? "任务已继续" : action === "cancel" ? "任务已终止，网络请求正在后台收尾" : action === "delete" ? "任务已删除" : "任务已重新排队");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "任务操作失败");
    } finally {
      setTaskActionId("");
    }
  };
  const openLongUrl = (url: string) => { window.open(url, "_blank", "noopener,noreferrer"); };
  const taskResults = useMemo(() => [...(state?.results || [])].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))), [state?.results]);
  const visibleTaskResults = useMemo(() => taskResults.slice(0, taskVisibleLimit), [taskResults, taskVisibleLimit]);
  const taskSummary = useMemo(() => ({
    total: taskResults.length,
    running: taskResults.filter((item) => ["queued", "running", "paused", "pausing", "cancelling"].includes(item.status)).length,
    completed: taskResults.filter((item) => item.status === "success").length,
    failed: taskResults.filter((item) => item.status === "failed" || item.status === "cancelled").length,
  }), [taskResults]);

  if (loading) {
    return <Card className="border-slate-200 bg-white/95 shadow-sm"><CardContent className="flex min-h-52 items-center justify-center text-slate-500"><LoaderCircle className="mr-2 size-5 animate-spin" /> 正在加载 Plus 提链配置…</CardContent></Card>;
  }

  return (
    <div className="space-y-5">
      <section className="overflow-hidden rounded-[2rem] bg-slate-950 text-white shadow-[0_24px_80px_-40px_rgba(15,23,42,0.9)]">
        <div className="bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.35),transparent_34%),radial-gradient(circle_at_bottom_right,rgba(59,130,246,0.22),transparent_32%)] p-5 sm:p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <div className="flex items-center gap-2"><span className="flex size-10 items-center justify-center rounded-2xl bg-emerald-400/15 text-emerald-200"><Zap className="size-5" /></span><div><h2 className="text-xl font-bold">Plus 提链总览</h2><p className="text-sm text-slate-300">本地调用内置提链代码，代理、账号、日志集中在本页完成。</p></div></div>
            </div>
            <div className="grid min-w-[320px] grid-cols-2 gap-3 sm:grid-cols-5 lg:grid-cols-5">
              {[["总数", stats.total || 0], ["运行中", stats.running || 0], ["成功", stats.success || 0], ["失败", stats.fail || 0], ["耗时", formatSeconds(stats.elapsed_seconds)]].map(([label, value]) => <div key={label} className="rounded-2xl border border-white/10 bg-white/10 px-3 py-2 text-center backdrop-blur"><div className="text-xs text-slate-300">{label}</div><div className="mt-1 text-lg font-bold text-white">{value}</div></div>)}
            </div>
          </div>
        </div>
      </section>

      <Card className="border-emerald-100 bg-white/95 shadow-sm">
        <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div><CardTitle className="flex items-center gap-2 text-lg"><Settings2 className="size-5 text-emerald-600" /> 分阶段提链</CardTitle><CardDescription>矩阵模式固定 Checkout/Approve/Checkout 账单出口，并轮播 Provider/PM 与 Promotion/Taxes 两组出口。</CardDescription></div>
          <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => void load()} disabled={saving || starting}><RefreshCw className="size-4" /> {state?.account_pool?.page_info?.partial ? `加载全部库存（${state.account_pool.page_info.total}）` : "刷新"}</Button><Button onClick={handleSave} disabled={saving} className="bg-emerald-600 hover:bg-emerald-700">{saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />} 保存配置</Button></div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-8">
            <div className="space-y-2"><FieldLabel hint="矩阵模式下同时固定 Checkout、Approve 与 Checkout 账单出口">Checkout 出口国家</FieldLabel><CountrySelect value={draft.checkout_country} options={BILLING_COUNTRIES} disabled={Boolean(state?.runner_alive)} onChange={(value) => patchDraft(matrixEnabled ? { checkout_country: value, target_country: value, approve_country: value } : { checkout_country: value })} /></div>
            <div className="space-y-2"><FieldLabel hint="Stripe init、PM 与 Confirm 请求出口；与 PayPal/PM 账单国家联动">Provider 出口</FieldLabel><CountrySelect value={draft.provider_country} options={PROVIDER_PM_COUNTRIES} disabled={Boolean(state?.runner_alive) || matrixEnabled} onChange={(value) => patchDraft({ provider_country: value, payment_method_country: value })} /></div>
            <div className="space-y-2"><FieldLabel hint="ChatGPT Approve 请求出口">Approve 出口</FieldLabel><CountrySelect value={draft.approve_country} options={BILLING_COUNTRIES} disabled={Boolean(state?.runner_alive) || matrixEnabled} onChange={(value) => patchDraft({ approve_country: value })} /></div>
            <div className="space-y-2"><FieldLabel>Checkout 账单国家</FieldLabel><CountrySelect value={draft.target_country} options={BILLING_COUNTRIES} disabled={Boolean(state?.runner_alive) || matrixEnabled} onChange={(value) => patchDraft({ target_country: value })} /></div>
            <div className="space-y-2"><FieldLabel hint="与 Provider 出口国家联动">PayPal/PM 账单国家</FieldLabel><CountrySelect value={draft.payment_method_country} options={PROVIDER_PM_COUNTRIES} disabled={Boolean(state?.runner_alive) || matrixEnabled} onChange={(value) => patchDraft({ payment_method_country: value, provider_country: value })} /></div>
            <div className="space-y-2"><FieldLabel hint="Promotion 与 Taxes 出口">优惠/税区出口</FieldLabel><CountrySelect value={draft.promotion_country} options={PROMO_COUNTRIES} disabled={Boolean(state?.runner_alive) || matrixEnabled} onChange={(value) => patchDraft({ promotion_country: value })} /></div>
            <div className="space-y-2"><FieldLabel>并发</FieldLabel><Input type="number" min={1} max={10} value={draft.concurrency || 1} onChange={(event) => patchDraft({ concurrency: Number(event.target.value || 1) })} /></div>
            <div className="space-y-2"><FieldLabel>Checkout/Approve 重试</FieldLabel><Input type="number" min={1} max={50} value={draft.checkout_retry_limit || 10} onChange={(event) => patchDraft({ checkout_retry_limit: Number(event.target.value || 10) })} /></div>
          </div>

          <div className="border-y border-indigo-100 bg-indigo-50/60 p-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="inline-flex rounded-md border border-indigo-200 bg-white p-1" role="tablist" aria-label="提链国家模式">
                <button type="button" role="tab" aria-selected={!matrixEnabled} disabled={Boolean(state?.runner_alive)} onClick={() => patchDraft({ country_rotation_enabled: false })} className={`min-h-9 rounded px-4 text-sm font-semibold transition ${!matrixEnabled ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-slate-100"}`}>固定分阶段</button>
                <button type="button" role="tab" aria-selected={matrixEnabled} disabled={Boolean(state?.runner_alive)} onClick={() => patchDraft({ country_rotation_enabled: true })} className={`min-h-9 rounded px-4 text-sm font-semibold transition ${matrixEnabled ? "bg-indigo-600 text-white" : "text-slate-600 hover:bg-slate-100"}`}>国家矩阵</button>
              </div>
              <div className="flex items-center gap-3"><div className="text-right"><div className="text-xs font-semibold text-indigo-600">当前配置</div><div className="text-sm text-indigo-900">{matrixEnabled ? `Provider/PM ${leftCountries.length} × 优惠 ${rightCountries.length}` : "六阶段独立国家"}</div></div><div className="text-2xl font-bold text-indigo-950">{comboTotal}</div></div>
            </div>

            {matrixEnabled ? <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)_220px]">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div><div className="font-bold text-slate-950">Provider/PM 国家序列</div><div className="mt-1 text-xs text-slate-500">Stripe Init、PM、Confirm、Poll、Redirect</div></div>
                  <div className="flex flex-wrap items-center gap-2"><Badge variant="outline">≤ ${smsbowerPmPriceLimit.toFixed(3)} · {smsbowerPriceCountries.length} 国</Badge><Badge variant="info">已选 {leftCountries.length} 国</Badge></div>
                </div>
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                  <div className="space-y-1.5"><span className="block text-xs font-semibold text-slate-600">SMS 接码上限（USD）</span><Input type="number" min={0.001} max={10} step={0.001} value={smsbowerPmPriceLimit} disabled={Boolean(state?.runner_alive)} onChange={(event) => patchDraft({ smsbower_pm_max_price: Number(event.target.value || 0.001) })} onKeyDown={(event) => { if (event.key === "Enter") applySmsbowerPriceCountries(); }} /></div>
                  <Button variant="outline" className="h-11 self-end border-indigo-200 text-indigo-700 hover:bg-indigo-50" disabled={Boolean(state?.runner_alive) || !smsbowerPriceCountries.length} onClick={applySmsbowerPriceCountries} title="按 SMSBower PayPal 最低价替换矩阵国家">
                    <Filter className="size-4" />应用 {smsbowerPriceCountries.length} 国
                  </Button>
                </div>
                <CountryMultiSelect value={leftCountries} defaults={DEFAULT_BILLING_ROTATION} options={PROVIDER_PM_COUNTRIES} disabled={Boolean(state?.runner_alive)} onChange={(value) => patchDraft({ billing_country_rotation: value })} />
              </div>
              <div className="space-y-3"><div className="flex items-center justify-between gap-2"><div><div className="font-bold text-slate-950">Promotion/Taxes 国家序列</div><div className="mt-1 text-xs text-slate-500">只用于优惠与税区同步</div></div><Badge variant="violet">{rightCountries.length} 国</Badge></div><CountryMultiSelect value={rightCountries} defaults={DEFAULT_PROXY_ROTATION} options={PROMO_COUNTRIES} disabled={Boolean(state?.runner_alive)} compact onChange={(value) => patchDraft({ proxy_country_rotation: value })} /></div>
              <div className="space-y-2"><FieldLabel hint="失败后切换下一国家组合">矩阵每组重试</FieldLabel><Input type="number" min={1} max={20} value={draft.matrix_combo_retry_limit || 1} onChange={(event) => patchDraft({ matrix_combo_retry_limit: Number(event.target.value || 1) })} /></div>
            </div> : <div className="mt-4 grid gap-4 md:grid-cols-[minmax(0,1fr)_220px] md:items-end"><div className="text-sm leading-6 text-indigo-900">固定模式严格使用上方六个国家字段。Checkout 账单和 PayPal/PM 账单可独立设置，例如 Checkout 账单 GB、PM 账单 JP。</div><div className="space-y-2"><FieldLabel hint="Stripe Provider 链路总重试次数">固定配置重试</FieldLabel><Input type="number" min={1} max={20} value={draft.provider_retry_limit || 15} onChange={(event) => patchDraft({ provider_retry_limit: Number(event.target.value || 15) })} /></div></div>}

            <label className="mt-4 flex items-start gap-3 border-t border-indigo-100 pt-3 text-sm text-slate-700"><Checkbox checked={Boolean(draft.require_ipv6_egress)} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ require_ipv6_egress: checked === true })} /><span><span className="block font-bold text-slate-900">强制 IPv6 出口</span><span className="mt-1 block text-xs leading-5 text-slate-500">仅校验各阶段实际出口的 IP 版本，不改变上述国家映射。</span></span></label>
          </div>

          <div className="border-y border-emerald-200 bg-emerald-50/70 px-4 py-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <label className="flex items-start gap-3 text-sm text-emerald-950"><Checkbox checked={draft.proxy_quality_gate_enabled !== false} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ proxy_quality_gate_enabled: checked === true })} /><span><span className="block font-bold">正式提链前检测三出口质量</span><span className="mt-1 block text-xs text-emerald-700">Checkout / Provider-PM / Promotion-Taxes</span></span></label>
              <Badge variant={draft.proxy_quality_gate_enabled !== false ? "success" : "outline"}>{draft.proxy_quality_gate_enabled !== false ? "强制通过" : "已关闭"}</Badge>
            </div>
            <div className={`mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4 ${draft.proxy_quality_gate_enabled !== false ? "" : "opacity-50"}`}>
              <div className="space-y-2"><FieldLabel>候选策略</FieldLabel><div className="flex h-10 items-center rounded-md border border-emerald-200 bg-white px-3 text-sm font-semibold text-emerald-800">持续轮换，直到通过</div></div>
              <div className="space-y-2"><FieldLabel>粘性检测轮数</FieldLabel><Input type="number" min={1} max={3} disabled={draft.proxy_quality_gate_enabled === false || Boolean(state?.runner_alive)} value={draft.proxy_quality_probe_rounds || 2} onChange={(event) => patchDraft({ proxy_quality_probe_rounds: Number(event.target.value || 2) })} /></div>
              <div className="space-y-2"><FieldLabel>目标延迟上限 / ms</FieldLabel><Input type="number" min={2000} max={30000} step={1000} disabled={draft.proxy_quality_gate_enabled === false || Boolean(state?.runner_alive)} value={draft.proxy_quality_max_latency_ms || 12000} onChange={(event) => patchDraft({ proxy_quality_max_latency_ms: Number(event.target.value || 12000) })} /></div>
              <div className="space-y-2"><FieldLabel>失败隔离 / 秒</FieldLabel><Input type="number" min={30} max={3600} step={30} disabled={draft.proxy_quality_gate_enabled === false || Boolean(state?.runner_alive)} value={draft.proxy_quality_quarantine_seconds || 300} onChange={(event) => patchDraft({ proxy_quality_quarantine_seconds: Number(event.target.value || 300) })} /></div>
            </div>
          </div>

          <div className="grid gap-3 border-y border-slate-200 py-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="space-y-2"><FieldLabel>Approve 后轮询秒数</FieldLabel><Input type="number" min={30} max={180} value={draft.post_approve_poll_timeout_seconds || 90} onChange={(event) => patchDraft({ post_approve_poll_timeout_seconds: Number(event.target.value || 90) })} /></div>
            <label className="flex min-h-16 items-center gap-3 border-l-4 border-emerald-400 bg-emerald-50 px-4 py-3 text-sm text-emerald-950"><Checkbox checked={draft.enable_playwright_approve_fallback !== false} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ enable_playwright_approve_fallback: checked === true })} /><span className="font-semibold">Playwright Approve 兜底</span></label>
            <label className="flex min-h-16 items-center gap-3 border-l-4 border-sky-400 bg-sky-50 px-4 py-3 text-sm text-sky-950"><Checkbox checked={draft.enable_playwright_redirect_fallback !== false} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ enable_playwright_redirect_fallback: checked === true })} /><span className="font-semibold">Playwright 跳转兜底</span></label>
            <label className="flex min-h-16 items-center gap-3 border-l-4 border-amber-400 bg-amber-50 px-4 py-3 text-sm text-amber-950"><Checkbox checked={Boolean(draft.allow_intermediate_url_fallback)} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ allow_intermediate_url_fallback: checked === true })} /><span className="font-semibold">保留 intermediate_url 备用</span></label>
          </div>

          <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
            <div className="rounded-3xl border border-amber-200 bg-amber-50/70 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3"><div className="text-base font-bold text-amber-950">Plus 专用按国家住宅代理池</div><Badge variant="warning">711 模板：USER-zone-custom-region-JP:pass@host:port</Badge></div>
              <p className="mt-2 text-sm leading-6 text-amber-800">当前出口预览：Checkout/Approve {stagePreview.checkoutCountry}；Provider/PM/Poll/Redirect {stagePreview.providerCountry}；Promotion/Taxes {stagePreview.promotionCountry}。</p>
              <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {visibleProxyCountries.map((code) => <div key={code} className="space-y-1.5"><div className="flex items-center justify-between"><span className="text-sm font-bold text-amber-950">{code} · {COUNTRY_LABEL[code]}</span><Badge variant="outline">{lineCount(draft.residential_country_proxy_pool?.[code])} 条</Badge></div><Textarea className="min-h-24 resize-y bg-white/90 font-mono text-xs leading-5" placeholder={`USER123456-zone-custom-region-${code}:fixturepass@global.example.711proxy.invalid:10000`} value={draft.residential_country_proxy_pool?.[code] || ""} onChange={(event) => patchCountryProxy(code, event.target.value)} /></div>)}
              </div>
            </div>

            <div className="space-y-4 rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div><div className="flex items-center justify-between gap-2"><div className="text-sm font-bold text-slate-900">Plus 专用公共住宅模板</div><Badge variant="outline">业务出口保持住宅</Badge></div><p className="mt-1 text-xs leading-5 text-slate-500">对应国家为空时才回退，支持 {'{country}'} / {'{session}'} 占位符。</p><Textarea className="mt-2 min-h-28 resize-y bg-white font-mono text-xs leading-5" placeholder="http://user:pass@host:port" value={draft.residential_proxy_pool || ""} onChange={(event) => patchCommonResidentialProxy(event.target.value)} /></div>
              <div className="rounded-2xl border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900">
                <div className="flex items-start justify-between gap-3"><label className="flex items-start gap-3"><Checkbox checked={draft.residential_via_project_proxy !== false} disabled={Boolean(state?.runner_alive)} onCheckedChange={(checked) => patchDraft({ residential_via_project_proxy: checked === true, residential_via_clash: checked === true })} /><span><span className="block font-bold">住宅代理通过项目代理节点转接</span><span className="mt-1 block text-xs leading-5 text-sky-700">国内部署建议开启；项目部署 IP 在国外且可直接连接 711 住宅代理时可以关闭。</span></span></label><Badge variant={draft.residential_via_project_proxy !== false ? "info" : "outline"}>{draft.residential_via_project_proxy !== false ? "已开启" : "直连 711"}</Badge></div>
                <div className={`mt-3 rounded-xl border border-sky-200 bg-white/80 px-3 py-2 ${draft.residential_via_project_proxy !== false ? "" : "opacity-50"}`}><div className="text-xs font-bold text-sky-900">父代理节点池由“代理配置”管理</div><div className="mt-1 text-xs leading-5 text-sky-700">请在代理配置选择 Plus 提链专用订阅。注册不会套用该筛选，旧筛选值仅在未指定专用订阅时兜底。</div></div>
              </div>
              <label className="flex items-center gap-3 text-sm font-semibold text-slate-700"><Checkbox checked={draft.refresh_tokens} onCheckedChange={(checked) => patchDraft({ refresh_tokens: checked })} />提链前先刷新 refresh_token</label>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-4">
            {[["Checkout / Approve", stagePreview.checkoutCountry, stagePreview.checkout], ["Provider / PM", stagePreview.providerCountry, stagePreview.provider], ["Promotion / Taxes", stagePreview.promotionCountry, stagePreview.promotion], ["Poll / Redirect", stagePreview.providerCountry, stagePreview.provider]].map(([title, code, value]) => <div key={title} className="rounded-3xl border border-emerald-100 bg-emerald-50/70 p-4"><div className="flex items-center justify-between gap-2"><div className="text-sm font-bold text-emerald-950">{title}</div><Badge variant="success">首组 {code}</Badge></div><Textarea readOnly className="mt-2 min-h-24 resize-none bg-white/80 font-mono text-xs leading-5" value={String(value || "")} /><div className="mt-2 text-xs text-emerald-700">当前池：{lineCount(String(value || ""))} 条</div></div>)}
          </div>
        </CardContent>
      </Card>

      <Card className="border-slate-200 bg-white/95 shadow-sm">
        <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div><CardTitle className="flex items-center gap-2 text-lg"><UserRound className="size-5 text-sky-600" /> 账号选择</CardTitle><CardDescription>支持筛选库存/半成品账号；库存默认隐藏 Plus 账号，也可以直接粘贴多个 access_token 或 session。</CardDescription></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setSelectedEmails(filteredEligibleAccounts.map(accountSelector))} disabled={!filteredEligibleAccounts.length}>选择筛选库存</Button>
            <Button variant="outline" onClick={() => setSelectedPartials(filteredEligiblePartials.map(partialSelector))} disabled={!filteredEligiblePartials.length}>选择筛选半成品</Button>
            <Button variant="outline" onClick={() => { setSelectedEmails([]); setSelectedPartials([]); setManualText(""); }} disabled={!selectedCount}>清除选择</Button>
            {!state?.runner_alive ? <Button variant="outline" onClick={openTemporaryConfig} disabled={starting || !selectedCount}><Settings2 className="size-4" /> 临时任务配置</Button> : null}
            <Button onClick={handleStart} disabled={starting || stopping || !selectedCount} className="bg-slate-950 hover:bg-slate-800">{starting ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />} {state?.runner_alive ? "添加任务" : "开始提链"}（{selectedCount}）</Button>
            <Button variant="destructive" onClick={handleStop} disabled={!state?.runner_alive || stopping}>{stopping ? <LoaderCircle className="size-4 animate-spin" /> : <Square className="size-4" />} 停止</Button>
            <Button variant="outline" onClick={handleClear} disabled={Boolean(state?.runner_alive)}><Trash2 className="size-4" /> 清空日志</Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="min-w-0 rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2 font-bold text-slate-900"><Database className="size-4 text-slate-500" /> 库存账号</div><Badge variant="outline" className="max-w-full whitespace-normal text-right leading-5">筛选 {filteredEligibleAccounts.length} / 可用 {eligibleAccounts.length} / 总计 {accounts.length} / 隐藏付费 {showPlusAccounts ? 0 : hiddenPaidAccounts}</Badge></div>
              <div className="mb-3 grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_150px_auto]"><Input value={accountQuery} onChange={(event) => setAccountQuery(event.target.value)} placeholder="筛选邮箱 / plan / token 状态 / 分组" /><select value={accountGroupFilter} onChange={(event) => setAccountGroupFilter(event.target.value)} className="h-10 min-w-0 w-full rounded-2xl border border-white bg-white px-3 text-sm text-slate-700"><option value="all">全部分组</option>{accountGroups.map((group) => <option key={group} value={group}>{group}</option>)}</select><label className="flex min-w-0 items-center gap-2 rounded-2xl border border-white bg-white px-3 text-sm font-semibold text-slate-700"><Checkbox checked={showPlusAccounts} onCheckedChange={(checked) => setShowPlusAccounts(Boolean(checked))} />显示 Plus 账号</label></div>
              <div className="max-h-72 space-y-2 overflow-auto pr-1">
                {filteredEligibleAccounts.length ? visibleEligibleAccounts.map((account) => {
                  const selector = accountSelector(account);
                  const effectivePlan = accountEffectivePlan(account);
                  const rawPlan = normalizePlanValue(account.plan_type);
                  return <label key={selector} className="flex items-start gap-3 rounded-2xl border border-white bg-white p-3 text-sm shadow-sm"><Checkbox checked={selectedEmailSet.has(selector)} onCheckedChange={(checked) => setSelectedEmails((items) => selectedToggle(items, selector, checked))} /><span className="min-w-0 flex-1"><span className="block truncate font-semibold text-slate-900">{account.email}</span><span className="mt-1 flex flex-wrap gap-1 text-xs text-slate-500"><Badge variant="secondary">{mailboxGroupLabel(account)}</Badge><Badge variant={planBadgeVariant(effectivePlan)}>{effectivePlan}</Badge>{rawPlan && rawPlan !== effectivePlan ? <Badge variant="outline">原始 {rawPlan}</Badge> : null}{account.oauth_status ? <Badge variant="outline">{account.oauth_status}</Badge> : null}{account.has_access_token ? <Badge variant="success">access_token</Badge> : null}{account.has_refresh_token ? <Badge variant="info">refresh_token</Badge> : null}</span></span></label>;
                }) : <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">暂无可用库存账号</div>}
                {visibleEligibleAccounts.length < filteredEligibleAccounts.length ? <Button type="button" size="sm" variant="outline" className="w-full bg-white" onClick={() => setAccountVisibleLimit((limit) => limit + ACCOUNT_RENDER_BATCH)}>再显示 {Math.min(ACCOUNT_RENDER_BATCH, filteredEligibleAccounts.length - visibleEligibleAccounts.length)} 条（已显示 {visibleEligibleAccounts.length}/{filteredEligibleAccounts.length}）</Button> : null}
              </div>
            </div>
            <div className="min-w-0 rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2 font-bold text-slate-900"><CheckCircle2 className="size-4 text-slate-500" /> 半成品账号</div><Badge variant="outline" className="max-w-full whitespace-normal text-right leading-5">筛选 {filteredEligiblePartials.length} / 可用 {eligiblePartials.length} / 总计 {partialAccounts.length}</Badge></div>
              <div className="mb-3 grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_150px]"><Input value={partialQuery} onChange={(event) => setPartialQuery(event.target.value)} placeholder="筛选邮箱 / 阶段 / 状态 / 分组" /><select value={partialGroupFilter} onChange={(event) => setPartialGroupFilter(event.target.value)} className="h-10 min-w-0 w-full rounded-2xl border border-white bg-white px-3 text-sm text-slate-700"><option value="all">全部分组</option>{partialGroups.map((group) => <option key={group} value={group}>{group}</option>)}</select></div>
              <div className="max-h-72 space-y-2 overflow-auto pr-1">
                {filteredEligiblePartials.length ? visibleEligiblePartials.map((account) => {
                  const selector = partialSelector(account);
                  return <label key={selector} className="flex items-start gap-3 rounded-2xl border border-white bg-white p-3 text-sm shadow-sm"><Checkbox checked={selectedPartialSet.has(selector)} onCheckedChange={(checked) => setSelectedPartials((items) => selectedToggle(items, selector, checked))} /><span className="min-w-0 flex-1"><span className="block truncate font-semibold text-slate-900">{account.email}</span><span className="mt-1 flex flex-wrap gap-1 text-xs text-slate-500"><Badge variant="secondary">{mailboxGroupLabel(account)}</Badge>{account.stage ? <Badge variant="outline">{account.stage}</Badge> : null}{account.has_access_token ? <Badge variant="success">access_token</Badge> : null}{account.has_session_token ? <Badge variant="warning">session</Badge> : null}{account.has_refresh_token ? <Badge variant="info">refresh_token</Badge> : null}</span></span></label>;
                }) : <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">暂无带 token/session 的半成品账号</div>}
                {visibleEligiblePartials.length < filteredEligiblePartials.length ? <Button type="button" size="sm" variant="outline" className="w-full bg-white" onClick={() => setPartialVisibleLimit((limit) => limit + PARTIAL_RENDER_BATCH)}>再显示 {Math.min(PARTIAL_RENDER_BATCH, filteredEligiblePartials.length - visibleEligiblePartials.length)} 条（已显示 {visibleEligiblePartials.length}/{filteredEligiblePartials.length}）</Button> : null}
              </div>
            </div>
          </div>
          <div className="rounded-3xl border border-violet-100 bg-violet-50/70 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><div><div className="font-bold text-violet-950">手动 access_token / session</div><div className="text-xs text-violet-700">每行一个；支持纯 token、Bearer token、email token 或 JSON。</div></div><Badge variant="violet">{manualTokenLines.length} 条</Badge></div>
            <Textarea className="min-h-36 resize-y bg-white font-mono text-xs leading-5" placeholder={'user@example.com access_token=eyJ...\nBearer eyJ...\n{"email":"a@b.com","access_token":"eyJ..."}'} value={manualText} onChange={(event) => setManualText(event.target.value)} />
          </div>
        </CardContent>
      </Card>

      <Card className="overflow-hidden border-slate-200 bg-white/95 shadow-sm">
        <CardHeader className="border-b border-slate-200 bg-white p-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex flex-wrap gap-2 text-sm">
              {[["总任务", taskSummary.total], ["运行中", taskSummary.running], ["已完成", taskSummary.completed], ["失败/停止", taskSummary.failed]].map(([label, value]) => <div key={label} className="flex h-10 min-w-24 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3"><span className="text-base font-bold text-slate-950">{value}</span><span className="text-xs text-slate-500">{label}</span></div>)}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={handleStart} disabled={starting || stopping || !selectedCount} className="bg-emerald-600 hover:bg-emerald-700">{starting ? <LoaderCircle className="size-3.5 animate-spin" /> : <Play className="size-3.5" />} {state?.runner_alive ? "添加任务" : "一键启动"}（{selectedCount}）</Button>
              <Button size="sm" variant="destructive" onClick={handleStop} disabled={!state?.runner_alive || stopping}>{stopping ? <LoaderCircle className="size-3.5 animate-spin" /> : <Square className="size-3.5" />} 一键终止</Button>
              <Button size="sm" variant="outline" onClick={() => void handleRestartFailed()} disabled={restartingAll || !restartableTaskCount} className="border-amber-200 text-amber-700 hover:bg-amber-50">{restartingAll ? <LoaderCircle className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />} 一键重启（{restartableTaskCount}）</Button>
              <Button size="sm" variant="outline" disabled={!successLinks.length} onClick={() => copyText(successLinks.map((item) => item.long_url || "").filter(Boolean).join("\n"), "成功长链已复制")}><Copy className="size-3.5" /> 复制成功链接</Button>
              <Button size="sm" variant="outline" disabled={!successLinks.length} onClick={() => copyText(successLinks.map((item) => item.email).filter(Boolean).join("\n"), "成功邮箱已复制")}><Copy className="size-3.5" /> 复制成功邮箱</Button>
              <Button size="sm" variant="outline" disabled={!taskSummary.failed} onClick={() => void handleBulkClear("failed")}><Trash2 className="size-3.5" /> 清除失败</Button>
              <Button size="sm" variant="outline" disabled={!taskSummary.completed && !taskSummary.failed} onClick={() => void handleBulkClear("finished")}><Trash2 className="size-3.5" /> 清理已结束</Button>
              <Button size="sm" variant="outline" onClick={() => void load(true, false)}><RefreshCw className="size-3.5" /> 刷新任务</Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="bg-slate-50 p-4">
          {taskResults.length ? <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">
            {visibleTaskResults.map((result, index) => {
              const progress = progressValue(result);
              const paused = result.status === "paused" || result.status === "pausing";
              const active = ["queued", "running", "paused", "pausing", "cancelling"].includes(result.status);
              const terminal = ["success", "failed", "cancelled"].includes(result.status);
              const retryable = result.retryable !== false && !result.cleanup_pending;
              const busy = taskActionId.startsWith(`${result.task_id}:`);
              const proxyDisplay = taskProxyDisplay(result);
              const attemptHistory = [...(result.attempt_diagnostics_history || [])].reverse();
              return <div key={result.task_id} className={`overflow-hidden rounded-lg border border-l-4 border-slate-200 bg-white shadow-sm ${taskAccent(result.status)}`}>
                <div className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px]">
                        <span className="font-bold uppercase tracking-wide text-rose-500">TASK {taskResults.length - index}</span>
                        <span className="font-medium text-slate-400">开始时间 {taskStartTimeText(result)}</span>
                      </div>
                      <div className="mt-1 flex min-w-0 items-baseline gap-1.5 text-sm"><span className="shrink-0 text-[10px] font-semibold text-slate-400">邮箱</span><span className="min-w-0 truncate font-bold text-slate-950" title={result.account_email || result.email}>{result.account_email || result.email || result.task_id}</span></div>
                      <div className="mt-2 flex flex-wrap gap-1.5"><span className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1 text-[10px] font-semibold text-sky-700">账号 {accountTypeText(result.eligibility_plan || result.account_type)}</span><span className="rounded-md border border-violet-200 bg-violet-50 px-2 py-1 text-[10px] font-semibold text-violet-700" title={tokenExpiryText(result.token_expires_at)}>AT 有效期 {tokenExpiryText(result.token_expires_at)}</span>{result.cleanup_pending ? <span className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] font-semibold text-amber-700">后台收尾</span> : null}<span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-mono text-[10px] text-slate-500">{shortTaskId(result.task_id)}</span>{result.matrix_total ? <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[10px] text-slate-500">组合 {result.matrix_index || 1}/{result.matrix_total}{result.matrix_skipped ? ` · 跳过 ${result.matrix_skipped}` : ""}</span> : null}{paypalAvailabilityText(result.paypal_availability) ? <span className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[10px] text-slate-600">{paypalAvailabilityText(result.paypal_availability)}</span> : null}</div>
                    </div>
                    <Badge variant={statusVariant(result.status)}>{result.status === "success" ? "已完成" : statusText(result.status)}</Badge>
                  </div>

                  <div className="mt-4">
                    <div className="mb-1.5 flex items-center justify-between gap-3 text-xs"><span className="truncate font-semibold text-slate-700">{taskStageTitle(result)}</span><span className="shrink-0 font-bold text-slate-700">{progress}%</span></div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className={`h-full rounded-full transition-[width] duration-500 ${taskProgressColor(result.status)}`} style={{ width: `${progress}%` }} /></div>
                    <div className="mt-2 min-h-10 text-xs leading-5 text-slate-500">{taskStageDescription(result)}</div>
                  </div>

                  <div className="mt-3 flex min-w-0 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px]">
                    <span className="shrink-0 font-semibold text-sky-700">{proxyDisplay.primary.label} {proxyDisplay.primary.country}</span><span className="min-w-0 truncate font-mono text-slate-700" title={proxyDisplay.primary.ip}>{proxyDisplay.primary.ip}</span><span className="shrink-0 text-slate-300">→</span><span className="shrink-0 font-semibold text-violet-700">{proxyDisplay.secondary.label} {proxyDisplay.secondary.country}</span><span className="min-w-0 truncate font-mono text-slate-700" title={proxyDisplay.secondary.ip}>{proxyDisplay.secondary.ip}</span>
                  </div>

                  {result.quality_gate_results?.length ? <div className="mt-2 grid grid-cols-3 gap-1.5">
                    {result.quality_gate_results.map((item) => <div key={item.stage || item.label} className={`min-w-0 rounded-md border px-2 py-1.5 text-[10px] ${item.ok ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-rose-200 bg-rose-50 text-rose-700"}`} title={item.error || `${item.target_latency_ms || 0}ms`}><div className="truncate font-semibold">{item.label || item.stage} · {item.country || item.expected_country || "--"}</div><div className="mt-0.5 truncate font-mono">{item.ip || item.error || "等待检测"}</div></div>)}
                  </div> : null}

                  <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">类型</div><div className="mt-1 font-semibold text-slate-800">PayPal</div></div>
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">尝试</div><div className="mt-1 font-semibold text-slate-800">{result.link_attempt_count || 0}/{result.link_attempt_limit || result.active_retry_limit || draft.matrix_combo_retry_limit || 1} 次</div></div>
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">耗时</div><div className="mt-1 font-semibold text-slate-800">{taskDurationText(result)}</div></div>
                  </div>

                  {attemptHistory.length ? <details className="group mt-3 border-t border-slate-200 pt-2">
                    <summary className="flex min-h-8 cursor-pointer list-none items-center justify-between gap-3 text-xs font-semibold text-slate-700">
                      <span>轮次记录</span><span className="rounded-md bg-slate-100 px-2 py-1 text-[10px] text-slate-500">{attemptHistory.length} 条</span>
                    </summary>
                    <div className="mt-1 max-h-60 overflow-y-auto border-y border-slate-200">
                      {attemptHistory.map((item) => <div key={`${item.run_number || 1}-${item.matrix_index || 1}-${item.attempt}`} className="border-b border-slate-100 px-1 py-2.5 text-[11px] last:border-b-0">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-semibold text-slate-800">{Number(item.run_number || 1) > 1 ? `重试 ${item.run_number} · ` : ""}组合 {item.matrix_index || 1}/{item.matrix_total || 1} · 第 {item.attempt}/{item.attempt_limit || result.link_attempt_limit || 1} 次</span>
                          <span className={item.status === "success" ? "font-semibold text-emerald-700" : item.failure_category === "proxy_quality_gate" ? "font-semibold text-amber-600" : "font-semibold text-rose-600"}>{item.status === "success" ? "成功" : item.failure_category === "proxy_quality_gate" ? "候选链路淘汰 · 未进入提链" : `${attemptStepLabel(item)} · ${attemptFailureLabel(item)}`}</span>
                        </div>
                        <div className="mt-1 break-words leading-4 text-slate-600">{item.status === "success" ? "已生成 PayPal BA 长链。" : item.error || item.promotion_error || item.provider_error || item.stripe_confirm_error || "未提供错误摘要"}</div>
                        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 font-mono text-[10px] text-slate-400"><span className="break-all">{attemptRouteText(item)}</span><span>{Number(item.duration_seconds || 0).toFixed(1)}s</span></div>
                      </div>)}
                    </div>
                  </details> : null}

                  {result.long_url ? <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3"><div className="text-[10px] font-semibold text-emerald-700">提取结果</div><div className="mt-1 truncate font-mono text-xs text-emerald-950" title={result.long_url}>{result.long_url}</div><div className="mt-2 flex gap-2"><Button size="sm" variant="outline" onClick={() => copyText(result.long_url || "", "长链已复制")}><Copy className="size-3.5" /> 复制长链</Button><Button size="sm" variant="outline" onClick={() => openLongUrl(result.long_url || "")}><ExternalLink className="size-3.5" /> 打开长链</Button></div></div> : null}
                  {result.intermediate_url ? <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"><div className="text-[10px] font-semibold text-amber-700">Stripe 中转备用链接</div><div className="mt-1 truncate font-mono text-xs text-amber-950" title={result.intermediate_url}>{result.intermediate_url}</div><div className="mt-2 flex gap-2"><Button size="sm" variant="outline" onClick={() => copyText(result.intermediate_url || "", "中转链接已复制")}><Copy className="size-3.5" /> 复制</Button><Button size="sm" variant="outline" onClick={() => openLongUrl(result.intermediate_url || "")}><ExternalLink className="size-3.5" /> 打开</Button></div></div> : null}

                  <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-200 pt-3">
                    {active && result.status !== "cancelling" ? paused ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "resume")}><Play className="size-3.5" /> 继续</Button> : <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "pause")}><Pause className="size-3.5" /> 暂停</Button> : null}
                    {active ? <Button size="sm" variant="destructive" disabled={busy || result.status === "cancelling"} onClick={() => void handleTaskAction(result.task_id, "cancel")}><X className="size-3.5" /> 停止任务</Button> : null}
                    {terminal && retryable ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "retry")}><RotateCcw className="size-3.5" /> 重新开始</Button> : null}
                    <Button size="sm" variant="outline" title="删除任务" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "delete")}><Trash2 className="size-3.5" /> 删除</Button>
                  </div>
                </div>
              </div>;
            })}
            {visibleTaskResults.length < taskResults.length ? <Button type="button" variant="outline" className="w-full bg-white" onClick={() => setTaskVisibleLimit((limit) => limit + TASK_RENDER_BATCH)}>再显示 {Math.min(TASK_RENDER_BATCH, taskResults.length - visibleTaskResults.length)} 个历史任务（已显示 {visibleTaskResults.length}/{taskResults.length}）</Button> : null}
          </div> : <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">暂无提链任务，配置代理和账号后点击开始提链。</div>}
        </CardContent>
      </Card>

      {temporaryConfigOpen ? <div className="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/55 p-4 backdrop-blur-xs" role="dialog" aria-modal="true" aria-label="新增任务临时国家配置">
        <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
          <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
            <div><div className="flex items-center gap-2 text-lg font-bold text-slate-950"><Settings2 className="size-5 text-emerald-600" />新增任务临时国家配置</div><div className="mt-1 text-sm text-slate-500">仅应用于本次新增的 {selectedCount} 个任务，不修改正在运行的任务或全局默认配置。</div></div>
            <Button size="icon" variant="ghost" title="关闭临时配置" disabled={starting} onClick={() => setTemporaryConfigOpen(false)}><X className="size-4" /></Button>
          </div>

          <div className="overflow-y-auto px-5 py-5">
            <div className="grid grid-cols-3 gap-1 rounded-lg bg-slate-100 p-1" role="tablist" aria-label="新增任务国家配置来源">
              <button type="button" role="tab" aria-selected={temporaryCountryMode === "last"} disabled={!latestTaskCountryConfig} onClick={() => selectTemporaryCountryMode("last")} className={`min-h-10 rounded-md px-3 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${temporaryCountryMode === "last" ? "bg-white text-slate-950 shadow-sm" : "text-slate-500 hover:text-slate-800"}`}>跟随上一次任务</button>
              <button type="button" role="tab" aria-selected={temporaryCountryMode === "default"} onClick={() => selectTemporaryCountryMode("default")} className={`min-h-10 rounded-md px-3 text-sm font-semibold transition ${temporaryCountryMode === "default" ? "bg-white text-slate-950 shadow-sm" : "text-slate-500 hover:text-slate-800"}`}>当前默认配置</button>
              <button type="button" role="tab" aria-selected={temporaryCountryMode === "custom"} onClick={() => selectTemporaryCountryMode("custom")} className={`min-h-10 rounded-md px-3 text-sm font-semibold transition ${temporaryCountryMode === "custom" ? "bg-white text-slate-950 shadow-sm" : "text-slate-500 hover:text-slate-800"}`}>自定义</button>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-1 rounded-lg border border-indigo-200 bg-indigo-50 p-1" role="tablist" aria-label="新增任务提链模式">
              <button type="button" role="tab" aria-selected={temporaryExtractionMode === "fixed"} onClick={() => selectTemporaryExtractionMode("fixed")} className={`min-h-10 rounded-md px-3 text-sm font-semibold transition ${temporaryExtractionMode === "fixed" ? "bg-slate-950 text-white shadow-sm" : "text-indigo-700 hover:bg-white"}`}>固定组合</button>
              <button type="button" role="tab" aria-selected={temporaryExtractionMode === "matrix"} onClick={() => selectTemporaryExtractionMode("matrix")} className={`min-h-10 rounded-md px-3 text-sm font-semibold transition ${temporaryExtractionMode === "matrix" ? "bg-indigo-600 text-white shadow-sm" : "text-indigo-700 hover:bg-white"}`}>国家矩阵</button>
            </div>

            {temporaryCountryMode === "custom" && temporaryExtractionMode === "fixed" ? <div className="mt-5 grid border-y border-slate-200 py-5 lg:grid-cols-3 lg:gap-5">
              <div className="space-y-2"><FieldLabel hint="Checkout、Approve 与 Checkout 账单">固定 Checkout 国家</FieldLabel><CountrySelect value={temporaryCheckoutCountry} options={BILLING_COUNTRIES} onChange={setTemporaryCheckoutCountry} /></div>
              <div className="space-y-2"><FieldLabel hint="Init、PM、Confirm、Poll、Redirect">Provider/PM 国家</FieldLabel><CountrySelect value={temporaryLeftCountries[0] || draft.provider_country} options={PROVIDER_PM_COUNTRIES} onChange={(value) => setTemporaryLeftCountries([value])} /></div>
              <div className="space-y-2"><FieldLabel hint="只用于优惠与税区同步">Promotion/Taxes 国家</FieldLabel><CountrySelect value={temporaryRightCountries[0] || draft.promotion_country} options={PROMO_COUNTRIES} onChange={(value) => setTemporaryRightCountries([value])} /></div>
            </div> : temporaryCountryMode === "custom" ? <div className="mt-5 grid border-y border-slate-200 lg:grid-cols-[240px_minmax(0,1fr)_minmax(0,1fr)]">
              <div className="space-y-3 border-b border-slate-200 py-4 lg:border-r lg:border-b-0 lg:pr-5">
                <div><div className="font-bold text-slate-950">固定 Checkout 国家</div><div className="mt-1 text-xs text-slate-500">Checkout、Approve、Checkout 账单</div></div>
                <CountrySelect value={temporaryCheckoutCountry} options={BILLING_COUNTRIES} onChange={setTemporaryCheckoutCountry} />
              </div>
              <div className="space-y-3 border-b border-slate-200 py-4 lg:border-r lg:border-b-0 lg:px-5">
                <div className="flex items-center justify-between gap-2"><div><div className="font-bold text-slate-950">Provider/PM 国家</div><div className="mt-1 text-xs text-slate-500">Init、PM、Confirm、Poll、Redirect</div></div><Badge variant="info">{temporaryLeftCountries.length} 国</Badge></div>
                <div className="rounded-lg border border-sky-200 bg-sky-50 p-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="min-w-32 flex-1 space-y-1.5">
                      <span className="block text-xs font-semibold text-slate-600">SMS 接码上限（USD）</span>
                      <Input type="number" min={0.001} max={10} step={0.001} value={temporarySmsPriceLimit} onChange={(event) => setTemporarySmsPriceLimit(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") applyTemporarySmsbowerPriceCountries(); }} />
                    </label>
                    <Button type="button" variant="outline" className="shrink-0 bg-white" onClick={applyTemporarySmsbowerPriceCountries}><Filter className="size-4" />应用 {temporarySmsPriceCountries.length} 国</Button>
                  </div>
                  <div className="mt-2 text-xs text-sky-800">按 SMSBower PayPal 当前价格筛选，仅修改本次新增任务。</div>
                </div>
                <CountryMultiSelect value={temporaryLeftCountries} defaults={leftCountries} options={PROVIDER_PM_COUNTRIES} onChange={setTemporaryLeftCountries} />
              </div>
              <div className="space-y-3 py-4 lg:pl-5">
                <div className="flex items-center justify-between gap-2"><div><div className="font-bold text-slate-950">Promotion/Taxes 国家</div><div className="mt-1 text-xs text-slate-500">只用于优惠与税区同步</div></div><Badge variant="violet">{temporaryRightCountries.length} 国</Badge></div>
                <CountryMultiSelect value={temporaryRightCountries} defaults={rightCountries} options={PROMO_COUNTRIES} compact onChange={setTemporaryRightCountries} />
              </div>
            </div> : temporaryExtractionMode === "fixed" ? <div className="mt-5 grid gap-5 border-y border-slate-200 py-5 lg:grid-cols-3">
              <div><div className="text-sm font-bold text-slate-900">固定 Checkout</div><div className="mt-3"><Badge variant="success">{temporaryCheckoutCountry} · {COUNTRY_LABEL[temporaryCheckoutCountry]}</Badge></div></div>
              <div><div className="text-sm font-bold text-slate-900">Provider/PM</div><div className="mt-3"><Badge variant="info">{temporaryLeftCountries[0]} · {COUNTRY_LABEL[temporaryLeftCountries[0]]}</Badge></div></div>
              <div><div className="text-sm font-bold text-slate-900">Promotion/Taxes</div><div className="mt-3"><Badge variant="violet">{temporaryRightCountries[0]} · {COUNTRY_LABEL[temporaryRightCountries[0]]}</Badge></div></div>
            </div> : <div className="mt-5 grid gap-5 border-y border-slate-200 py-5 lg:grid-cols-[220px_minmax(0,1fr)_minmax(0,1fr)]">
              <div><div className="text-sm font-bold text-slate-900">固定 Checkout</div><div className="mt-3"><Badge variant="success">{temporaryCheckoutCountry} · {COUNTRY_LABEL[temporaryCheckoutCountry]}</Badge></div></div>
              <div><div className="text-sm font-bold text-slate-900">Provider/PM · {temporaryLeftCountries.length}</div><div className="mt-3 flex flex-wrap gap-2">{temporaryLeftCountries.map((code) => <Badge key={code} variant="info">{code} · {COUNTRY_LABEL[code]}</Badge>)}</div></div>
              <div><div className="text-sm font-bold text-slate-900">Promotion/Taxes · {temporaryRightCountries.length}</div><div className="mt-3 flex flex-wrap gap-2">{temporaryRightCountries.map((code) => <Badge key={code} variant="violet">{code} · {COUNTRY_LABEL[code]}</Badge>)}</div></div>
            </div>}

            <div className="mt-5 flex flex-wrap items-center justify-between gap-4 bg-emerald-50 px-4 py-3 text-sm text-emerald-950">
              <div><span className="font-bold">本次新增</span><span className="ml-2 text-emerald-700">{selectedCount} 个账号，{temporaryExtractionMode === "matrix" ? `每个账号运行 ${temporaryLeftCountries.length} × ${temporaryRightCountries.length} 个国家组合` : `固定运行 ${temporaryCheckoutCountry} / ${temporaryLeftCountries[0]} / ${temporaryRightCountries[0]}`}</span></div>
              <div className="text-2xl font-bold">{temporaryComboTotal}</div>
            </div>
          </div>

          <div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-4">
            <Button variant="outline" disabled={starting} onClick={() => setTemporaryConfigOpen(false)}>取消</Button>
            <Button disabled={starting || !temporaryComboTotal || !selectedCount} onClick={handleTemporaryStart} className="bg-emerald-600 hover:bg-emerald-700">{starting ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}添加 {selectedCount} 个任务</Button>
          </div>
        </div>
      </div> : null}
    </div>
  );
}

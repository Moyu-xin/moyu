"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Copy, Database, ExternalLink, LoaderCircle, Pause, Play, RefreshCw, RotateCcw, Save, Settings2, Square, Trash2, UserRound, X, Zap } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  clearFailedPlusLinkTasks,
  clearFinishedPlusLinkTasks,
  clearPlusLinkTask,
  cancelPlusLinkTask,
  fetchPlusLinkState,
  pausePlusLinkTask,
  resumePlusLinkTask,
  retryPlusLinkTask,
  startPlusLinkTask,
  stopPlusLinkTask,
  updatePlusLinkConfig,
  type AccountItem,
  type PartialAccountItem,
  type PlusLinkConfig,
  type PlusLinkResult,
  type PlusLinkState,
} from "@/lib/api";

const BILLING_COUNTRIES = [
  { value: "US", label: "US · 美国" },
  { value: "DE", label: "DE · 德国" },
  { value: "FR", label: "FR · 法国" },
  { value: "IT", label: "IT · 意大利" },
  { value: "ES", label: "ES · 西班牙" },
  { value: "NL", label: "NL · 荷兰" },
  { value: "CA", label: "CA · 加拿大" },
  { value: "AU", label: "AU · 澳大利亚" },
  { value: "GB", label: "GB · 英国" },
] as const;
const PROMO_COUNTRIES = [
  { value: "JP", label: "JP · 日本" },
  { value: "TR", label: "TR · 土耳其" },
  { value: "BR", label: "BR · 巴西" },
  { value: "VN", label: "VN · 越南" },
] as const;
const DEFAULT_BILLING_ROTATION = ["US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB"];
const DEFAULT_PROXY_ROTATION = ["JP", "TR", "BR", "VN"];
const PROXY_COUNTRY_CODES = ["US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB", "JP", "TR", "BR", "VN"] as const;
const COUNTRY_LABEL: Record<string, string> = { US: "美国", DE: "德国", FR: "法国", IT: "意大利", ES: "西班牙", NL: "荷兰", CA: "加拿大", AU: "澳大利亚", GB: "英国", JP: "日本", TR: "土耳其", BR: "巴西", VN: "越南" };
const CHAIN_TEXT = "项目代理父级 → Plus 住宅代理 → 左池 checkout → 右池 promotion update / taxes → 左池 Stripe init / PayPal PM / confirm / ChatGPT approve → 右池 Stripe poll / redirect → PayPal BA approve";
const PAID_PLAN_TYPES = new Set(["plus", "pro", "team", "business", "enterprise"]);

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
  residential_via_project_proxy: true,
  residential_parent_node_filter: "",
  residential_via_clash: true,
  residential_clash_proxy: "",
  proxy: "",
  target_country: "US",
  checkout_country: "US",
  promotion_country: "JP",
  payment_method_country: "US",
  payment_locale: "en",
  concurrency: 5,
  timeout_seconds: 360,
  checkout_retry_limit: 10,
  provider_retry_limit: 15,
  matrix_combo_retry_limit: 1,
  retry_limits_version: 2,
  refresh_tokens: false,
  provider_same_as_target: false,
  approve_same_as_checkout: true,
  country_rotation_enabled: true,
  billing_country_rotation: DEFAULT_BILLING_ROTATION,
  proxy_country_rotation: DEFAULT_PROXY_ROTATION,
  require_zero_stripe_amount: true,
  strict_same_proxy_ip: false,
  strict_approve_same_proxy_ip: false,
};

type StageProxyPreview = { leftCountry: string; promotionCountry: string; checkout: string; provider: string; approve: string; promotion: string };

function mergeConfig(config?: Partial<PlusLinkConfig>): PlusLinkConfig {
  const definedConfig = Object.fromEntries(Object.entries(config || {}).filter(([, value]) => value !== undefined)) as Partial<PlusLinkConfig>;
  const legacyRetryLimits = Number(definedConfig.retry_limits_version || 0) < 2;
  const matrixRetry = Math.max(1, Number(definedConfig.matrix_combo_retry_limit || defaultConfig.matrix_combo_retry_limit));
  const configuredFixedRetry = Math.max(1, Number(definedConfig.provider_retry_limit || defaultConfig.provider_retry_limit));
  const fixedRetry = legacyRetryLimits && configuredFixedRetry === matrixRetry ? 15 : configuredFixedRetry;
  return { ...defaultConfig, ...definedConfig, provider_retry_limit: fixedRetry, matrix_combo_retry_limit: matrixRetry, retry_limits_version: 2, country_rotation_enabled: definedConfig.country_rotation_enabled !== false, engine_mode: "local", use_proxy_pool: false, use_residential_proxy_pool: true, residential_via_project_proxy: true, residential_via_clash: true, residential_clash_proxy: "", proxy: "", residential_country_proxy_pool: { ...((definedConfig.residential_country_proxy_pool || {}) as Record<string, string>) } };
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
function sequenceText(value: unknown, defaults: string[], allowed: ReadonlyArray<{ value: string }>) {
  return normalizeCodeSequence(value, defaults, allowed).join("/");
}
function countryPool(map: Record<string, string> | undefined, country: string) {
  const code = String(country || "").toUpperCase();
  if (!map) return "";
  if (map[code]) return map[code];
  return "";
}
function computeStageProxies(config: PlusLinkConfig): StageProxyPreview {
  const map = normalizeCountryMap(config.residential_country_proxy_pool);
  const leftCountry = String(config.target_country || config.checkout_country || "US").toUpperCase();
  const promotionCountry = String(config.promotion_country || "JP").toUpperCase();
  const fallback = String(config.residential_proxy_pool || "").trim();
  const checkout = countryPool(map, leftCountry) || fallback;
  const provider = checkout;
  const approve = checkout;
  const promotion = countryPool(map, promotionCountry) || fallback;
  return { leftCountry, promotionCountry, checkout, provider, approve, promotion };
}
function configForSave(config: PlusLinkConfig): PlusLinkConfig {
  const stage = computeStageProxies(config);
  const billingRotation = normalizeCodeSequence(config.billing_country_rotation, DEFAULT_BILLING_ROTATION, BILLING_COUNTRIES);
  const proxyRotation = normalizeCodeSequence(config.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES);
  const matrixComboRetryLimit = Math.max(1, Number(config.matrix_combo_retry_limit || 1));
  const fixedPairRetryLimit = Math.max(1, Number(config.provider_retry_limit || 15));
  return { ...config, engine_mode: "local", use_proxy_pool: false, use_residential_proxy_pool: true, residential_via_project_proxy: true, residential_via_clash: true, residential_clash_proxy: "", proxy: "", checkout_country: stage.leftCountry, payment_method_country: stage.leftCountry, residential_country_proxy_pool: normalizeCountryMap(config.residential_country_proxy_pool), residential_checkout_proxy: stage.checkout, residential_provider_proxy: stage.provider, residential_approve_proxy: stage.approve, residential_promotion_proxy: stage.promotion, provider_same_as_target: false, approve_same_as_checkout: true, residential_single_proxy_mode: false, country_rotation_enabled: config.country_rotation_enabled !== false, billing_country_rotation: billingRotation, proxy_country_rotation: proxyRotation, checkout_retry_limit: Math.max(1, Number(config.checkout_retry_limit || 10)), provider_retry_limit: fixedPairRetryLimit, matrix_combo_retry_limit: matrixComboRetryLimit, retry_limits_version: 2 };
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
function progressValue(result?: PlusLinkResult) { if (result?.status === "success") return 100; return Math.max(0, Math.min(100, Number(result?.progress_percent || 0))); }
function matrixPool(result: PlusLinkResult | undefined, side: "left" | "right") {
  if (side === "left") return { country: result?.left_proxy_country || result?.provider_country || result?.billing_country || "--", ip: result?.left_proxy_ip || "等待检测" };
  return { country: result?.right_proxy_country || result?.promotion_country || "--", ip: result?.right_proxy_ip || "等待检测" };
}
function taskDurationSeconds(result?: PlusLinkResult) { const fixed = Number(result?.duration_seconds || 0); if (fixed > 0 || !result?.started_at) return fixed; const started = new Date(result.started_at).getTime(); return Number.isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : 0; }
function taskDurationText(result?: PlusLinkResult) { return `${taskDurationSeconds(result).toFixed(1)}s`; }
function shortTaskId(taskId?: string) { const value = String(taskId || ""); return value.length > 16 ? value.slice(-16) : value || "--"; }
function taskAccent(status?: string) { return status === "success" ? "border-l-emerald-500" : status === "failed" || status === "cancelled" ? "border-l-rose-500" : status === "paused" || status === "pausing" ? "border-l-amber-500" : "border-l-sky-500"; }
function taskProgressColor(status?: string) { return status === "success" ? "bg-emerald-500" : status === "failed" || status === "cancelled" ? "bg-rose-400" : status === "paused" || status === "pausing" ? "bg-amber-400" : "bg-sky-500"; }
function taskStageTitle(result: PlusLinkResult) {
  const left = matrixPool(result, "left").country;
  const right = matrixPool(result, "right").country;
  const fixedPair = result.extraction_mode === "fixed_pair" || Number(result.matrix_total || 1) === 1;
  const titles: Record<string, string> = {
    queued: "等待执行",
    preparing: fixedPair ? `固定双 IP ${left} + ${right}` : `矩阵配置 ${left} + ${right}`,
    refresh: "刷新 OAuth 令牌",
    eligibility: "检测账号提链资格",
    run: "启动提链链路",
    billing: `${left} 账单资料`,
    checkout: `${left} Checkout`,
    sync: `${right} 优惠更新`,
    link_attempt: "开始本轮提链",
    provider: `${left} Provider 会话`,
    proxy: `${left} + ${right} 出口检测`,
    stripe_init: "主链路 Stripe init",
    stripe_methods: "确认 PayPal 支付方式",
    payment_method: "创建 PayPal 支付方式",
    stripe_amount: "校验 Stripe 金额",
    confirm: "确认 Stripe Checkout",
    approve: `${left} ChatGPT approve`,
    stripe_redirect: `${right} Stripe poll`,
    redirect: `${right} 最终跳转`,
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
  const left = matrixPool(result, "left");
  const right = matrixPool(result, "right");
  const attempt = Number(result.link_attempt_count || 0);
  const limit = Number(result.link_attempt_limit || 0);
  if (result.status === "success") return `第 ${Math.max(1, attempt)} 次提链成功，长链已经生成。`;
  if (result.status === "failed") return result.error || "本轮链路未能生成长链，可直接重试该任务。";
  if (result.status === "cancelled") return "任务已停止，可保留当前记录或重新发起提链。";
  if (result.status === "paused" || result.status === "pausing") return "当前任务已暂停，继续后将从当前链路检查点恢复。";
  const descriptions: Record<string, string> = {
    queued: "等待工作线程接管该账号。",
    preparing: `正在分配矩阵组合：左池 ${left.country}，右池 ${right.country}。`,
    refresh: "正在刷新账号令牌，完成后进入 Checkout。",
    run: "账号和代理已经准备完成，正在启动提链。",
    billing: `正在生成 ${left.country} 随机账单资料。`,
    checkout: `正在通过左池 ${left.country} 创建 PayPal Checkout。`,
    sync: `保留同一 Checkout，正在通过右池 ${right.country} 更新优惠和税区。`,
    link_attempt: `正在执行第 ${Math.max(1, attempt)}/${Math.max(1, limit)} 次提链尝试。`,
    provider: `正在通过左池 ${left.country} 创建 Stripe/Provider 会话。`,
    proxy: `正在验证左池 ${left.country} 与右池 ${right.country} 的实际出口。`,
    stripe_init: "优惠已更新，正在回到主链路初始化 Stripe。",
    stripe_methods: "正在确认当前 Stripe 会话包含 PayPal 支付方式。",
    payment_method: "正在创建 PayPal payment method。",
    stripe_amount: "正在确认当前 Checkout 金额符合提链条件。",
    confirm: "正在绑定支付方式并确认 Stripe Checkout。",
    approve: `正在通过左池 ${left.country} 请求 ChatGPT approve。`,
    stripe_redirect: `正在通过右池 ${right.country} 轮询 Stripe 跳转结果。`,
    redirect: "正在解析最终 PayPal BA approve 长链。",
    combo_result: "本轮组合已结束，失败时将自动进入下一次尝试。",
    done: "已获得可复制和打开的长链。",
  };
  return descriptions[result.engine_step || ""] || "正在执行当前提链步骤。";
}

function CountrySelect({ value, options, onChange }: { value?: string; options: ReadonlyArray<{ value: string; label: string }>; onChange: (value: string) => void }) {
  return <Select value={String(value || options[0]?.value || "")} onValueChange={onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{options.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectContent></Select>;
}
function FieldLabel({ children, hint }: { children: ReactNode; hint?: string }) { return <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>{children}</span>{hint ? <span className="block text-xs font-normal text-slate-500">{hint}</span> : null}</label>; }

export function PlusLinkCard() {
  const [state, setState] = useState<PlusLinkState | null>(null);
  const [draft, setDraft] = useState<PlusLinkConfig>(() => mergeConfig());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [selectedEmails, setSelectedEmails] = useState<string[]>([]);
  const [selectedPartials, setSelectedPartials] = useState<string[]>([]);
  const [manualText, setManualText] = useState("");
  const [accountQuery, setAccountQuery] = useState("");
  const [partialQuery, setPartialQuery] = useState("");
  const [accountGroupFilter, setAccountGroupFilter] = useState("all");
  const [partialGroupFilter, setPartialGroupFilter] = useState("all");
  const [showPlusAccounts, setShowPlusAccounts] = useState(false);
  const [taskActionId, setTaskActionId] = useState("");
  const draftDirtyRef = useRef(false);
  const audioContextRef = useRef<AudioContext | null>(null);
  const successSoundArmedRef = useRef(false);
  const knownSuccessTaskIdsRef = useRef<Set<string>>(new Set());

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
  const stagePreview = useMemo(() => computeStageProxies(draft), [draft]);
  const matrixEnabled = draft.country_rotation_enabled !== false;
  const manualTokenLines = useMemo(() => manualLines(manualText), [manualText]);
  const selectedCount = selectedEmails.length + selectedPartials.length + manualTokenLines.length;
  const successLinks = useMemo(() => (state?.results || []).filter((item) => item.status === "success" && item.long_url), [state]);

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

  const load = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const payload = await fetchPlusLinkState();
      setState(payload.plus_link);
      if (!draftDirtyRef.current) setDraft(mergeConfig(payload.plus_link.config));
      const validEmailSelectors = new Set((payload.plus_link.account_pool?.accounts || []).map(accountSelector));
      setSelectedEmails((items) => items.filter((item) => validEmailSelectors.has(item)));
      const validPartialSelectors = new Set((payload.plus_link.partial_account_pool?.items || []).map(partialSelector));
      setSelectedPartials((items) => items.filter((item) => validPartialSelectors.has(item)));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Plus 提链状态刷新失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 2500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!successSoundArmedRef.current) return;
    const successIds = new Set((state?.results || []).filter((item) => item.status === "success").map((item) => item.task_id));
    const hasNewSuccess = Array.from(successIds).some((taskId) => !knownSuccessTaskIdsRef.current.has(taskId));
    knownSuccessTaskIdsRef.current = successIds;
    if (hasNewSuccess) playSuccessSound();
  }, [state?.results]);

  const patchDraft = (updates: Partial<PlusLinkConfig>) => { draftDirtyRef.current = true; setDraft((current) => mergeConfig({ ...current, ...updates })); };
  const patchCountryProxy = (code: string, value: string) => {
    draftDirtyRef.current = true;
    setDraft((current) => {
      const map = { ...normalizeCountryMap(current.residential_country_proxy_pool), [code]: value };
      if (!value.trim()) delete map[code];
      return mergeConfig({ ...current, use_residential_proxy_pool: true, residential_country_proxy_pool: map });
    });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload = await updatePlusLinkConfig(configForSave(draft));
      setState(payload.plus_link);
      setDraft(mergeConfig(payload.plus_link.config));
      draftDirtyRef.current = false;
      toast.success("Plus 提链配置已保存");
    } catch (error) { toast.error(error instanceof Error ? error.message : "保存配置失败"); }
    finally { setSaving(false); }
  };
  const handleStart = async () => {
    if (!selectedCount) { toast.warning("请先选择库存/半成品账号，或粘贴 access_token/session"); return; }
    armSuccessSound();
    knownSuccessTaskIdsRef.current = new Set();
    setStarting(true);
    try {
      const payload = await startPlusLinkTask({ emails: selectedEmails, partial_accounts: selectedPartials, manual_tokens: manualTokenLines, clear_results: true, config: configForSave(draft) });
      setState(payload.plus_link);
      setDraft(mergeConfig(payload.plus_link.config));
      draftDirtyRef.current = false;
      toast.success(`Plus 提链已启动：${selectedCount} 个任务`);
    } catch (error) { toast.error(error instanceof Error ? error.message : "启动提链失败"); }
    finally { setStarting(false); }
  };
  const handleStop = async () => { try { const payload = await stopPlusLinkTask(); setState(payload.plus_link); toast.success("已请求停止 Plus 提链"); } catch (error) { toast.error(error instanceof Error ? error.message : "停止失败"); } };
  const handleClear = async () => { try { const payload = await clearPlusLinkTask(); setState(payload.plus_link); toast.success("已清空提链日志和结果"); } catch (error) { toast.error(error instanceof Error ? error.message : "清空失败"); } };
  const handleBulkClear = async (scope: "failed" | "finished") => {
    try {
      const payload = await (scope === "failed" ? clearFailedPlusLinkTasks() : clearFinishedPlusLinkTasks());
      setState(payload.plus_link);
      toast.success(scope === "failed" ? "已清除失败和停止任务" : "已清理全部结束任务");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清理任务失败");
    }
  };
  const handleTaskAction = async (taskId: string, action: "pause" | "resume" | "cancel" | "retry") => {
    setTaskActionId(`${taskId}:${action}`);
    try {
      if (action === "retry") armSuccessSound();
      const request = action === "pause" ? pausePlusLinkTask : action === "resume" ? resumePlusLinkTask : action === "cancel" ? cancelPlusLinkTask : retryPlusLinkTask;
      const payload = await request(taskId);
      setState(payload.plus_link);
      toast.success(action === "pause" ? "任务将在当前步骤结束后暂停" : action === "resume" ? "任务已继续" : action === "cancel" ? "已请求取消任务" : "任务已重新排队");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "任务操作失败");
    } finally {
      setTaskActionId("");
    }
  };
  const openLongUrl = (url: string) => { window.open(url, "_blank", "noopener,noreferrer"); };
  const taskResults = useMemo(() => [...(state?.results || [])].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))), [state?.results]);
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
            <div className="space-y-2">
              <div className="flex items-center gap-2"><span className="flex size-10 items-center justify-center rounded-2xl bg-emerald-400/15 text-emerald-200"><Zap className="size-5" /></span><div><h2 className="text-xl font-bold">Plus 提链总览</h2><p className="text-sm text-slate-300">本地调用内置提链代码，代理、账号、日志集中在本页完成。</p></div></div>
              <div className="rounded-2xl border border-emerald-300/20 bg-emerald-300/10 px-4 py-3 text-sm font-semibold leading-6 text-emerald-100">当前链路：{CHAIN_TEXT}</div>
            </div>
            <div className="grid min-w-[320px] grid-cols-2 gap-3 sm:grid-cols-5 lg:grid-cols-5">
              {[["总数", stats.total || 0], ["运行中", stats.running || 0], ["成功", stats.success || 0], ["失败", stats.fail || 0], ["耗时", formatSeconds(stats.elapsed_seconds)]].map(([label, value]) => <div key={label} className="rounded-2xl border border-white/10 bg-white/10 px-3 py-2 text-center backdrop-blur"><div className="text-xs text-slate-300">{label}</div><div className="mt-1 text-lg font-bold text-white">{value}</div></div>)}
            </div>
          </div>
        </div>
      </section>

      <Card className="border-emerald-100 bg-white/95 shadow-sm">
        <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div><CardTitle className="flex items-center gap-2 text-lg"><Settings2 className="size-5 text-emerald-600" /> 双池提链</CardTitle><CardDescription>固定双 IP 与矩阵轮询使用独立重试次数；左池负责 checkout/provider/approve，右池负责优惠、税区、Stripe poll 和最终跳转。</CardDescription></div>
          <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => void load()} disabled={saving || starting}><RefreshCw className="size-4" /> 刷新</Button><Button onClick={handleSave} disabled={saving} className="bg-emerald-600 hover:bg-emerald-700">{saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />} 保存配置</Button></div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-5">
            <div className="space-y-2"><FieldLabel>左池国家</FieldLabel><CountrySelect value={draft.target_country} options={BILLING_COUNTRIES} onChange={(value) => patchDraft({ target_country: value, checkout_country: value, payment_method_country: value })} /></div>
            <div className="space-y-2"><FieldLabel>PayPal/PM 账单国家</FieldLabel><CountrySelect value={draft.payment_method_country} options={BILLING_COUNTRIES} onChange={(value) => patchDraft({ payment_method_country: value })} /></div>
            <div className="space-y-2"><FieldLabel>右池优惠国家</FieldLabel><CountrySelect value={draft.promotion_country} options={PROMO_COUNTRIES} onChange={(value) => patchDraft({ promotion_country: value })} /></div>
            <div className="space-y-2"><FieldLabel>并发</FieldLabel><Input type="number" min={1} max={10} value={draft.concurrency || 1} onChange={(event) => patchDraft({ concurrency: Number(event.target.value || 1) })} /></div>
            <div className="space-y-2"><FieldLabel>Checkout/approve 重试</FieldLabel><Input type="number" min={1} max={50} value={draft.checkout_retry_limit || 10} onChange={(event) => patchDraft({ checkout_retry_limit: Number(event.target.value || 10) })} /></div>
          </div>

          <div className="rounded-3xl border border-indigo-100 bg-indigo-50/70 p-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-2">
                <div className="inline-flex rounded-lg border border-indigo-200 bg-white p-1" role="group" aria-label="提链模式">
                  <Button type="button" size="sm" variant={!matrixEnabled ? "default" : "ghost"} aria-pressed={!matrixEnabled} onClick={() => patchDraft({ country_rotation_enabled: false })}>固定双 IP</Button>
                  <Button type="button" size="sm" variant={matrixEnabled ? "default" : "ghost"} aria-pressed={matrixEnabled} onClick={() => patchDraft({ country_rotation_enabled: true })}>9 × 4 矩阵</Button>
                </div>
                <p className="max-w-2xl text-xs leading-5 text-indigo-700">{matrixEnabled ? "每个账号依次轮询全部国家组合，每种组合使用矩阵专属重试次数。" : `固定使用左池 ${stagePreview.leftCountry} 与右池 ${stagePreview.promotionCountry}，只使用固定模式重试次数。`}</p>
              </div>
              <div className="flex min-w-64 items-end gap-3">
                {matrixEnabled ? <div className="flex-1 space-y-2"><FieldLabel hint="当前国家组合最多尝试 N 次，失败后切换下一组合；不会修改固定模式次数。">矩阵每种配置次数</FieldLabel><Input type="number" min={1} max={20} value={draft.matrix_combo_retry_limit || 1} onChange={(event) => patchDraft({ matrix_combo_retry_limit: Number(event.target.value || 1) })} /></div> : <div className="flex-1 space-y-2"><FieldLabel hint="固定左/右代理组合整链重跑次数；不会修改矩阵次数。">固定双 IP 重试次数</FieldLabel><Input type="number" min={1} max={20} value={draft.provider_retry_limit || 15} onChange={(event) => patchDraft({ provider_retry_limit: Number(event.target.value || 15) })} /></div>}
                <Badge variant="info">{matrixEnabled ? "9 × 4 = 36" : "固定 1 组"}</Badge>
              </div>
            </div>
            {matrixEnabled ? <div className="mt-4 grid gap-3 md:grid-cols-2">
              <div className="space-y-2"><FieldLabel hint="默认：US/DE/FR/IT/ES/NL/CA/AU/GB">左池国家序列</FieldLabel><Input value={sequenceText(draft.billing_country_rotation, DEFAULT_BILLING_ROTATION, BILLING_COUNTRIES)} onChange={(event) => patchDraft({ billing_country_rotation: event.target.value })} placeholder="US/DE/FR/IT/ES/NL/CA/AU/GB" /></div>
              <div className="space-y-2"><FieldLabel hint="默认：JP/TR/BR/VN">右池国家序列</FieldLabel><Input value={sequenceText(draft.proxy_country_rotation, DEFAULT_PROXY_ROTATION, PROMO_COUNTRIES)} onChange={(event) => patchDraft({ proxy_country_rotation: event.target.value })} placeholder="JP/TR/BR/VN" /></div>
            </div> : null}
          </div>

          <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
            <div className="rounded-3xl border border-amber-200 bg-amber-50/70 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3"><div className="text-base font-bold text-amber-950">Plus 专用按国家住宅代理池</div><Badge variant="warning">711 模板：USER-zone-custom-region-JP:pass@host:port</Badge></div>
              <p className="mt-2 text-sm leading-6 text-amber-800">左池 {stagePreview.leftCountry} 供 Checkout / Provider / Approve 共用；右池 {stagePreview.promotionCountry} 供 Update / Taxes / Poll / Redirect 使用。</p>
              <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {PROXY_COUNTRY_CODES.map((code) => <div key={code} className="space-y-1.5"><div className="flex items-center justify-between"><span className="text-sm font-bold text-amber-950">{code} · {COUNTRY_LABEL[code]}</span><Badge variant="outline">{lineCount(draft.residential_country_proxy_pool?.[code])} 条</Badge></div><Textarea className="min-h-24 resize-y bg-white/90 font-mono text-xs leading-5" placeholder={`USER123456-zone-custom-region-${code}:fixturepass@global.example.711proxy.invalid:10000`} value={draft.residential_country_proxy_pool?.[code] || ""} onChange={(event) => patchCountryProxy(code, event.target.value)} /></div>)}
              </div>
            </div>

            <div className="space-y-4 rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div><div className="flex items-center justify-between gap-2"><div className="text-sm font-bold text-slate-900">Plus 专用公共住宅模板</div><Badge variant="outline">业务出口保持住宅</Badge></div><p className="mt-1 text-xs leading-5 text-slate-500">对应国家为空时才回退，支持 {'{country}'} / {'{session}'} 占位符。</p><Textarea className="mt-2 min-h-28 resize-y bg-white font-mono text-xs leading-5" placeholder="http://user:pass@host:port" value={draft.residential_proxy_pool || ""} onChange={(event) => patchDraft({ residential_proxy_pool: event.target.value })} /></div>
              <div className="rounded-2xl border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900"><div className="flex items-center justify-between gap-2"><span className="font-bold">项目代理配置 → 住宅代理</span><Badge variant="info">强制两跳</Badge></div><div className="mt-2 text-xs leading-5 text-sky-700">父级节点由项目代理配置自动分配；Checkout、Stripe 与 PayPal 的最终出口仍是 Plus 住宅代理。</div><div className="mt-3 space-y-1.5"><FieldLabel hint="例如 anytls-aw">父代理节点名称筛选</FieldLabel><Input value={draft.residential_parent_node_filter || ""} onChange={(event) => patchDraft({ residential_parent_node_filter: event.target.value })} placeholder="留空表示全部项目节点" /></div></div>
              <label className="flex items-center gap-3 text-sm font-semibold text-slate-700"><Checkbox checked={draft.refresh_tokens} onCheckedChange={(checked) => patchDraft({ refresh_tokens: checked })} />提链前先刷新 refresh_token</label>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-4">
            {[["Checkout", stagePreview.leftCountry, stagePreview.checkout], ["Provider / Approve", stagePreview.leftCountry, stagePreview.provider], ["Promotion / Taxes", stagePreview.promotionCountry, stagePreview.promotion], ["Poll / Redirect", stagePreview.promotionCountry, stagePreview.promotion]].map(([title, code, value]) => <div key={title} className="rounded-3xl border border-emerald-100 bg-emerald-50/70 p-4"><div className="flex items-center justify-between gap-2"><div className="text-sm font-bold text-emerald-950">{title}</div><Badge variant="success">自动取 {code}</Badge></div><Textarea readOnly className="mt-2 min-h-24 resize-none bg-white/80 font-mono text-xs leading-5" value={String(value || "")} /><div className="mt-2 text-xs text-emerald-700">当前池：{lineCount(String(value || ""))} 条</div></div>)}
          </div>
        </CardContent>
      </Card>

      <Card className="border-slate-200 bg-white/95 shadow-sm">
        <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div><CardTitle className="flex items-center gap-2 text-lg"><UserRound className="size-5 text-sky-600" /> 账号选择</CardTitle><CardDescription>支持筛选库存/半成品账号；库存默认隐藏 Plus 账号，也可以直接粘贴多个 access_token 或 session。</CardDescription></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setSelectedEmails(filteredEligibleAccounts.map(accountSelector))} disabled={!filteredEligibleAccounts.length || state?.enabled}>选择筛选库存</Button>
            <Button variant="outline" onClick={() => setSelectedPartials(filteredEligiblePartials.map(partialSelector))} disabled={!filteredEligiblePartials.length || state?.enabled}>选择筛选半成品</Button>
            <Button variant="outline" onClick={() => { setSelectedEmails([]); setSelectedPartials([]); }} disabled={state?.enabled || !selectedCount}>清除选择</Button>
            <Button onClick={handleStart} disabled={Boolean(state?.enabled) || starting || !selectedCount} className="bg-slate-950 hover:bg-slate-800">{starting ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />} 开始提链（{selectedCount}）</Button>
            <Button variant="destructive" onClick={handleStop} disabled={!state?.enabled}><Square className="size-4" /> 停止</Button>
            <Button variant="outline" onClick={handleClear} disabled={Boolean(state?.enabled)}><Trash2 className="size-4" /> 清空日志</Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div className="mb-3 flex items-center justify-between gap-2"><div className="flex items-center gap-2 font-bold text-slate-900"><Database className="size-4 text-slate-500" /> 库存账号</div><Badge variant="outline">筛选 {filteredEligibleAccounts.length} / 可用 {eligibleAccounts.length} / 总计 {accounts.length} / 隐藏付费 {showPlusAccounts ? 0 : hiddenPaidAccounts}</Badge></div>
              <div className="mb-3 grid gap-2 sm:grid-cols-[1fr_150px_auto]"><Input value={accountQuery} onChange={(event) => setAccountQuery(event.target.value)} placeholder="筛选邮箱 / plan / token 状态 / 分组" /><select value={accountGroupFilter} onChange={(event) => setAccountGroupFilter(event.target.value)} className="h-10 rounded-2xl border border-white bg-white px-3 text-sm text-slate-700"><option value="all">全部分组</option>{accountGroups.map((group) => <option key={group} value={group}>{group}</option>)}</select><label className="flex items-center gap-2 rounded-2xl border border-white bg-white px-3 text-sm font-semibold text-slate-700"><Checkbox checked={showPlusAccounts} onCheckedChange={(checked) => setShowPlusAccounts(Boolean(checked))} />显示 Plus 账号</label></div>
              <div className="max-h-72 space-y-2 overflow-auto pr-1">
                {filteredEligibleAccounts.length ? filteredEligibleAccounts.map((account) => {
                  const selector = accountSelector(account);
                  const effectivePlan = accountEffectivePlan(account);
                  const rawPlan = normalizePlanValue(account.plan_type);
                  return <label key={selector} className="flex items-start gap-3 rounded-2xl border border-white bg-white p-3 text-sm shadow-sm"><Checkbox checked={selectedEmails.includes(selector)} onCheckedChange={(checked) => setSelectedEmails((items) => selectedToggle(items, selector, checked))} disabled={state?.enabled} /><span className="min-w-0 flex-1"><span className="block truncate font-semibold text-slate-900">{account.email}</span><span className="mt-1 flex flex-wrap gap-1 text-xs text-slate-500"><Badge variant="secondary">{mailboxGroupLabel(account)}</Badge><Badge variant={planBadgeVariant(effectivePlan)}>{effectivePlan}</Badge>{rawPlan && rawPlan !== effectivePlan ? <Badge variant="outline">原始 {rawPlan}</Badge> : null}{account.oauth_status ? <Badge variant="outline">{account.oauth_status}</Badge> : null}{account.has_access_token ? <Badge variant="success">access_token</Badge> : null}{account.has_refresh_token ? <Badge variant="info">refresh_token</Badge> : null}</span></span></label>;
                }) : <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">暂无可用库存账号</div>}
              </div>
            </div>
            <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div className="mb-3 flex items-center justify-between gap-2"><div className="flex items-center gap-2 font-bold text-slate-900"><CheckCircle2 className="size-4 text-slate-500" /> 半成品账号</div><Badge variant="outline">筛选 {filteredEligiblePartials.length} / 可用 {eligiblePartials.length} / 总计 {partialAccounts.length}</Badge></div>
              <div className="mb-3 grid gap-2 sm:grid-cols-[1fr_150px]"><Input value={partialQuery} onChange={(event) => setPartialQuery(event.target.value)} placeholder="筛选邮箱 / 阶段 / 状态 / 分组" /><select value={partialGroupFilter} onChange={(event) => setPartialGroupFilter(event.target.value)} className="h-10 rounded-2xl border border-white bg-white px-3 text-sm text-slate-700"><option value="all">全部分组</option>{partialGroups.map((group) => <option key={group} value={group}>{group}</option>)}</select></div>
              <div className="max-h-72 space-y-2 overflow-auto pr-1">
                {filteredEligiblePartials.length ? filteredEligiblePartials.map((account) => {
                  const selector = partialSelector(account);
                  return <label key={selector} className="flex items-start gap-3 rounded-2xl border border-white bg-white p-3 text-sm shadow-sm"><Checkbox checked={selectedPartials.includes(selector)} onCheckedChange={(checked) => setSelectedPartials((items) => selectedToggle(items, selector, checked))} disabled={state?.enabled} /><span className="min-w-0 flex-1"><span className="block truncate font-semibold text-slate-900">{account.email}</span><span className="mt-1 flex flex-wrap gap-1 text-xs text-slate-500"><Badge variant="secondary">{mailboxGroupLabel(account)}</Badge>{account.stage ? <Badge variant="outline">{account.stage}</Badge> : null}{account.has_access_token ? <Badge variant="success">access_token</Badge> : null}{account.has_session_token ? <Badge variant="warning">session</Badge> : null}{account.has_refresh_token ? <Badge variant="info">refresh_token</Badge> : null}</span></span></label>;
                }) : <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">暂无带 token/session 的半成品账号</div>}
              </div>
            </div>
          </div>
          <div className="rounded-3xl border border-violet-100 bg-violet-50/70 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><div><div className="font-bold text-violet-950">手动 access_token / session</div><div className="text-xs text-violet-700">每行一个；支持纯 token、Bearer token、email token 或 JSON。</div></div><Badge variant="violet">{manualTokenLines.length} 条</Badge></div>
            <Textarea className="min-h-36 resize-y bg-white font-mono text-xs leading-5" placeholder={'user@example.com access_token=eyJ...\nBearer eyJ...\n{"email":"a@b.com","access_token":"eyJ..."}'} value={manualText} onChange={(event) => setManualText(event.target.value)} disabled={state?.enabled} />
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
              <Button size="sm" variant="outline" disabled={!successLinks.length} onClick={() => copyText(successLinks.map((item) => item.long_url || "").filter(Boolean).join("\n"), "成功长链已复制")}><Copy className="size-3.5" /> 复制成功链接</Button>
              <Button size="sm" variant="outline" disabled={!successLinks.length} onClick={() => copyText(successLinks.map((item) => item.email).filter(Boolean).join("\n"), "成功邮箱已复制")}><Copy className="size-3.5" /> 复制成功邮箱</Button>
              <Button size="sm" variant="outline" disabled={!taskSummary.failed} onClick={() => void handleBulkClear("failed")}><Trash2 className="size-3.5" /> 清除失败</Button>
              <Button size="sm" variant="outline" disabled={!taskSummary.completed && !taskSummary.failed} onClick={() => void handleBulkClear("finished")}><Trash2 className="size-3.5" /> 清理已结束</Button>
              <Button size="sm" variant="outline" onClick={() => void load(true)}><RefreshCw className="size-3.5" /> 刷新</Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="bg-slate-50 p-4">
          {taskResults.length ? <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">
            {taskResults.map((result, index) => {
              const progress = progressValue(result);
              const paused = result.status === "paused" || result.status === "pausing";
              const active = ["queued", "running", "paused", "pausing", "cancelling"].includes(result.status);
              const terminal = ["success", "failed", "cancelled"].includes(result.status);
              const busy = taskActionId.startsWith(`${result.task_id}:`);
              const leftPool = matrixPool(result, "left");
              const rightPool = matrixPool(result, "right");
              return <div key={result.task_id} className={`overflow-hidden rounded-lg border border-l-4 border-slate-200 bg-white shadow-sm ${taskAccent(result.status)}`}>
                <div className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="text-[10px] font-bold uppercase tracking-wide text-rose-500">TASK {taskResults.length - index}</div>
                      <div className="mt-1 truncate text-sm font-bold text-slate-950" title={result.email}>{result.email || result.task_id}</div>
                      <div className="mt-2 flex flex-wrap gap-1.5"><span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-mono text-[10px] text-slate-500">{shortTaskId(result.task_id)}</span>{result.matrix_total ? <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[10px] text-slate-500">矩阵 {result.matrix_index || 1}/{result.matrix_total}</span> : null}</div>
                    </div>
                    <Badge variant={statusVariant(result.status)}>{result.status === "success" ? "已完成" : statusText(result.status)}</Badge>
                  </div>

                  <div className="mt-4">
                    <div className="mb-1.5 flex items-center justify-between gap-3 text-xs"><span className="truncate font-semibold text-slate-700">{taskStageTitle(result)}</span><span className="shrink-0 font-bold text-slate-700">{progress}%</span></div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className={`h-full rounded-full transition-[width] duration-500 ${taskProgressColor(result.status)}`} style={{ width: `${progress}%` }} /></div>
                    <div className="mt-2 min-h-10 text-xs leading-5 text-slate-500">{taskStageDescription(result)}</div>
                  </div>

                  <div className="mt-3 flex min-w-0 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px]">
                    <span className="shrink-0 font-semibold text-sky-700">左池 {leftPool.country}</span><span className="min-w-0 truncate font-mono text-slate-700" title={leftPool.ip}>{leftPool.ip}</span><span className="shrink-0 text-slate-300">→</span><span className="shrink-0 font-semibold text-violet-700">右池 {rightPool.country}</span><span className="min-w-0 truncate font-mono text-slate-700" title={rightPool.ip}>{rightPool.ip}</span>
                  </div>

                  <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">类型</div><div className="mt-1 font-semibold text-slate-800">PayPal</div></div>
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">尝试</div><div className="mt-1 font-semibold text-slate-800">{result.link_attempt_count || 0}/{result.link_attempt_limit || result.active_retry_limit || (matrixEnabled ? draft.matrix_combo_retry_limit : draft.provider_retry_limit) || 1} 次</div></div>
                    <div className="rounded-lg border border-slate-200 bg-white p-2"><div className="text-[10px] text-slate-400">耗时</div><div className="mt-1 font-semibold text-slate-800">{taskDurationText(result)}</div></div>
                  </div>

                  {result.long_url ? <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3"><div className="text-[10px] font-semibold text-emerald-700">提取结果</div><div className="mt-1 truncate font-mono text-xs text-emerald-950" title={result.long_url}>{result.long_url}</div><div className="mt-2 flex gap-2"><Button size="sm" variant="outline" onClick={() => copyText(result.long_url || "", "长链已复制")}><Copy className="size-3.5" /> 复制长链</Button><Button size="sm" variant="outline" onClick={() => openLongUrl(result.long_url || "")}><ExternalLink className="size-3.5" /> 打开长链</Button></div></div> : null}

                  <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-200 pt-3">
                    {active && result.status !== "cancelling" ? paused ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "resume")}><Play className="size-3.5" /> 继续</Button> : <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "pause")}><Pause className="size-3.5" /> 暂停</Button> : null}
                    {active ? <Button size="sm" variant="destructive" disabled={busy || result.status === "cancelling"} onClick={() => void handleTaskAction(result.task_id, "cancel")}><X className="size-3.5" /> 停止任务</Button> : null}
                    {terminal ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void handleTaskAction(result.task_id, "retry")}><RotateCcw className="size-3.5" /> 重试</Button> : null}
                  </div>
                </div>
              </div>;
            })}
          </div> : <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">暂无提链任务，配置代理和账号后点击开始提链。</div>}
        </CardContent>
      </Card>
    </div>
  );
}

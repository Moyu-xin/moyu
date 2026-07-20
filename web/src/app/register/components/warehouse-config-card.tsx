"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Database, Images, LoaderCircle, RefreshCw, Save, ServerCog, TestTube2, TimerReset, Warehouse } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  fetchCpaConfig,
  fetchGrok2ApiConfig,
  fetchSub2ApiConfig,
  fetchSub2ApiGroups,
  testCpaConnection,
  testGrok2ApiConnection,
  testSub2ApiConnection,
  updateCpaConfig,
  updateGrok2ApiConfig,
  updateSub2ApiConfig,
  type CpaConfig,
  type Grok2ApiConfig,
  type Sub2ApiConfig,
} from "@/lib/api";

import { useSettingsStore } from "../store";

type WarehouseKind = "sub2api" | "cpa" | "grok2api";
type Sub2ApiDraft = Sub2ApiConfig & {
  admin_token?: string;
  password?: string;
  group_ids_text: string;
};
type CpaDraft = CpaConfig & { management_key?: string };
type Grok2ApiDraft = Grok2ApiConfig & { password?: string; admin_token?: string };
type Sub2ApiGroupOption = { id: string; name: string };

function normalizeGroupOptions(items: Array<Record<string, unknown>>): Sub2ApiGroupOption[] {
  const seen = new Set<string>();
  return items.flatMap((item, index) => {
    const rawId = item.id ?? item.group_id ?? item.groupId ?? item.value;
    const id = String(rawId ?? "").trim();
    if (!id || seen.has(id)) return [];
    seen.add(id);
    const name = String(item.name ?? item.group_name ?? item.groupName ?? item.display_name ?? `分组 ${index + 1}`).trim();
    return [{ id, name: name || `分组 ${id}` }];
  });
}

function ToggleRow({ checked, onChange, title, detail, danger = false }: { checked: boolean; onChange: (value: boolean) => void; title: string; detail: string; danger?: boolean }) {
  return (
    <label className={`flex cursor-pointer items-start gap-3 border px-3 py-3 ${danger ? "border-rose-200 bg-rose-50/70" : "border-slate-200 bg-slate-50"}`}>
      <Checkbox checked={checked} onCheckedChange={(value) => onChange(Boolean(value))} />
      <span>
        <span className={`block text-sm font-semibold ${danger ? "text-rose-900" : "text-slate-900"}`}>{title}</span>
        <span className={`mt-1 block text-xs leading-5 ${danger ? "text-rose-700" : "text-slate-500"}`}>{detail}</span>
      </span>
    </label>
  );
}

function ProviderBubble({ active, enabled, title, description, url, icon: Icon, onClick }: { active: boolean; enabled: boolean; title: string; description: string; url: string; icon: typeof Warehouse; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-w-0 border p-4 text-left transition ${active ? "border-emerald-400 bg-emerald-50 shadow-sm" : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"}`}
    >
      <span className="flex items-start justify-between gap-3">
        <span className={`flex size-11 shrink-0 items-center justify-center ${active ? "bg-emerald-600 text-white" : "bg-slate-100 text-slate-600"}`}><Icon className="size-5" /></span>
        <Badge variant={enabled ? "success" : "secondary"} className="rounded-md">{enabled ? "已启用" : "未启用"}</Badge>
      </span>
      <span className="mt-4 block text-base font-semibold text-slate-950">{title}</span>
      <span className="mt-1 block text-xs leading-5 text-slate-500">{description}</span>
      <span className="mt-3 block truncate font-mono text-[11px] text-slate-400">{url || "尚未配置地址"}</span>
    </button>
  );
}

export function WarehouseConfigCard() {
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);
  const [active, setActive] = useState<WarehouseKind>("sub2api");
  const [sub2api, setSub2Api] = useState<Sub2ApiDraft | null>(null);
  const [cpa, setCpa] = useState<CpaDraft | null>(null);
  const [grok2api, setGrok2Api] = useState<Grok2ApiDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupOptions, setGroupOptions] = useState<Sub2ApiGroupOption[]>([]);

  const load = async () => {
    setLoading(true);
    try {
      const [subData, cpaData, grok2apiData] = await Promise.all([fetchSub2ApiConfig(), fetchCpaConfig(), fetchGrok2ApiConfig()]);
      setSub2Api({
        ...subData.sub2api_config,
        admin_token: "",
        password: "",
        group_ids_text: String((subData.sub2api_config.group_ids || [])[0] || ""),
      });
      setCpa({ ...cpaData.cpa_config, management_key: "" });
      setGrok2Api({ ...grok2apiData.grok2api_config, password: "", admin_token: "" });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载中转仓库配置失败");
    } finally {
      setLoading(false);
    }
  };

  const loadSub2ApiGroups = async (announce = false) => {
    setGroupsLoading(true);
    try {
      const data = await fetchSub2ApiGroups();
      const result = data.sub2api_groups;
      if (!result.ok) throw new Error((result.errors || []).join("；") || "sub2api 未返回分组");
      const options = normalizeGroupOptions(result.items || []);
      setGroupOptions(options);
      if (announce) toast.success(`已获取 ${options.length} 个 sub2api 分组`);
    } catch (error) {
      if (announce) toast.error(error instanceof Error ? error.message : "获取 sub2api 分组失败");
    } finally {
      setGroupsLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (sub2api?.enabled) void loadSub2ApiGroups(false);
  }, [sub2api?.enabled]);

  const patchSub = (updates: Partial<Sub2ApiDraft>) => setSub2Api((current) => current ? { ...current, ...updates } : current);
  const patchCpa = (updates: Partial<CpaDraft>) => setCpa((current) => current ? { ...current, ...updates } : current);
  const patchGrok2Api = (updates: Partial<Grok2ApiDraft>) => setGrok2Api((current) => current ? { ...current, ...updates } : current);

  const saveSub2Api = async () => {
    if (!sub2api) return;
    setSaving(true);
    try {
      const data = await updateSub2ApiConfig({
        enabled: sub2api.enabled,
        base_url: sub2api.base_url,
        admin_token: sub2api.admin_token || undefined,
        username: sub2api.username,
        password: sub2api.password || undefined,
        skip_ssl_verify: sub2api.skip_ssl_verify,
        import_method: sub2api.import_method,
        group_ids: sub2api.group_ids_text,
        concurrency: Number(sub2api.concurrency) || 10,
        priority: Number(sub2api.priority) || 1,
        rate_multiplier: Number(sub2api.rate_multiplier) || 1,
        load_factor: Number(sub2api.load_factor) || 10,
        auto_pause_on_expired: sub2api.auto_pause_on_expired,
        auto_import: sub2api.auto_import,
        scheduled_enabled: sub2api.scheduled_enabled,
        check_interval_minutes: Number(sub2api.check_interval_minutes) || 60,
        test_concurrency: Number(sub2api.test_concurrency) || 5,
        test_model_id: sub2api.test_model_id,
        test_prompt: sub2api.test_prompt,
        test_mode: sub2api.test_mode,
        disable_on_failure: sub2api.disable_on_failure,
        delete_on_failure: sub2api.delete_on_failure,
        notes: sub2api.notes,
      });
      setSub2Api({
        ...data.sub2api_config,
        admin_token: "",
        password: "",
        group_ids_text: String((data.sub2api_config.group_ids || [])[0] || ""),
      });
      setRegisterConfig(data.register);
      toast.success("sub2api 仓库配置已保存");
      await loadSub2ApiGroups(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存 sub2api 配置失败");
    } finally {
      setSaving(false);
    }
  };

  const saveCpa = async () => {
    if (!cpa) return;
    setSaving(true);
    try {
      const data = await updateCpaConfig({
        enabled: cpa.enabled,
        base_url: cpa.base_url,
        management_key: cpa.management_key || undefined,
        skip_ssl_verify: cpa.skip_ssl_verify,
        auto_import: cpa.auto_import,
        scheduled_enabled: cpa.scheduled_enabled,
        check_interval_minutes: Number(cpa.check_interval_minutes) || 60,
        concurrency: Number(cpa.concurrency) || 5,
        quota_threshold: Number(cpa.quota_threshold) || 0,
        disable_on_failure: cpa.disable_on_failure,
        delete_dead_accounts: cpa.delete_dead_accounts,
        delete_quota_exhausted: cpa.delete_quota_exhausted,
        notes: cpa.notes,
      });
      setCpa({ ...data.cpa_config, management_key: "" });
      setRegisterConfig(data.register);
      toast.success("CPA 仓库配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存 CPA 配置失败");
    } finally {
      setSaving(false);
    }
  };

  const saveGrok2Api = async () => {
    if (!grok2api) return;
    setSaving(true);
    try {
      const data = await updateGrok2ApiConfig({
        enabled: grok2api.enabled,
        base_url: grok2api.base_url,
        username: grok2api.username,
        password: grok2api.password || undefined,
        admin_token: grok2api.admin_token || undefined,
        skip_ssl_verify: grok2api.skip_ssl_verify,
        notes: grok2api.notes,
      });
      setGrok2Api({ ...data.grok2api_config, password: "", admin_token: "" });
      setRegisterConfig(data.register);
      toast.success("grok2api 仓库配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存 grok2api 配置失败");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    setTesting(true);
    try {
      let result: Record<string, unknown>;
      if (active === "sub2api") {
        result = (await testSub2ApiConnection()).sub2api_connection;
      } else if (active === "cpa") {
        result = (await testCpaConnection()).cpa_connection;
      } else {
        result = (await testGrok2ApiConnection()).grok2api_connection;
      }
      toast.success(`连接正常，读取到 ${Number(result.accounts || 0)} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "连接测试失败");
    } finally {
      setTesting(false);
    }
  };

  if (loading) return <div className="flex min-h-56 items-center justify-center border border-slate-200 bg-white"><LoaderCircle className="size-5 animate-spin text-slate-400" /></div>;
  if (!sub2api || !cpa || !grok2api) return null;
  const selectedGroupId = sub2api.group_ids_text.trim();
  const visibleGroupOptions = selectedGroupId && !groupOptions.some((group) => group.id === selectedGroupId)
    ? [{ id: selectedGroupId, name: `已保存分组 ${selectedGroupId}` }, ...groupOptions]
    : groupOptions;

  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b border-slate-200/80">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-xl text-slate-950"><Warehouse className="size-5 text-emerald-600" />中转仓库配置</CardTitle>
            <CardDescription className="mt-2 text-slate-600">sub2api、CPA 与 grok2api 分别配置和保存，互不覆盖地址、凭据与管理策略。</CardDescription>
          </div>
          <Badge variant="outline" className="rounded-md">3 个独立仓库</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-5 p-5">
        <div className="grid gap-3 md:grid-cols-3">
          <ProviderBubble active={active === "sub2api"} enabled={sub2api.enabled} title="sub2api 仓库" description="账号分组、原生测活、额度查询与 OAuth 账号调度。" url={sub2api.base_url} icon={Database} onClick={() => setActive("sub2api")} />
          <ProviderBubble active={active === "cpa"} enabled={cpa.enabled} title="CPA 仓库" description="对接 CLIProxyAPI，管理 Codex auth files、额度与启停状态。" url={cpa.base_url} icon={ServerCog} onClick={() => setActive("cpa")} />
          <ProviderBubble active={active === "grok2api"} enabled={grok2api.enabled} title="grok2api 仓库" description="直接接收 xAI SSO，管理 Grok Web 账号池和 Imagine 生图能力。" url={grok2api.base_url} icon={Images} onClick={() => setActive("grok2api")} />
        </div>

        {active === "sub2api" ? (
          <div className="space-y-5">
            <div className="grid gap-3 lg:grid-cols-3">
              <ToggleRow checked={sub2api.enabled} onChange={(value) => patchSub({ enabled: value })} title="启用 sub2api 仓库" detail="开放库存导入、管理页查询与仓库测活接口。" />
              <ToggleRow checked={sub2api.auto_import} onChange={(value) => patchSub({ auto_import: value })} title="注册成功后自动入库" detail="每个成功账号写入本地库存后，再独立推送到 sub2api。" />
              <ToggleRow checked={sub2api.scheduled_enabled} onChange={(value) => patchSub({ scheduled_enabled: value })} title="定期测活" detail={`当前间隔 ${sub2api.check_interval_minutes || 60} 分钟。`} />
              <ToggleRow checked={sub2api.skip_ssl_verify} onChange={(value) => patchSub({ skip_ssl_verify: value })} title="跳过 TLS 证书校验" detail="仅用于可信内网中的自签名证书；公网仓库应保持关闭。" danger />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <label className="space-y-1.5 lg:col-span-2"><span className="text-xs font-semibold text-slate-600">sub2api 地址</span><Input value={sub2api.base_url || ""} onChange={(event) => patchSub({ base_url: event.target.value })} placeholder="http://127.0.0.1:8080" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">管理员 Token</span><Input value={sub2api.admin_token || ""} onChange={(event) => patchSub({ admin_token: event.target.value })} placeholder={sub2api.has_admin_token ? `已保存：${sub2api.admin_token_preview}` : "x-api-key / Bearer token"} type="password" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">导入方式</span><select value={sub2api.import_method || "batch"} onChange={(event) => patchSub({ import_method: event.target.value as "batch" | "data" })} className="h-11 w-full border border-slate-200 bg-white px-3 text-sm"><option value="batch">批量创建</option><option value="data">数据导入</option></select></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">登录邮箱</span><Input value={sub2api.username || ""} onChange={(event) => patchSub({ username: event.target.value })} placeholder="无 Token 时使用" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">登录密码</span><Input value={sub2api.password || ""} onChange={(event) => patchSub({ password: event.target.value })} placeholder={sub2api.has_password ? "已保存，留空不修改" : "password"} type="password" /></label>
            </div>

            <div className="grid gap-3 border-y border-slate-200 py-4 md:grid-cols-4">
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">账号并发</span><Input type="number" value={String(sub2api.concurrency || 10)} onChange={(event) => patchSub({ concurrency: Number(event.target.value) || 10 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">优先级</span><Input type="number" value={String(sub2api.priority || 1)} onChange={(event) => patchSub({ priority: Number(event.target.value) || 1 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">倍率</span><Input type="number" value={String(sub2api.rate_multiplier || 1)} onChange={(event) => patchSub({ rate_multiplier: Number(event.target.value) || 1 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">负载因子</span><Input type="number" value={String(sub2api.load_factor || 10)} onChange={(event) => patchSub({ load_factor: Number(event.target.value) || 10 })} /></label>
              <label className="space-y-1.5 md:col-span-2">
                <span className="flex items-center justify-between gap-2 text-xs font-semibold text-slate-600">
                  <span>默认导入分组</span>
                  <button type="button" onClick={() => void loadSub2ApiGroups(true)} disabled={groupsLoading || !sub2api.enabled} className="inline-flex items-center gap-1 text-emerald-700 disabled:text-slate-400" title="从 sub2api 获取分组">
                    <RefreshCw className={`size-3.5 ${groupsLoading ? "animate-spin" : ""}`} />刷新分组
                  </button>
                </span>
                <select value={selectedGroupId} onChange={(event) => patchSub({ group_ids_text: event.target.value })} className="h-11 w-full border border-slate-200 bg-white px-3 text-sm">
                  <option value="">不指定（由 sub2api 决定）</option>
                  {visibleGroupOptions.map((group) => <option key={group.id} value={group.id}>{group.name}（ID: {group.id}）</option>)}
                </select>
                <span className="block text-[11px] font-normal leading-5 text-slate-500">保存后，库存手动导入和注册成功自动入库都会默认写入此分组。</span>
              </label>
              <label className="space-y-1.5 md:col-span-2"><span className="text-xs font-semibold text-slate-600">备注</span><Input value={sub2api.notes || ""} onChange={(event) => patchSub({ notes: event.target.value })} /></label>
            </div>

            <div className="grid gap-3 md:grid-cols-4">
              <label className="space-y-1.5"><span className="flex items-center gap-1 text-xs font-semibold text-slate-600"><TimerReset className="size-3.5" />巡检间隔（分）</span><Input type="number" value={String(sub2api.check_interval_minutes || 60)} onChange={(event) => patchSub({ check_interval_minutes: Number(event.target.value) || 60 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">测活并发</span><Input type="number" value={String(sub2api.test_concurrency || 5)} onChange={(event) => patchSub({ test_concurrency: Number(event.target.value) || 5 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">测试模型</span><Input value={sub2api.test_model_id || ""} onChange={(event) => patchSub({ test_model_id: event.target.value })} placeholder="留空自动选择" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">测试提示词</span><Input value={sub2api.test_prompt || "hi"} onChange={(event) => patchSub({ test_prompt: event.target.value })} /></label>
              <ToggleRow checked={sub2api.disable_on_failure} onChange={(value) => patchSub({ disable_on_failure: value })} title="巡检失败自动停用" detail="仅定时巡检应用此策略。" />
              <ToggleRow checked={sub2api.delete_on_failure} onChange={(value) => patchSub({ delete_on_failure: value })} title="巡检失败物理删除" detail="优先级高于自动停用。" danger />
              <ToggleRow checked={sub2api.auto_pause_on_expired} onChange={(value) => patchSub({ auto_pause_on_expired: value })} title="到期自动暂停" detail="导入账号时写入 sub2api 调度配置。" />
            </div>

          </div>
        ) : active === "cpa" ? (
          <div className="space-y-5">
            <div className="grid gap-3 lg:grid-cols-3">
              <ToggleRow checked={cpa.enabled} onChange={(value) => patchCpa({ enabled: value })} title="启用 CPA 仓库" detail="通过 CLIProxyAPI Management API 管理 Codex 凭据。" />
              <ToggleRow checked={cpa.auto_import} onChange={(value) => patchCpa({ auto_import: value })} title="注册成功后自动入库" detail="成功账号独立上传为 CLIProxyAPI auth file。" />
              <ToggleRow checked={cpa.scheduled_enabled} onChange={(value) => patchCpa({ scheduled_enabled: value })} title="定期测活" detail={`当前间隔 ${cpa.check_interval_minutes || 60} 分钟。`} />
              <ToggleRow checked={cpa.skip_ssl_verify} onChange={(value) => patchCpa({ skip_ssl_verify: value })} title="跳过 TLS 证书校验" detail="仅用于可信内网中的自签名证书；公网仓库应保持关闭。" danger />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <label className="space-y-1.5 lg:col-span-2"><span className="text-xs font-semibold text-slate-600">CPA / CLIProxyAPI 地址</span><Input value={cpa.base_url || ""} onChange={(event) => patchCpa({ base_url: event.target.value })} placeholder="http://127.0.0.1:8317" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">Management Key</span><Input value={cpa.management_key || ""} onChange={(event) => patchCpa({ management_key: event.target.value })} placeholder={cpa.has_management_key ? `已保存：${cpa.management_key_preview}` : "remote-management.secret-key"} type="password" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">备注</span><Input value={cpa.notes || ""} onChange={(event) => patchCpa({ notes: event.target.value })} /></label>
            </div>

            <div className="grid gap-4 border-y border-slate-200 py-4 md:grid-cols-3">
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">巡检间隔（分）</span><Input type="number" value={String(cpa.check_interval_minutes || 60)} onChange={(event) => patchCpa({ check_interval_minutes: Number(event.target.value) || 60 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">测活并发</span><Input type="number" value={String(cpa.concurrency || 5)} onChange={(event) => patchCpa({ concurrency: Number(event.target.value) || 5 })} /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">周限额阈值（%）</span><Input type="number" value={String(cpa.quota_threshold || 0)} onChange={(event) => patchCpa({ quota_threshold: Number(event.target.value) || 0 })} /></label>
            </div>

            <div className="grid gap-3 lg:grid-cols-3">
              <ToggleRow checked={cpa.disable_on_failure} onChange={(value) => patchCpa({ disable_on_failure: value })} title="巡检失败自动停用" detail="凭据失效或额度不足时先从调度中停用。" />
              <ToggleRow checked={cpa.delete_dead_accounts} onChange={(value) => patchCpa({ delete_dead_accounts: value })} title="死号物理删除" detail="Token 无效、账号失效时直接删除 auth file。" danger />
              <ToggleRow checked={cpa.delete_quota_exhausted} onChange={(value) => patchCpa({ delete_quota_exhausted: value })} title="额度耗尽物理删除" detail="低于阈值或耗尽时直接删除 auth file。" danger />
            </div>

            <div className="border border-emerald-200 bg-emerald-50/70 px-4 py-3 text-xs leading-5 text-emerald-800">
              <div className="flex items-center gap-2 font-semibold text-emerald-950"><CheckCircle2 className="size-4" />CPA 账号管理使用 CLIProxyAPI 原生 auth-files</div>
              <p className="mt-1">额度通过每个凭据的 auth_index 调用 ChatGPT usage 接口，账号不会与 sub2api 列表混合。</p>
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="grid gap-3 lg:grid-cols-2">
              <ToggleRow checked={grok2api.enabled} onChange={(value) => patchGrok2Api({ enabled: value })} title="启用 grok2api 仓库" detail="开放 Grok Web SSO 导入、账号池查询和额度刷新。" />
              <ToggleRow checked={grok2api.skip_ssl_verify} onChange={(value) => patchGrok2Api({ skip_ssl_verify: value })} title="跳过 TLS 证书校验" detail="仅用于可信内网中的自签名证书；公网仓库应保持关闭。" danger />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <label className="space-y-1.5 lg:col-span-2"><span className="text-xs font-semibold text-slate-600">grok2api 地址</span><Input value={grok2api.base_url || ""} onChange={(event) => patchGrok2Api({ base_url: event.target.value })} placeholder="http://127.0.0.1:8080" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">管理员用户名</span><Input value={grok2api.username || ""} onChange={(event) => patchGrok2Api({ username: event.target.value })} placeholder="admin" autoComplete="off" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">管理员密码</span><Input value={grok2api.password || ""} onChange={(event) => patchGrok2Api({ password: event.target.value })} placeholder={grok2api.has_password ? "已保存，留空不修改" : "password"} type="password" autoComplete="new-password" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">管理员 Access Token（可选）</span><Input value={grok2api.admin_token || ""} onChange={(event) => patchGrok2Api({ admin_token: event.target.value })} placeholder={grok2api.has_admin_token ? `已保存：${grok2api.admin_token_preview}` : "用户名/密码为空时使用"} type="password" autoComplete="off" /></label>
              <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">备注</span><Input value={grok2api.notes || ""} onChange={(event) => patchGrok2Api({ notes: event.target.value })} /></label>
            </div>

            <div className="border border-emerald-200 bg-emerald-50/70 px-4 py-3 text-xs leading-5 text-emerald-800">
              <div className="flex items-center gap-2 font-semibold text-emerald-950"><CheckCircle2 className="size-4" />SSO 使用 grok2api 原生 Grok Web 导入协议</div>
              <p className="mt-1">注册器上传 provider=grok_web 的 JSON 文件。Free/Basic 账号可用于 grok-imagine-image；是否换取 Grok OAuth 不影响此导入。</p>
            </div>
          </div>
        )}

        <div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 pt-4">
          <Button variant="outline" className="h-10 bg-white" onClick={() => void testConnection()} disabled={testing || saving || !(active === "sub2api" ? sub2api.enabled : active === "cpa" ? cpa.enabled : grok2api.enabled)}>
            {testing ? <LoaderCircle className="size-4 animate-spin" /> : <TestTube2 className="size-4" />}测试连接
          </Button>
          <Button className="h-10 bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void (active === "sub2api" ? saveSub2Api() : active === "cpa" ? saveCpa() : saveGrok2Api())} disabled={saving || testing}>
            {saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}保存当前仓库
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

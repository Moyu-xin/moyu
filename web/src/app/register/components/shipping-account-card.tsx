"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Download, ExternalLink, HeartPulse, LoaderCircle, PackageCheck, RefreshCw, Save, Send, Server, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  checkShippingAccountHealth,
  deleteShippingAccounts,
  fetchShippingAccountPool,
  fetchShippingStoreConfig,
  importShippingAccounts,
  outboundShippingAccounts,
  publishShippingAccounts,
  testShippingStoreConnection,
  updateShippingStoreConfig,
  type ShippingAccountItem,
  type ShippingAccountPool,
  type ShippingStoreConfig,
} from "@/lib/api";

const aliveStatuses = new Set(["ok", "alive", "healthy"]);

function downloadJson(filename: string, content: Record<string, unknown>) {
  const blob = new Blob([JSON.stringify(content, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function healthBadge(status: unknown) {
  const value = String(status || "unknown").toLowerCase();
  if (aliveStatuses.has(value)) return <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">可用</Badge>;
  if (["banned", "dead", "invalid", "disabled", "token_invalid"].includes(value)) return <Badge className="border-red-200 bg-red-50 text-red-700">失效</Badge>;
  return <Badge variant="outline" className="text-slate-500">待测活</Badge>;
}

export function ShippingAccountCard({ active = true }: { active?: boolean }) {
  const [pool, setPool] = useState<ShippingAccountPool | null>(null);
  const [config, setConfig] = useState<ShippingStoreConfig | null>(null);
  const [draft, setDraft] = useState({ enabled: false, base_url: "", api_key: "", card_prefix: "GROK-", request_timeout: 60 });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [batch, setBatch] = useState("");
  const [status, setStatus] = useState("available");
  const [quantity, setQuantity] = useState(1);
  const [format, setFormat] = useState<"sub2api" | "grok2api">("sub2api");
  const [includeOutbound, setIncludeOutbound] = useState(false);
  const [busy, setBusy] = useState("");

  const applyConfig = useCallback((next: ShippingStoreConfig) => {
    setConfig(next);
    setDraft({
      enabled: Boolean(next.enabled),
      base_url: next.base_url || "",
      api_key: "",
      card_prefix: next.card_prefix || "GROK-",
      request_timeout: Number(next.request_timeout || 60),
    });
  }, []);

  const load = useCallback(async (force = false) => {
    setBusy((current) => current || "load");
    try {
      const [poolResult, configResult] = await Promise.all([
        fetchShippingAccountPool({ force }),
        fetchShippingStoreConfig({ force }),
      ]);
      setPool(poolResult.shipping_pool);
      applyConfig(configResult.shipping_config);
      setSelected((current) => new Set([...current].filter((id) => poolResult.shipping_pool.accounts.some((item) => item.shipping_id === id))));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载出货账号失败");
    } finally {
      setBusy("");
    }
  }, [applyConfig]);

  useEffect(() => {
    if (active && !pool) void load();
  }, [active, load, pool]);

  const visible = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return (pool?.accounts || []).filter((item) => {
      if (keyword && !`${item.email} ${item.shipping_id} ${item.registration_batch_label || ""}`.toLowerCase().includes(keyword)) return false;
      if (batch && item.registration_batch_id !== batch) return false;
      const health = String(item.health_status || "unknown").toLowerCase();
      if (status === "alive" && !aliveStatuses.has(health)) return false;
      if (status === "available" && (!aliveStatuses.has(health) || Boolean(item.outbound_at))) return false;
      if (status === "outbound" && !item.outbound_at) return false;
      if (status === "unknown" && (aliveStatuses.has(health) || ["banned", "dead", "invalid", "disabled", "token_invalid"].includes(health))) return false;
      return true;
    });
  }, [batch, pool, search, status]);

  const selectedIds = useMemo(() => [...selected], [selected]);
  const allVisibleSelected = visible.length > 0 && visible.every((item) => selected.has(item.shipping_id));

  const runHealth = async () => {
    setBusy("health");
    try {
      const result = await checkShippingAccountHealth(selectedIds, 8);
      setPool(result.shipping_health.shipping_pool);
      toast.success(`测活完成：可用 ${result.shipping_health.stats.alive}，失败 ${result.shipping_health.stats.failed}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "出货账号测活失败");
    } finally {
      setBusy("");
    }
  };

  const runImport = async (target: "sub2api" | "grok2api") => {
    setBusy(`import-${target}`);
    try {
      const result = await importShippingAccounts(target, selectedIds);
      setPool(result.shipping_import.shipping_pool);
      toast.success(`已向 ${target} 提交 ${result.shipping_import.count} 个测活成功账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : `导入 ${target} 失败`);
    } finally {
      setBusy("");
    }
  };

  const outboundPayload = () => ({
    selectors: selectedIds.length ? selectedIds : undefined,
    quantity: selectedIds.length ? 0 : Math.max(1, quantity),
    registration_batch_id: selectedIds.length ? undefined : batch || undefined,
    include_outbound: includeOutbound,
  });

  const runOutbound = async () => {
    setBusy("outbound");
    try {
      const result = await outboundShippingAccounts({ ...outboundPayload(), format });
      setPool(result.shipping_outbound.shipping_pool);
      downloadJson(result.shipping_outbound.filename, result.shipping_outbound.content);
      toast.success(`已出库 ${result.shipping_outbound.count} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "出库失败");
    } finally {
      setBusy("");
    }
  };

  const runPublish = async () => {
    if (!window.confirm("确认把当前条件下测活成功的账号同步到商店库存？")) return;
    setBusy("publish");
    try {
      const result = await publishShippingAccounts(outboundPayload());
      setPool(result.store_publish.shipping_pool);
      toast.success(`商店库存同步完成：${result.store_publish.published}/${result.store_publish.requested}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "商店库存同步失败");
    } finally {
      setBusy("");
    }
  };

  const saveConfig = async () => {
    setBusy("save-config");
    try {
      const result = await updateShippingStoreConfig(draft);
      applyConfig(result.shipping_config);
      toast.success("商店连接配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存商店配置失败");
    } finally {
      setBusy("");
    }
  };

  const testStore = async () => {
    setBusy("test-store");
    try {
      await testShippingStoreConnection();
      toast.success("商店连接正常");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "商店连接失败");
    } finally {
      setBusy("");
    }
  };

  const removeSelected = async () => {
    if (!selectedIds.length || !window.confirm(`确认删除 ${selectedIds.length} 个出货账号？`)) return;
    setBusy("delete");
    try {
      const result = await deleteShippingAccounts(selectedIds);
      setPool(result.shipping_pool);
      setSelected(new Set());
      toast.success(`已删除 ${result.shipping_pool.removed || 0} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusy("");
    }
  };

  const stats = pool?.stats || {};
  const loading = Boolean(busy);

  return (
    <div className="space-y-4">
      <section className="border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-slate-950">出货库存</h3>
            <p className="mt-1 text-xs text-slate-500">独立保存 Grok 账号；注册成功后可自动同步商店，兑换时由商店实时测活并出库。</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void load(true)} disabled={loading} title="刷新出货库存"><RefreshCw className={`size-4 ${busy === "load" ? "animate-spin" : ""}`} />刷新</Button>
        </div>

        <div className="grid grid-cols-2 border-b border-slate-200 md:grid-cols-5">
          {[
            ["总库存", stats.total || 0],
            ["测活成功", stats.alive || 0],
            ["可出库", stats.available || 0],
            ["已出库", stats.outbound || 0],
            ["已同步商店", stats.store_listed || 0],
          ].map(([label, value]) => <div key={String(label)} className="border-r border-slate-200 px-4 py-3 last:border-r-0"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 text-xl font-semibold text-slate-950">{value}</div></div>)}
        </div>

        <div className="grid gap-3 border-b border-slate-200 p-4 lg:grid-cols-[minmax(180px,1fr)_180px_150px_110px_auto]">
          <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索邮箱、ID 或批次" className="h-10 rounded-none" />
          <select value={batch} onChange={(event) => setBatch(event.target.value)} className="h-10 border border-input bg-background px-3 text-sm">
            <option value="">全部批次</option>
            {(pool?.batches || []).map((item) => <option key={item.id} value={item.id}>{item.label} ({item.available}/{item.total})</option>)}
          </select>
          <select value={status} onChange={(event) => setStatus(event.target.value)} className="h-10 border border-input bg-background px-3 text-sm">
            <option value="available">仅可出库</option><option value="alive">全部可用</option><option value="outbound">已出库</option><option value="unknown">待测活</option><option value="all">全部状态</option>
          </select>
          <Input type="number" min={1} value={quantity} onChange={(event) => setQuantity(Math.max(1, Number(event.target.value) || 1))} className="h-10 rounded-none" title="未勾选账号时按此数量出库" />
          <div className="flex items-center gap-2 text-xs text-slate-600"><Checkbox checked={includeOutbound} onCheckedChange={(value) => setIncludeOutbound(Boolean(value))} />允许重复出库</div>
        </div>

        <div className="flex flex-wrap gap-2 border-b border-slate-200 p-4">
          <Button size="sm" onClick={runHealth} disabled={loading}><HeartPulse className="size-4" />{selectedIds.length ? `测活所选 ${selectedIds.length}` : "全部测活"}</Button>
          <Button variant="outline" size="sm" onClick={() => void runImport("sub2api")} disabled={loading}><Upload className="size-4" />导入 sub2api</Button>
          <Button variant="outline" size="sm" onClick={() => void runImport("grok2api")} disabled={loading}><Upload className="size-4" />导入 grok2api</Button>
          <div className="inline-grid grid-cols-2 border border-slate-300 bg-slate-50 p-0.5">
            {(["sub2api", "grok2api"] as const).map((value) => <button key={value} type="button" onClick={() => setFormat(value)} className={`px-3 py-1.5 text-xs font-semibold ${format === value ? "bg-slate-950 text-white" : "text-slate-600"}`}>{value}</button>)}
          </div>
          <Button size="sm" onClick={runOutbound} disabled={loading}><Download className="size-4" />下载出库</Button>
          <Button size="sm" className="bg-emerald-600 hover:bg-emerald-700" onClick={runPublish} disabled={loading || !config?.enabled}><Send className="size-4" />同步商店库存</Button>
          <Button variant="outline" size="icon" onClick={removeSelected} disabled={loading || !selectedIds.length} title="删除所选"><Trash2 className="size-4 text-red-600" /></Button>
          {busy ? <span className="flex items-center gap-1 text-xs text-slate-500"><LoaderCircle className="size-4 animate-spin" />处理中</span> : null}
        </div>

        <div className="max-h-[560px] overflow-auto">
          <table className="w-full min-w-[1050px] text-left text-xs">
            <thead className="sticky top-0 z-10 bg-slate-50 text-slate-500"><tr className="border-b border-slate-200"><th className="w-10 p-3"><Checkbox checked={allVisibleSelected} onCheckedChange={(value) => setSelected((current) => { const next = new Set(current); visible.forEach((item) => value ? next.add(item.shipping_id) : next.delete(item.shipping_id)); return next; })} /></th><th className="p-3">账号</th><th className="p-3">注册批次</th><th className="p-3">测活</th><th className="p-3">凭据</th><th className="p-3">仓库导入</th><th className="p-3">出库状态</th></tr></thead>
            <tbody>
              {visible.map((item) => (
                <tr key={item.shipping_id} className="border-b border-slate-100 hover:bg-slate-50/70">
                  <td className="p-3"><Checkbox checked={selected.has(item.shipping_id)} onCheckedChange={(value) => setSelected((current) => { const next = new Set(current); value ? next.add(item.shipping_id) : next.delete(item.shipping_id); return next; })} /></td>
                  <td className="p-3"><div className="font-semibold text-slate-900">{item.email}</div><div className="mt-1 font-mono text-[10px] text-slate-400">{item.shipping_id}</div></td>
                  <td className="p-3"><div>{item.registration_batch_label || item.registration_batch_id || "-"}</div><div className="mt-1 text-slate-400">{item.registered_at ? new Date(item.registered_at).toLocaleString("zh-CN") : "-"}</div></td>
                  <td className="p-3">{healthBadge(item.health_status)}<div className="mt-1 text-slate-400">{item.health_code || "-"} · {item.health_method || "-"}</div></td>
                  <td className="p-3"><div className="flex gap-1">{item.has_sso_token ? <Badge variant="outline">SSO</Badge> : null}{item.has_refresh_token ? <Badge variant="outline">RT</Badge> : null}{item.has_access_token ? <Badge variant="outline">AT</Badge> : null}</div></td>
                  <td className="p-3"><div>sub2api: {item.sub2api_import_status === "ok" ? "已导入" : "-"}</div><div className="mt-1">grok2api: {item.grok2api_import_status === "ok" ? "已导入" : "-"}</div></td>
                  <td className="p-3">{item.store_listing_id ? <div><Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">已同步商店</Badge><div className="mt-1 font-mono text-[10px] text-slate-500">{item.store_listing_id}</div></div> : item.outbound_at ? <div><Badge variant="outline">已出库</Badge><div className="mt-1 text-slate-400">{item.outbound_format}</div></div> : <Badge variant="outline" className="text-slate-500">库存中</Badge>}</td>
                </tr>
              ))}
              {!visible.length ? <tr><td colSpan={7} className="p-12 text-center text-sm text-slate-400">当前筛选条件下没有出货账号</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="border border-slate-200 bg-white shadow-sm">
        <div className="flex items-center gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3"><Server className="size-4 text-emerald-600" /><div><h3 className="text-sm font-semibold text-slate-950">商店连接</h3><p className="mt-1 text-xs text-slate-500">账号与 CDK 独立维护；这里仅同步账号库存，通用 Grok CDK 在商店后台生成。</p></div></div>
        <div className="grid gap-3 p-4 lg:grid-cols-[120px_minmax(220px,1fr)_minmax(180px,1fr)_100px_auto]">
          <label className="flex h-10 items-center gap-2 border border-slate-200 px-3 text-xs"><Checkbox checked={draft.enabled} onCheckedChange={(value) => setDraft((current) => ({ ...current, enabled: Boolean(value) }))} />启用</label>
          <Input value={draft.base_url} onChange={(event) => setDraft((current) => ({ ...current, base_url: event.target.value }))} placeholder="商店地址，例如 https://shop.example.com" className="h-10 rounded-none" />
          <Input type="password" value={draft.api_key} onChange={(event) => setDraft((current) => ({ ...current, api_key: event.target.value }))} placeholder={config?.has_api_key ? `已配置 ${config.api_key_preview || ""}` : "商店接入密钥"} className="h-10 rounded-none" />
          <Input type="number" min={10} max={300} value={draft.request_timeout} onChange={(event) => setDraft((current) => ({ ...current, request_timeout: Number(event.target.value) || 60 }))} className="h-10 rounded-none" title="请求超时秒数" />
          <div className="flex gap-2"><Button size="icon" onClick={saveConfig} disabled={loading} title="保存商店配置"><Save className="size-4" /></Button><Button variant="outline" size="icon" onClick={testStore} disabled={loading || !config?.has_api_key} title="测试商店连接"><ExternalLink className="size-4" /></Button></div>
        </div>
        {config?.enabled && config.has_api_key ? <div className="flex items-center gap-2 border-t border-slate-200 bg-emerald-50 px-4 py-2 text-xs text-emerald-700"><CheckCircle2 className="size-4" />注册成功后自动同步商店库存</div> : null}
      </section>
    </div>
  );
}

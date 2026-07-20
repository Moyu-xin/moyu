"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Copy, CreditCard, LoaderCircle, Play, RefreshCw, RotateCcw, Save, Smartphone, Square, Trash2, Upload, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  cancelPaypalPayment,
  clearFinishedPaypalPayments,
  deletePaypalPayment,
  deletePaypalPhonePool,
  fetchPaypalPaymentState,
  importPaypalPhonePool,
  resetPaypalPhonePool,
  retryPaypalPayment,
  startPaypalPayments,
  stopPaypalPayments,
  updatePaypalPaymentConfig,
  type PaypalPaymentConfig,
  type PaypalPaymentResult,
  type PaypalPaymentState,
} from "@/lib/api";

const countries = [
  { value: "AUTO", label: "自动 · 跟随 BA 国家" },
  { value: "BR", label: "BR · 巴西" },
  { value: "GB", label: "GB · 英国" },
  { value: "US", label: "US · 美国" },
  { value: "TH", label: "TH · 泰国" },
  { value: "ID", label: "ID · 印度尼西亚" },
];

const defaultConfig: PaypalPaymentConfig = {
  auto_pay_new_links: false,
  protocol_country: "AUTO",
  sms_provider: "smsbower",
  proxy_override: "",
  smsbower_api_key: "",
  smsbower_api_key_configured: false,
  smsbower_max_price: 0.014,
  max_card_attempts: 5,
  sms_max_attempts: 5,
  flow_attempts: 2,
  concurrency: 1,
};

function mergeConfig(value?: Partial<PaypalPaymentConfig>): PaypalPaymentConfig {
  return { ...defaultConfig, ...(value || {}) };
}

function statusVariant(status: string) {
  if (status === "success") return "success" as const;
  if (status === "failed" || status === "cancelled") return "danger" as const;
  if (status === "running" || status === "cancelling") return "warning" as const;
  return "secondary" as const;
}

function statusText(status: string) {
  return status === "success" ? "成功" : status === "failed" ? "失败" : status === "cancelled" ? "已取消" : status === "cancelling" ? "停止中" : status === "running" ? "运行中" : "排队中";
}

function shortBa(value: string) {
  const match = value.match(/ba_token=([^&]+)/i);
  const token = match?.[1] || value;
  return token.length > 18 ? `${token.slice(0, 9)}…${token.slice(-6)}` : token;
}

async function copyText(value: string, label: string) {
  await navigator.clipboard.writeText(value);
  toast.success(label);
}

function resultSummary(result: PaypalPaymentResult) {
  const payment = result.payment_result || {};
  const settlement = String(payment.settlement_status || "");
  const buyer = String(payment.user_id || "");
  if (result.status === "success") return [settlement, buyer].filter(Boolean).join(" · ") || "PayPal BA 授权成功";
  return result.error || result.last_log || result.last_message || "等待执行";
}

export function PaypalPaymentCard({ active = true, activityRef }: { active?: boolean; activityRef?: { current: boolean } }) {
  const [state, setState] = useState<PaypalPaymentState | null>(null);
  const [draft, setDraft] = useState<PaypalPaymentConfig>(() => mergeConfig());
  const [selected, setSelected] = useState<string[]>([]);
  const [manualBaText, setManualBaText] = useState("");
  const [manualCountry, setManualCountry] = useState("TH");
  const [phonePoolImport, setPhonePoolImport] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const dirtyRef = useRef(false);

  const load = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const payload = await fetchPaypalPaymentState({ force: silent });
      setState(payload.paypal_payments);
      if (!dirtyRef.current) setDraft(mergeConfig(payload.paypal_payments.config));
      const valid = new Set(payload.paypal_payments.candidates.map((item) => item.source_task_id));
      setSelected((current) => current.filter((id) => valid.has(id)));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "协议支付状态刷新失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    const intervalMs = state?.runner_alive ? 2500 : 15000;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      const isActive = activityRef ? activityRef.current : active;
      if (isActive) await load(true);
      if (!cancelled) timer = window.setTimeout(() => void poll(), isActive ? intervalMs : 250);
    };
    timer = window.setTimeout(() => void poll(), (activityRef ? activityRef.current : active) ? intervalMs : 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [active, activityRef, state?.runner_alive]);

  const patchDraft = (updates: Partial<PaypalPaymentConfig>) => {
    dirtyRef.current = true;
    setDraft((current) => mergeConfig({ ...current, ...updates }));
  };
  const candidates = state?.candidates || [];
  const selectable = useMemo(() => candidates.filter((item) => (item.supported || draft.protocol_country !== "AUTO") && !item.paid), [candidates, draft.protocol_country]);
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const results = useMemo(() => [...(state?.results || [])].reverse(), [state?.results]);
  const manualBaLines = useMemo(() => manualBaText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean), [manualBaText]);
  const paymentCount = selected.length + manualBaLines.length;
  const stats = state?.stats || { success: 0, fail: 0, cancelled: 0, done: 0, running: 0, total: 0 };

  const save = async () => {
    setBusy("save");
    try {
      const payload = await updatePaypalPaymentConfig(draft);
      setState(payload.paypal_payments);
      setDraft(mergeConfig(payload.paypal_payments.config));
      dirtyRef.current = false;
      toast.success("协议支付配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "协议支付配置保存失败");
    } finally {
      setBusy("");
    }
  };

  const start = async () => {
    if (!paymentCount) { toast.warning("请选择 Plus BA 或添加手动 BA 链"); return; }
    setBusy("start");
    try {
      const payload = await startPaypalPayments({
        source_task_ids: selected,
        manual_items: manualBaLines.map((ba_url) => ({ ba_url, country: manualCountry })),
        config: draft,
        persist_config: true,
      });
      setState(payload.paypal_payments);
      setDraft(mergeConfig(payload.paypal_payments.config));
      setSelected([]);
      setManualBaText("");
      dirtyRef.current = false;
      toast.success(`已加入 ${paymentCount} 个协议支付任务`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "协议支付启动失败");
    } finally {
      setBusy("");
    }
  };

  const importPhonePool = async () => {
    if (!phonePoolImport.trim()) { toast.warning("请输入手机号和验证码 URL"); return; }
    setBusy("phone-import");
    try {
      const payload = await importPaypalPhonePool(phonePoolImport);
      setState(payload.paypal_payments);
      setPhonePoolImport("");
      const invalid = payload.import_result.invalid.length;
      toast.success(`导入 ${payload.import_result.imported}，更新 ${payload.import_result.updated}${invalid ? `，无效 ${invalid}` : ""}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "手机号池导入失败");
    } finally {
      setBusy("");
    }
  };

  const phonePoolAction = async (entryId: string, action: "reset" | "delete") => {
    setBusy(`phone-${action}:${entryId}`);
    try {
      const payload = action === "reset" ? await resetPaypalPhonePool(entryId) : await deletePaypalPhonePool(entryId);
      setState(payload.paypal_payments);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "手机号池操作失败");
    } finally {
      setBusy("");
    }
  };

  const stop = async () => {
    setBusy("stop");
    try {
      const payload = await stopPaypalPayments();
      setState(payload.paypal_payments);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "协议支付停止失败");
    } finally {
      setBusy("");
    }
  };

  const taskAction = async (result: PaypalPaymentResult, action: "cancel" | "retry" | "delete") => {
    setBusy(`${action}:${result.task_id}`);
    try {
      const payload = action === "cancel" ? await cancelPaypalPayment(result.task_id) : action === "retry" ? await retryPaypalPayment(result.task_id) : await deletePaypalPayment(result.task_id);
      setState(payload.paypal_payments);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "任务操作失败");
    } finally {
      setBusy("");
    }
  };

  if (loading && !state) {
    return <Card><CardContent className="flex min-h-48 items-center justify-center text-sm text-slate-500"><LoaderCircle className="mr-2 size-5 animate-spin" />正在加载协议支付</CardContent></Card>;
  }

  return <div className="space-y-4">
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      {[["BA 候选", candidates.length], ["运行中", stats.running], ["成功", stats.success], ["失败", stats.fail], ["已完成", stats.done]].map(([label, value]) => <div key={String(label)} className="border border-slate-200 bg-white px-4 py-3 shadow-sm"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 text-xl font-semibold tabular-nums text-slate-950">{value}</div></div>)}
    </div>

    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div><CardTitle className="flex items-center gap-2 text-lg"><CreditCard className="size-5 text-emerald-600" />协议配置</CardTitle><CardDescription>BR / GB / US / TH / ID New</CardDescription></div>
        <div className="flex gap-2"><Button variant="outline" onClick={() => void load()} disabled={Boolean(busy)}><RefreshCw />刷新</Button><Button onClick={() => void save()} disabled={Boolean(busy)}>{busy === "save" ? <LoaderCircle className="animate-spin" /> : <Save />}保存</Button></div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 border border-slate-200 bg-slate-50 p-1" role="tablist" aria-label="验证码来源">
          <button type="button" role="tab" aria-selected={draft.sms_provider === "smsbower"} onClick={() => patchDraft({ sms_provider: "smsbower" })} className={`flex min-h-10 items-center justify-center gap-2 px-3 text-sm font-semibold transition ${draft.sms_provider === "smsbower" ? "bg-slate-950 text-white shadow-sm" : "text-slate-600 hover:bg-white"}`}><CreditCard className="size-4" />SMSBower</button>
          <button type="button" role="tab" aria-selected={draft.sms_provider === "phone_pool"} onClick={() => patchDraft({ sms_provider: "phone_pool" })} className={`flex min-h-10 items-center justify-center gap-2 px-3 text-sm font-semibold transition ${draft.sms_provider === "phone_pool" ? "bg-emerald-600 text-white shadow-sm" : "text-slate-600 hover:bg-white"}`}><Smartphone className="size-4" />手机号池</button>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>协议国家</span><Select value={draft.protocol_country} onValueChange={(value) => patchDraft({ protocol_country: value })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{countries.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectContent></Select></label>
          <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>并发</span><Input type="number" min={1} max={5} value={draft.concurrency} onChange={(event) => patchDraft({ concurrency: Number(event.target.value || 1) })} /></label>
          {draft.sms_provider === "smsbower" ? <><label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>SMSBower API Key</span><Input type="password" name="paypal-smsbower-api-key" autoComplete="new-password" aria-autocomplete="none" data-1p-ignore data-lpignore="true" value={draft.smsbower_api_key} onChange={(event) => patchDraft({ smsbower_api_key: event.target.value })} placeholder={draft.smsbower_api_key_configured ? "已配置，留空不修改" : "API Key"} /></label><label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>号码最高价格 USD</span><Input type="number" min={0.001} max={10} step={0.001} value={draft.smsbower_max_price} onChange={(event) => patchDraft({ smsbower_max_price: Number(event.target.value || 0.014) })} /></label></> : <><div className="border border-emerald-200 bg-emerald-50 px-3 py-2"><div className="text-xs text-emerald-700">号池总数</div><div className="mt-1 text-lg font-semibold text-emerald-950">{state?.phone_pool?.total || 0}</div></div><div className="border border-sky-200 bg-sky-50 px-3 py-2"><div className="text-xs text-sky-700">当前国家可用</div><div className="mt-1 text-lg font-semibold text-sky-950">{draft.protocol_country === "AUTO" ? "自动匹配" : (state?.phone_pool?.numbers || []).filter((item) => item.country === draft.protocol_country && item.available).length}</div></div></>}
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>绑卡尝试</span><Input type="number" min={1} max={20} value={draft.max_card_attempts} onChange={(event) => patchDraft({ max_card_attempts: Number(event.target.value || 5) })} /></label>
          <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>换号次数</span><Input type="number" min={1} max={20} value={draft.sms_max_attempts} onChange={(event) => patchDraft({ sms_max_attempts: Number(event.target.value || 5) })} /></label>
          <label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>流程尝试</span><Input type="number" min={1} max={5} value={draft.flow_attempts} onChange={(event) => patchDraft({ flow_attempts: Number(event.target.value || 2) })} /></label>
        </div>
        <label className="block space-y-1.5 text-sm font-semibold text-slate-700"><span>协议代理覆盖</span><Textarea className="min-h-20 font-mono text-xs" value={draft.proxy_override} onChange={(event) => patchDraft({ proxy_override: event.target.value })} placeholder="留空时使用 Plus 对应国家代理池" /></label>
        <label className="flex items-start gap-3 border-t border-slate-200 pt-4 text-sm text-slate-700"><Checkbox checked={draft.auto_pay_new_links} onCheckedChange={(checked) => patchDraft({ auto_pay_new_links: checked === true })} /><span><span className="block font-semibold text-slate-950">新 BA 自动加入支付队列</span><span className="mt-1 block text-xs text-slate-500">提链任务保持独立成功状态</span></span></label>
      </CardContent>
    </Card>

    {draft.sms_provider === "phone_pool" ? <Card className="border-emerald-200 bg-white shadow-sm">
      <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between"><div><CardTitle className="flex items-center gap-2 text-lg"><Smartphone className="size-5 text-emerald-600" />手机号池</CardTitle><CardDescription>{state?.phone_pool?.total || 0} 个号码</CardDescription></div><Button variant="outline" onClick={() => void load()} disabled={Boolean(busy)}><RefreshCw />刷新</Button></CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end"><label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>批量导入</span><Textarea className="min-h-24 font-mono text-xs" value={phonePoolImport} onChange={(event) => setPhonePoolImport(event.target.value)} placeholder="+66912345678----https://sms.example.com/code" /></label><Button onClick={() => void importPhonePool()} disabled={!phonePoolImport.trim() || Boolean(busy)}>{busy === "phone-import" ? <LoaderCircle className="animate-spin" /> : <Upload />}导入号码</Button></div>
        {(state?.phone_pool?.numbers || []).length ? <div className="divide-y divide-slate-200 border-y border-slate-200">{(state?.phone_pool?.numbers || []).map((item) => <div key={item.id} className="grid gap-2 px-2 py-3 text-sm sm:grid-cols-[100px_minmax(0,1fr)_130px_auto] sm:items-center"><Badge variant={item.available ? "success" : item.in_use ? "warning" : "secondary"}>{item.country} · {item.status}</Badge><span className="min-w-0"><span className="block font-mono font-semibold text-slate-900">{item.phone}</span><span className="mt-0.5 block truncate text-xs text-slate-500">{item.url_hint || item.status_message}</span></span><span className="text-xs text-slate-500">失败 {item.fail_count} 次</span><span className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled={item.in_use || Boolean(busy)} onClick={() => void phonePoolAction(item.id, "reset")}><RotateCcw />恢复</Button><Button size="icon" variant="ghost" title="删除号码" disabled={item.in_use || Boolean(busy)} onClick={() => void phonePoolAction(item.id, "delete")}><Trash2 /></Button></span></div>)}</div> : <div className="border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">暂无手机号</div>}
      </CardContent>
    </Card> : null}

    <Card className="border-sky-200 bg-white shadow-sm">
      <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between"><div><CardTitle className="flex items-center gap-2 text-lg"><CreditCard className="size-5 text-sky-600" />手动 BA</CardTitle><CardDescription>{manualBaLines.length} 条待加入</CardDescription></div>{state?.runner_alive ? <Button variant="destructive" onClick={() => void stop()} disabled={Boolean(busy)}>{busy === "stop" ? <LoaderCircle className="animate-spin" /> : <Square />}停止队列</Button> : <Button onClick={() => void start()} disabled={!paymentCount || Boolean(busy)}>{busy === "start" ? <LoaderCircle className="animate-spin" /> : <Play />}开始支付 {paymentCount || ""}</Button>}</CardHeader>
      <CardContent className="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)]"><label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>手动链国家</span><Select value={manualCountry} onValueChange={setManualCountry}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{countries.filter((item) => item.value !== "AUTO").map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectContent></Select></label><label className="space-y-1.5 text-sm font-semibold text-slate-700"><span>BA 链</span><Textarea className="min-h-24 font-mono text-xs" value={manualBaText} onChange={(event) => setManualBaText(event.target.value)} placeholder="https://www.paypal.com/agreements/approve?ba_token=BA-..." /></label></CardContent>
    </Card>

    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div><CardTitle className="flex items-center gap-2 text-lg"><CheckCircle2 className="size-5 text-sky-600" />Plus BA 结果</CardTitle><CardDescription>可支付 {selectable.length} / 全部 {candidates.length}</CardDescription></div>
        <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => setSelected(selectable.map((item) => item.source_task_id))} disabled={!selectable.length}>全选可支付</Button><Button variant="outline" onClick={() => setSelected([])} disabled={!selected.length}>清除</Button></div>
      </CardHeader>
      <CardContent>
        {candidates.length ? <div className="divide-y divide-slate-200 border-y border-slate-200">
          {candidates.map((item) => {
            const available = (item.supported || draft.protocol_country !== "AUTO") && !item.paid;
            return <label key={item.source_task_id} className={`grid gap-3 px-2 py-3 text-sm sm:grid-cols-[28px_minmax(0,1fr)_100px_150px_auto] sm:items-center ${available ? "hover:bg-slate-50" : "opacity-60"}`}>
              <Checkbox checked={selectedSet.has(item.source_task_id)} disabled={!available} onCheckedChange={(checked) => setSelected((current) => checked === true ? Array.from(new Set([...current, item.source_task_id])) : current.filter((id) => id !== item.source_task_id))} />
              <span className="min-w-0"><span className="block truncate font-semibold text-slate-950">{item.email || item.source_task_id}</span><span className="mt-0.5 block truncate font-mono text-xs text-slate-500">{shortBa(item.ba_url)}</span></span>
              <Badge variant={item.supported ? "success" : "warning"}>{item.protocol_mode || `${item.country || "--"} 不支持`}</Badge>
              <span className="text-xs text-slate-500">{item.created_at ? new Date(item.created_at).toLocaleString() : "--"}</span>
              <span className="flex items-center justify-end gap-2">{item.paid ? <Badge variant="success">已支付</Badge> : null}<Button size="icon" variant="ghost" title="复制 BA 链" onClick={(event) => { event.preventDefault(); void copyText(item.ba_url, "BA 链已复制"); }}><Copy className="size-4" /></Button></span>
            </label>;
          })}
        </div> : <div className="border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">暂无 Plus BA 结果</div>}
      </CardContent>
    </Card>

    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="gap-3 lg:flex-row lg:items-start lg:justify-between"><div><CardTitle className="text-lg">支付任务</CardTitle><CardDescription>{results.length} 条记录</CardDescription></div><Button variant="outline" disabled={!results.some((item) => ["success", "failed", "cancelled"].includes(item.status)) || Boolean(busy)} onClick={async () => { setBusy("clear"); try { const payload = await clearFinishedPaypalPayments(); setState(payload.paypal_payments); } finally { setBusy(""); } }}><Trash2 />清理已完成</Button></CardHeader>
      <CardContent className="space-y-3">
        {results.map((result) => {
          const terminal = ["success", "failed", "cancelled"].includes(result.status);
          const progress = Math.max(0, Math.min(100, Number(result.progress_percent || 0)));
          return <div key={result.task_id} className="border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="truncate font-semibold text-slate-950">{result.email || result.source_task_id}</div><div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-500"><span>{result.protocol_mode || result.country}</span><span className="font-mono">{shortBa(result.ba_url)}</span>{result.proxy_label ? <span>{result.proxy_label}</span> : null}</div></div><Badge variant={statusVariant(result.status)}>{statusText(result.status)}</Badge></div>
            <div className="mt-3 flex items-center gap-3"><div className="h-1.5 min-w-0 flex-1 overflow-hidden bg-slate-100"><div className={`h-full ${result.status === "failed" ? "bg-rose-500" : "bg-emerald-500"}`} style={{ width: `${progress}%` }} /></div><span className="text-xs font-semibold tabular-nums text-slate-500">{progress}%</span></div>
            <div className="mt-2 text-xs leading-5 text-slate-600"><span className="font-semibold text-slate-800">{result.stage || statusText(result.status)}</span><span className="mx-2 text-slate-300">|</span>{resultSummary(result)}</div>
            <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-200 pt-3">{!terminal ? <Button size="sm" variant="destructive" disabled={Boolean(busy)} onClick={() => void taskAction(result, "cancel")}><X />停止</Button> : null}{terminal ? <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void taskAction(result, "retry")}><RotateCcw />重试</Button> : null}{terminal ? <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void taskAction(result, "delete")}><Trash2 />删除</Button> : null}<Button size="sm" variant="outline" onClick={() => void copyText(result.ba_url, "BA 链已复制")}><Copy />复制 BA</Button></div>
          </div>;
        })}
        {!results.length ? <div className="border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">暂无协议支付任务</div> : null}
      </CardContent>
    </Card>
  </div>;
}

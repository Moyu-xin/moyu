"use client";

import { Clock3, KeyRound, PackageCheck, ShieldCheck, Warehouse } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";

import { useSettingsStore } from "../store";

function ToggleRow({ checked, disabled, label, detail, onChange }: { checked: boolean; disabled: boolean; label: string; detail: string; onChange: (checked: boolean) => void }) {
  return (
    <label className="flex min-h-14 items-start gap-3 border-l border-slate-200 pl-4 first:border-l-0 first:pl-0">
      <Checkbox checked={checked} disabled={disabled} onCheckedChange={onChange} className="mt-0.5" />
      <span><span className="block text-sm font-semibold text-slate-900">{label}</span><span className="mt-1 block text-xs leading-5 text-slate-500">{detail}</span></span>
    </label>
  );
}

export function GrokConfigCard() {
  const config = useSettingsStore((state) => state.registerConfig);
  const setGrokConfig = useSettingsStore((state) => state.setGrokConfig);
  if (!config || config.register_mode !== "grok" || !config.grok) return null;

  const grok = config.grok;
  const disabled = Boolean(config.enabled);
  const shippingOnly = grok.delivery_target === "shipping";
  return (
    <section className="border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="flex size-9 items-center justify-center bg-slate-950 text-emerald-300"><KeyRound className="size-5" /></div>
          <div><h3 className="text-sm font-semibold text-slate-950">Grok 注册设置</h3><div className="mt-0.5 text-xs text-slate-500">邮箱验证、SSO/OAuth 与仓库入库</div></div>
        </div>
        <Badge variant="outline" className="rounded-md">打码服务由左侧“打码配置”管理</Badge>
      </div>

      <div className="grid gap-4 border-b border-slate-200 p-4 md:grid-cols-3">
        <label className="space-y-1.5"><span className="flex items-center gap-1.5 text-xs font-medium text-slate-600"><Clock3 className="size-3.5" />邮件验证码超时/秒</span><Input type="number" min={30} max={300} value={String(grok.email_timeout)} disabled={disabled} onChange={(event) => setGrokConfig({ email_timeout: Math.max(30, Math.min(300, Number(event.target.value) || 180)) })} className="h-10 rounded-none" /></label>
        <label className="space-y-1.5"><span className="text-xs font-medium text-slate-600">xAI 请求超时/秒</span><Input type="number" min={10} max={120} value={String(grok.request_timeout)} disabled={disabled} onChange={(event) => setGrokConfig({ request_timeout: Math.max(10, Math.min(120, Number(event.target.value) || 30)) })} className="h-10 rounded-none" /></label>
        <label className="space-y-1.5"><span className="text-xs font-medium text-slate-600">OAuth 交换超时/秒</span><Input type="number" min={15} max={180} value={String(grok.oauth_timeout)} disabled={disabled} onChange={(event) => setGrokConfig({ oauth_timeout: Math.max(15, Math.min(180, Number(event.target.value) || 45)) })} className="h-10 rounded-none" /></label>
      </div>

      <div className="border-b border-slate-200 p-4">
        <div className="mb-2 text-xs font-medium text-slate-600">注册成功后的保存位置</div>
        <div className="inline-grid w-full max-w-xl grid-cols-2 border border-slate-300 bg-slate-50 p-1">
          <button
            type="button"
            disabled={disabled}
            onClick={() => setGrokConfig({ delivery_target: "warehouses" })}
            className={`flex min-h-11 items-center justify-center gap-2 px-4 text-sm font-semibold transition ${!shippingOnly ? "bg-slate-950 text-white shadow-sm" : "text-slate-600 hover:bg-white"}`}
          >
            <Warehouse className="size-4" />仓库直入
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => setGrokConfig({ delivery_target: "shipping", auto_import_sub2api: false, auto_import_cpa: false, auto_import_grok2api: false })}
            className={`flex min-h-11 items-center justify-center gap-2 px-4 text-sm font-semibold transition ${shippingOnly ? "bg-emerald-600 text-white shadow-sm" : "text-slate-600 hover:bg-white"}`}
          >
            <PackageCheck className="size-4" />仅保存到出货账号
          </button>
        </div>
        <p className="mt-2 text-xs leading-5 text-slate-500">“出货账号”使用独立库存；选择后不会写入成品、半成品、sub2api、CPA 或 grok2api。</p>
      </div>

      <div className="grid gap-4 px-4 py-3 md:grid-cols-2 xl:grid-cols-4">
        <ToggleRow checked={grok.exchange_oauth} disabled={disabled} label="注册后换取 Grok OAuth" detail="完成 xAI 注册后继续交换 CLI OAuth 凭据。" onChange={(checked) => setGrokConfig({ exchange_oauth: checked })} />
        <ToggleRow checked={grok.auto_import_sub2api} disabled={disabled || shippingOnly} label="自动导入 sub2api" detail="成功账号按中转仓库默认分组入库。" onChange={(checked) => setGrokConfig({ auto_import_sub2api: checked })} />
        <ToggleRow checked={grok.auto_import_cpa} disabled={disabled || shippingOnly} label="自动导入 CPA" detail="同时生成并上传 xAI OAuth auth file。" onChange={(checked) => setGrokConfig({ auto_import_cpa: checked })} />
        <ToggleRow checked={grok.auto_import_grok2api} disabled={disabled || shippingOnly} label="自动导入 grok2api" detail="把原始 xAI SSO 直接写入 Grok Web 账号池，用于 Web Imagine 生图。" onChange={(checked) => setGrokConfig({ auto_import_grok2api: checked })} />
      </div>

      <div className="flex items-center gap-2 border-t border-slate-200 bg-emerald-50/60 px-4 py-2 text-xs text-emerald-800">
        <ShieldCheck className="size-4" />邮箱由“邮箱配置”当前来源提供；账号产物和入库策略在此设置。
        <PackageCheck className="ml-auto size-4" />
      </div>
    </section>
  );
}

"use client";

import { Copy, ExternalLink, KeyRound, LoaderCircle, X } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AccountOAuthSession } from "@/lib/api";

type AccountOAuthDialogProps = {
  session: AccountOAuthSession | null;
  busy?: boolean;
  onClose: () => void;
  onComplete: (callback: string) => Promise<void>;
};

export function AccountOAuthDialog({ session, busy = false, onClose, onComplete }: AccountOAuthDialogProps) {
  const [callback, setCallback] = useState("");

  useEffect(() => {
    setCallback("");
  }, [session?.job_id]);

  if (!session) return null;

  const copyLink = async () => {
    await navigator.clipboard.writeText(session.auth_url);
    toast.success("授权链接已复制");
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/50 p-4 backdrop-blur-xs" role="dialog" aria-modal="true" aria-label="OpenAI OAuth 授权">
      <div className="w-full max-w-2xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div>
            <div className="flex items-center gap-2 text-base font-semibold text-slate-950">
              <KeyRound className="size-5 text-emerald-600" />
              获取 OAuth 并导入仓库
            </div>
            <div className="mt-1 text-sm text-slate-500">{session.email}</div>
          </div>
          <button type="button" className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700" onClick={onClose} disabled={busy} title="关闭">
            <X className="size-5" />
          </button>
        </div>

        <div className="space-y-5 px-5 py-5">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" className="h-9 bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => window.open(session.auth_url, "_blank", "noopener,noreferrer")} disabled={busy}>
              <ExternalLink className="size-4" />
              打开授权页
            </Button>
            <Button type="button" variant="outline" className="h-9" onClick={() => void copyLink()} disabled={busy}>
              <Copy className="size-4" />
              复制链接
            </Button>
            <span className="text-xs text-slate-400">有效至 {new Date(session.expires_at).toLocaleTimeString()}</span>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium text-slate-800" htmlFor="oauth-callback-input">回调链接或 Code</label>
            <Input
              id="oauth-callback-input"
              value={callback}
              onChange={(event) => setCallback(event.target.value)}
              placeholder="http://localhost:1455/auth/callback?code=...&state=..."
              className="h-11 font-mono text-xs"
              disabled={busy}
              autoFocus
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-4">
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>取消</Button>
          <Button type="button" className="bg-sky-700 text-white hover:bg-sky-800" onClick={() => void onComplete(callback.trim())} disabled={busy || !callback.trim()}>
            {busy ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
            完成授权并导入
          </Button>
        </div>
      </div>
    </div>
  );
}

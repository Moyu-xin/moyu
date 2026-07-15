"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { LogOut, ShieldCheck } from "lucide-react";

import { BrandMark } from "@/components/brand-mark";
import { Button } from "@/components/ui/button";
import { getValidatedAuthSession } from "@/lib/auth-session";
import { clearStoredAuthSession, type StoredAuthSession } from "@/store/auth";

export function TopNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSession] = useState<StoredAuthSession | null | undefined>(undefined);

  useEffect(() => {
    let active = true;

    const load = async () => {
      if (pathname === "/login") {
        if (active) setSession(null);
        return;
      }
      const storedSession = await getValidatedAuthSession();
      if (active) setSession(storedSession);
    };

    void load();
    return () => {
      active = false;
    };
  }, [pathname]);

  const handleLogout = async () => {
    await clearStoredAuthSession();
    router.replace("/login");
  };

  if (pathname === "/login" || session === undefined || !session) {
    return null;
  }

  return (
    <header className="rounded-[26px] border border-slate-200/80 bg-white/[0.88] shadow-[0_16px_42px_-36px_rgba(15,23,42,0.45)] backdrop-blur">
      <div className="flex h-16 items-center justify-between gap-3 px-4 sm:px-5">
        <Link href="/register" className="flex min-w-0 items-center gap-3 text-[15px] font-semibold tracking-tight text-slate-950">
          <BrandMark className="size-9" />
          <span className="min-w-0">
            <span className="block truncate">摸鱼机</span>
            <span className="block truncate text-xs font-medium text-slate-500">本地控制台</span>
          </span>
        </Link>
        <nav className="flex min-w-0 items-center justify-end gap-2">
          <Link href="/register" className="hidden items-center gap-1.5 rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[13px] font-semibold text-emerald-700 transition hover:bg-emerald-100 sm:inline-flex">
            <ShieldCheck className="size-4" />
            控制台
          </Link>
          <Button variant="ghost" size="sm" onClick={() => void handleLogout()} className="rounded-2xl px-3 text-slate-600 hover:bg-slate-100 hover:text-slate-950">
            <LogOut className="size-4" />
            退出
          </Button>
        </nav>
      </div>
    </header>
  );
}

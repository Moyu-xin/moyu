import type { Metadata, Viewport } from "next";
import { Toaster } from "sonner";
import "./globals.css";
import { ThemeScript } from "@/components/theme-script";
import { TopNav } from "@/components/top-nav";

export const metadata: Metadata = {
  title: "摸鱼机",
  description: "本地注册、邮箱配置、账号库存与测活控制台",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#f8fafc" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <ThemeScript />
      </head>
      <body
        className="min-h-[100dvh] bg-slate-50 text-slate-900 antialiased"
        style={{
          fontFamily:
            'Geist, "SF Pro Display", "SF Pro Text", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif',
        }}
      >
        <Toaster position="top-center" richColors offset={48} />
        <main className="relative min-h-[100dvh] w-full max-w-[100vw] overflow-x-hidden bg-[radial-gradient(circle_at_18%_-12%,rgba(16,185,129,0.14),transparent_30%),radial-gradient(circle_at_86%_4%,rgba(15,23,42,0.08),transparent_26%),linear-gradient(180deg,#f8fafc_0%,#f1f5f9_58%,#eef2f7_100%)] px-0 pt-0 pb-3 text-slate-900 transition-colors duration-300 sm:px-2 sm:pt-2 lg:px-3">
          <div className="box-border flex min-h-[100dvh] w-full max-w-[100vw] flex-col gap-3 pt-[env(safe-area-inset-top)] sm:gap-4 sm:pt-0">
            <TopNav />
            {children}
          </div>
        </main>
      </body>
    </html>
  );
}

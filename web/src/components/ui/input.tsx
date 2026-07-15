import * as React from "react";
import { cn } from "@/lib/utils";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {}

export function Input({ className, type = "text", ...props }: InputProps) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-11 w-full min-w-0 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm text-slate-900 shadow-sm outline-none transition-[border-color,box-shadow,background-color] selection:bg-emerald-100 selection:text-emerald-900 placeholder:text-slate-400 file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-slate-50 disabled:opacity-60 focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100",
        className
      )}
      {...props}
    />
  );
}

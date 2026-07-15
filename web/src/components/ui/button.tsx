import * as React from "react";
import { cn } from "@/lib/utils";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "destructive" | "outline" | "secondary" | "ghost" | "link";
  size?: "default" | "sm" | "lg" | "icon";
}

export function Button({
  className,
  variant = "default",
  size = "default",
  type = "button",
  ...props
}: ButtonProps) {
  const baseStyles =
    "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-xl text-sm font-medium outline-none transition-all duration-200 active:translate-y-px disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100";

  const variantStyles = {
    default: "bg-slate-950 text-white shadow-sm hover:bg-slate-800",
    destructive: "bg-rose-600 text-white shadow-sm hover:bg-rose-700 focus-visible:ring-rose-100",
    outline: "border border-slate-200 bg-white text-slate-700 shadow-sm hover:bg-slate-50 hover:text-slate-950",
    secondary: "bg-slate-100 text-slate-800 shadow-sm hover:bg-slate-200",
    ghost: "text-slate-600 hover:bg-slate-100 hover:text-slate-950",
    link: "text-emerald-700 underline-offset-4 hover:underline",
  };

  const sizeStyles = {
    default: "h-9 px-4 py-2 has-[>svg]:px-3",
    sm: "h-8 rounded-lg gap-1.5 px-3 has-[>svg]:px-2.5",
    lg: "h-10 rounded-2xl px-6 has-[>svg]:px-4",
    icon: "size-9",
  };

  return (
    <button
      type={type}
      className={cn(baseStyles, variantStyles[variant], sizeStyles[size], className)}
      {...props}
    />
  );
}

import * as React from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

export interface CheckboxProps {
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
}

export function Checkbox({ checked, onCheckedChange, disabled, className }: CheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      className={cn(
        "peer size-4 shrink-0 rounded-[4px] border border-slate-300 bg-white flex items-center justify-center shadow-sm outline-none transition-all",
        checked && "border-emerald-600 bg-emerald-600 text-white",
        disabled && "cursor-not-allowed opacity-50 bg-slate-50",
        "focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100",
        className
      )}
    >
      {checked && <Check className="size-3.5 stroke-[3px] text-white" />}
    </button>
  );
}

import * as React from "react";
import { cn } from "@/lib/utils";

type Tone = "default" | "accent" | "warn" | "ok" | "danger";

const tones: Record<Tone, string> = {
  default: "border-line bg-surface-2 text-ink-dim",
  accent: "border-accent/30 bg-accent/10 text-accent-soft",
  warn: "border-warn/30 bg-warn/10 text-warn",
  ok: "border-ok/30 bg-ok/10 text-ok",
  danger: "border-danger/30 bg-danger/10 text-danger",
};

export function Badge({
  className,
  tone = "default",
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium leading-none",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}

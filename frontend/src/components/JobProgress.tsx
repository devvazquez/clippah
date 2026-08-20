"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import { STAGE_LABELS, type ProgressEvent } from "@/lib/api";
import { MomentCardSkeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";

/** Progreso en vivo: barra, etapa actual y las ultimas 5 lineas del SSE. */
export function JobProgress({
  stage,
  progress,
  message,
  log,
  warnings,
}: {
  stage: string;
  progress: number;
  message: string;
  log: ProgressEvent[];
  warnings: string[];
}) {
  const pct = Math.round(progress * 100);
  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="mb-2 flex items-baseline justify-between gap-3">
          <p className="flex items-center gap-2 text-[13px] font-medium text-ink">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
            {STAGE_LABELS[stage] ?? stage}
            <span className="font-normal text-ink-dim">{message}</span>
          </p>
          <span className="text-[13px] font-semibold text-ink tnum">{pct}%</span>
        </div>
        <Progress value={progress} />

        <ul className="mt-3 space-y-0.5 font-mono text-[11px] leading-relaxed text-ink-faint">
          {log.slice(-5).map((e, i) => (
            <li key={`${e.seq ?? i}-${e.stage}`} className="truncate">
              <span className="text-ink-dim">
                {(STAGE_LABELS[e.stage] ?? e.stage).padEnd(14, " ")}
              </span>
              {e.message ?? ""}
            </li>
          ))}
          {log.length === 0 ? <li>Conectando con el backend…</li> : null}
        </ul>
      </div>

      {warnings.length > 0 ? (
        <ul className="space-y-1.5">
          {warnings.map((w) => (
            <li
              key={w}
              className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn/[0.06] px-3 py-2 text-xs text-warn"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <MomentCardSkeleton key={i} />
        ))}
      </div>
    </div>
  );
}

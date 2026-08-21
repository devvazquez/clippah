"use client";

import { AlertTriangle, Check, Clock, Loader2, X } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cancelRequest } from "@/lib/supabase";
import { type ClipRequest, STAGE_LABELS } from "@/lib/types";
import { durationLabel } from "@/lib/utils";

const ICONS = {
  queued: Clock,
  claimed: Loader2,
  running: Loader2,
  done: Check,
  error: AlertTriangle,
  canceled: X,
} as const;

const TONES = {
  queued: "text-ink-faint",
  claimed: "text-accent-soft",
  running: "text-accent-soft",
  done: "text-ok",
  error: "text-danger",
  canceled: "text-ink-faint",
} as const;

function since(iso: string): string {
  const t = Date.parse(iso);
  if (!t) return "";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "ahora";
  if (diff < 3600) return `hace ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `hace ${Math.floor(diff / 3600)} h`;
  return new Date(t).toLocaleDateString("es-ES", { day: "2-digit", month: "short" });
}

function Row({ request, onChange }: { request: ClipRequest; onChange: () => void }) {
  const Icon = ICONS[request.status] ?? Clock;
  const tone = TONES[request.status] ?? "text-ink-faint";
  const live = request.status === "running" || request.status === "claimed";
  const title = request.video_title || request.url.replace(/^https?:\/\/(www\.)?/, "");

  async function cancel() {
    try {
      await cancelRequest(request.id);
      onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo cancelar");
    }
  }

  return (
    <li className="flex flex-col gap-2 border-b border-line px-4 py-3 last:border-0">
      <div className="flex items-start gap-2.5">
        <Icon
          className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${tone} ${live ? "animate-spin" : ""}`}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] text-ink" title={request.url}>
            {title}
          </p>
          <p className="tnum mt-0.5 text-[11px] text-ink-faint">
            {request.clips} clip{request.clips > 1 ? "s" : ""}
            {request.duration_s ? ` · ${durationLabel(request.duration_s)}` : ""}
            {" · "}
            {since(request.created_at)}
          </p>
        </div>
        {request.status === "queued" ? (
          <Button variant="ghost" size="icon" onClick={cancel} title="Quitar de la cola">
            <X className="h-3.5 w-3.5" aria-hidden />
          </Button>
        ) : null}
      </div>

      {live ? (
        <div className="flex flex-col gap-1 pl-6">
          <Progress value={Math.round(request.progress * 100)} />
          <p className="tnum text-[11px] text-ink-dim">
            {request.message || STAGE_LABELS[request.stage] || request.stage}
            {" · "}
            {Math.round(request.progress * 100)}%
          </p>
        </div>
      ) : null}

      {request.status === "error" && request.error ? (
        <p className="pl-6 text-[11px] text-danger">{request.error}</p>
      ) : null}

      {request.status === "done" ? (
        <p className="pl-6 text-[11px] text-ink-faint">{request.message}</p>
      ) : null}
    </li>
  );
}

/** La cola tal como esta ahora mismo: lo pendiente, lo que corre y lo ultimo que acabo. */
export function QueueList({
  requests,
  loading,
  onChange,
}: {
  requests: ClipRequest[];
  loading: boolean;
  onChange: () => void;
}) {
  const pending = requests.filter((r) => r.status === "queued").length;

  return (
    <section className="card overflow-hidden">
      <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h2 className="text-[13px] font-medium text-ink">Cola</h2>
        <span className="tnum text-[11px] text-ink-faint">
          {pending ? `${pending} esperando` : "al día"}
        </span>
      </header>
      {loading && requests.length === 0 ? (
        <p className="px-4 py-6 text-[12px] text-ink-faint">Cargando…</p>
      ) : requests.length === 0 ? (
        <p className="px-4 py-6 text-[12px] text-ink-faint">
          Nada por ahora. Pega un enlace arriba y quedará guardado aquí hasta que el
          backend lo coja.
        </p>
      ) : (
        <ul>
          {requests.map((r) => (
            <Row key={r.id} request={r} onChange={onChange} />
          ))}
        </ul>
      )}
    </section>
  );
}

"use client";

import {
  ChevronDown,
  ChevronUp,
  MessageSquare,
  Play,
  Scissors,
  Sparkles,
  Volume2,
  Zap,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, CATEGORY_LABELS, type Moment, api } from "@/lib/api";
import { cn, hhmmss, scoreTone } from "@/lib/utils";

export function MomentCard({
  moment,
  onPreview,
}: {
  moment: Moment;
  onPreview: (m: Moment) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [imgFailed, setImgFailed] = useState(false);
  const tone = scoreTone(moment.final_score);
  const score = Math.round(moment.final_score * 100);

  async function generateClip() {
    setRendering(true);
    try {
      await api.render(moment.id);
      // El backend devuelve 501: si llegamos aqui es que ya se implemento.
      toast.success("Render lanzado");
    } catch (err) {
      if (err instanceof ApiError && err.status === 501) {
        const detail = err.detail as { spec?: { t_start?: number; t_end?: number } } | null;
        const spec = detail?.spec;
        toast.info("Render no implementado todavía", {
          description: spec
            ? `RenderSpec listo: ${hhmmss(spec.t_start ?? 0)} → ${hhmmss(spec.t_end ?? 0)}`
            : undefined,
        });
      } else {
        toast.error(err instanceof ApiError ? err.message : "No se pudo generar el clip");
      }
    } finally {
      setRendering(false);
    }
  }

  return (
    <article className="card group flex flex-col overflow-hidden transition-colors hover:border-line/80">
      <div className="relative aspect-video w-full overflow-hidden bg-surface-2">
        {!imgFailed ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={moment.thumbnail_url}
            alt=""
            loading="lazy"
            onError={() => setImgFailed(true)}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-[11px] text-ink-faint">
            sin fotograma
          </div>
        )}
        <button
          onClick={() => onPreview(moment)}
          className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-all group-hover:bg-black/40 group-hover:opacity-100"
          aria-label={`Previsualizar ${moment.title}`}
        >
          <span className="rounded-full bg-black/70 p-2.5">
            <Play className="h-5 w-5 fill-white text-white" />
          </span>
        </button>
        <span className="pointer-events-none absolute bottom-1.5 right-1.5 rounded bg-black/80 px-1.5 py-0.5 text-[11px] font-medium text-ink tnum">
          {hhmmss(moment.t_peak)}
        </span>
        <span
          className={cn(
            "pointer-events-none absolute right-1.5 top-1.5 flex items-center gap-1 rounded bg-black/80 px-1.5 py-0.5 text-[11px] font-semibold tnum",
            tone.text,
          )}
          title={`Puntuación final ${score}/100`}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", tone.dot)} />
          {score}
        </span>
        <span className="pointer-events-none absolute bottom-1.5 left-1.5 rounded bg-black/80 px-1.5 py-0.5 text-[11px] text-ink-dim tnum">
          {Math.round(moment.duration)}s
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-1.5 p-3">
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-[13px] font-semibold leading-snug text-ink">{moment.title}</h3>
          {!moment.enriched ? (
            <Badge tone="warn" className="shrink-0" title="Generado sin LLM">
              <Sparkles className="h-2.5 w-2.5" /> Sin IA
            </Badge>
          ) : null}
        </div>
        <p className="line-clamp-2 text-xs leading-relaxed text-ink-dim">{moment.description}</p>

        <div className="mt-auto flex flex-wrap items-center gap-2 pt-1.5 text-[11px] text-ink-faint tnum">
          <Badge tone="accent">{CATEGORY_LABELS[moment.category]}</Badge>
          {moment.signals.msg_count > 0 ? (
            <span className="flex items-center gap-1" title="Mensajes de chat en la ventana">
              <MessageSquare className="h-3 w-3" />
              {moment.signals.msg_count}
            </span>
          ) : null}
          {moment.signals.audio_z !== 0 ? (
            <span className="flex items-center gap-1" title="Pico de audio sobre el nivel habitual">
              <Volume2 className="h-3 w-3" />
              {moment.signals.audio_z > 0 ? "+" : ""}
              {moment.signals.audio_z.toFixed(1)}σ
            </span>
          ) : null}
          {moment.signals.combo ? (
            <span className="flex items-center gap-1 text-accent-soft" title="Pico de audio Y de chat">
              <Zap className="h-3 w-3" /> combo
            </span>
          ) : null}
        </div>
      </div>

      {moment.transcript ? (
        <div className="border-t border-line">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex w-full items-center justify-between px-3 py-1.5 text-[11px] text-ink-faint transition-colors hover:text-ink-dim"
          >
            Transcripción
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
          {expanded ? (
            <p className="scrollbar-thin max-h-32 overflow-y-auto px-3 pb-2.5 text-xs leading-relaxed text-ink-dim">
              {moment.transcript}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-1.5 border-t border-line p-1.5">
        <Button variant="subtle" size="sm" onClick={() => onPreview(moment)}>
          <Play className="h-3.5 w-3.5" /> Previsualizar
        </Button>
        <Button variant="outline" size="sm" onClick={generateClip} disabled={rendering}>
          <Scissors className="h-3.5 w-3.5" /> Generar clip
        </Button>
      </div>
    </article>
  );
}

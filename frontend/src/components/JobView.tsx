"use client";

import {
  AlertTriangle,
  ArrowLeft,
  MessageSquareOff,
  RefreshCw,
  Sparkles,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { JobProgress } from "@/components/JobProgress";
import { MomentGrid } from "@/components/MomentGrid";
import { Thumb } from "@/components/Thumb";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, type Job, type ProgressEvent, api } from "@/lib/api";
import { durationLabel, hhmmss } from "@/lib/utils";

const TERMINAL = new Set(["done", "error", "cancelled"]);

export function JobView({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [log, setLog] = useState<ProgressEvent[]>([]);
  const [live, setLive] = useState({ stage: "queued", progress: 0, message: "" });
  const lastSeq = useRef(0);
  const source = useRef<EventSource | null>(null);

  const refetch = useCallback(async () => {
    try {
      const fresh = await api.getJob(jobId);
      setJob(fresh);
      setLive((prev) => ({
        stage: fresh.stage || prev.stage,
        progress: Math.max(prev.progress, fresh.progress),
        message: fresh.message || prev.message,
      }));
      return fresh;
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "No se pudo cargar el job");
      return null;
    }
  }, [jobId]);

  useEffect(() => {
    let closed = false;

    (async () => {
      const fresh = await refetch();
      if (closed || !fresh) return;
      if (TERMINAL.has(fresh.status)) return;

      // El SSE reenvia el historial desde `after`, asi que al recargar la pagina o
      // reabrir el navegador el progreso se recupera intacto sin hacer polling.
      const es = new EventSource(`/api/jobs/${jobId}/events?after=${lastSeq.current}`);
      source.current = es;

      es.onmessage = (raw) => {
        let event: ProgressEvent;
        try {
          event = JSON.parse(raw.data) as ProgressEvent;
        } catch {
          return;
        }
        if (event.seq !== undefined) {
          if (event.seq <= lastSeq.current) return;
          lastSeq.current = event.seq;
        }
        if (event.stage === "warning") {
          setJob((prev) =>
            prev && event.message && !prev.warnings.includes(event.message)
              ? { ...prev, warnings: [...prev.warnings, event.message] }
              : prev,
          );
          setLog((prev) => [...prev.slice(-40), event]);
          return;
        }
        setLog((prev) => [...prev.slice(-40), event]);
        setLive((prev) => ({
          stage: event.stage || prev.stage,
          progress: Math.max(prev.progress, event.progress ?? prev.progress),
          message: event.message ?? (event.stage === "done" ? "Análisis completado" : prev.message),
        }));
        if (TERMINAL.has(event.stage)) {
          es.close();
          source.current = null;
          void refetch();
        }
      };

      es.onerror = () => {
        // EventSource reintenta solo; si el job ya acabo, cerramos y refrescamos.
        void api.getJob(jobId).then((fresh2) => {
          if (TERMINAL.has(fresh2.status)) {
            es.close();
            source.current = null;
            setJob(fresh2);
          }
        });
      };
    })();

    return () => {
      closed = true;
      source.current?.close();
      source.current = null;
    };
  }, [jobId, refetch]);

  async function cancel() {
    try {
      await api.cancelJob(jobId);
      toast.success("Análisis cancelado");
      source.current?.close();
      await refetch();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "No se pudo cancelar");
    }
  }

  if (loadError) {
    return (
      <div className="card flex flex-col items-center gap-3 p-8 text-center">
        <XCircle className="h-6 w-6 text-danger" />
        <p className="text-sm text-ink">{loadError}</p>
        <Link href="/">
          <Button variant="outline" size="sm">
            <ArrowLeft className="h-3.5 w-3.5" /> Volver
          </Button>
        </Link>
      </div>
    );
  }

  const running = job ? !TERMINAL.has(job.status) : true;
  const video = job?.video ?? null;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start gap-4">
        <Link href="/" className="mt-0.5 text-ink-faint transition-colors hover:text-ink">
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <div className="h-[68px] w-[120px] shrink-0 overflow-hidden rounded border border-line bg-surface-2">
          <Thumb src={video?.thumbnail} />
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold tracking-tight text-ink">
            {video?.title ?? job?.url ?? "Cargando…"}
          </h1>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-dim tnum">
            {video?.uploader ? <span className="text-ink">{video.uploader}</span> : null}
            {video?.duration ? <span>· {durationLabel(video.duration)}</span> : null}
            {job && job.moments.length > 0 ? (
              <span>
                · <span className="text-ink">{job.moments.length}</span> momentos
              </span>
            ) : null}
            {job?.chat_messages ? (
              <span>· {job.chat_messages.toLocaleString("es-ES")} mensajes de chat</span>
            ) : null}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {job && !job.chat_available && !running ? (
              <Badge tone="warn">
                <MessageSquareOff className="h-2.5 w-2.5" /> sin chat — solo audio
              </Badge>
            ) : null}
            {job && !job.enriched && job.moments.length > 0 ? (
              <Badge tone="warn" title="Activa GEMINI_API_KEY para títulos y descripciones con IA">
                <Sparkles className="h-2.5 w-2.5" /> Sin IA — activa una API key
              </Badge>
            ) : null}
            {job?.status === "cancelled" ? <Badge>cancelado</Badge> : null}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {running ? (
            <Button variant="danger" size="sm" onClick={cancel}>
              Cancelar
            </Button>
          ) : (
            <Button variant="ghost" size="sm" onClick={() => void refetch()}>
              <RefreshCw className="h-3.5 w-3.5" /> Refrescar
            </Button>
          )}
        </div>
      </div>

      {job?.status === "error" ? (
        <div className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/[0.06] px-3 py-2.5 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{job.error ?? "El análisis falló"}</span>
        </div>
      ) : null}

      {running ? (
        <JobProgress
          stage={live.stage}
          progress={live.progress}
          message={live.message}
          log={log}
          warnings={job?.warnings ?? []}
        />
      ) : job && job.moments.length > 0 ? (
        <>
          {job.warnings.length > 0 ? (
            <ul className="space-y-1.5">
              {job.warnings.map((w) => (
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
          <MomentGrid moments={job.moments} video={video} />
        </>
      ) : job?.status === "done" ? (
        <p className="rounded-md border border-dashed border-line px-3 py-10 text-center text-xs text-ink-faint">
          El análisis terminó sin momentos destacados. Prueba con un VOD con más actividad
          o baja <code className="text-ink-dim">PEAK_PERCENTILE</code>.
        </p>
      ) : null}

      {job && !running && job.moments.length > 0 ? (
        <p className="pt-2 text-[11px] text-ink-faint tnum">
          Ventanas de {hhmmss(Math.min(...job.moments.map((m) => m.t_start)))} a{" "}
          {hhmmss(Math.max(...job.moments.map((m) => m.t_end)))} · transcripción:{" "}
          {job.transcribed ? "sí" : "no"} · IA: {job.enriched ? "sí" : "no"}
        </p>
      ) : null}
    </div>
  );
}

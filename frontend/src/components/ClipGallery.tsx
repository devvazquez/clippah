"use client";

import { ArrowLeft, ExternalLink } from "lucide-react";
import * as React from "react";
import { ClipCard } from "@/components/ClipCard";
import { StreamGrid } from "@/components/StreamGrid";
import { Button } from "@/components/ui/button";
import { groupByStream } from "@/lib/streams";
import type { Clip } from "@/lib/types";
import { streamDate } from "@/lib/utils";

/**
 * La galeria, en dos niveles: primero los directos y luego los clips de uno.
 *
 * Es como se piensan al subirlos a redes: primero eliges de que directo tiras y luego
 * cual de sus momentos. Con veinte clips de cinco directos, verlos todos de golpe no
 * ayuda a decidir.
 */
export function ClipGallery({
  clips,
  urls,
  loading,
}: {
  clips: Clip[];
  /** URLs firmadas por ruta de Storage: valen para los mp4 y para las portadas. */
  urls: Record<string, string>;
  loading: boolean;
}) {
  const [openKey, setOpenKey] = React.useState<string | null>(null);
  const streams = React.useMemo(() => groupByStream(clips), [clips]);
  // Se busca por clave en vez de guardar el objeto: asi lo que se ve sigue vivo cuando
  // Realtime trae un clip nuevo de ese mismo directo.
  const open = streams.find((s) => s.key === openKey) ?? null;

  if (loading && clips.length === 0) {
    return (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(232px,1fr))] gap-3">
        {Array.from({ length: 3 }, (_, i) => (
          <div key={i} className="card aspect-video animate-pulse bg-surface-2" />
        ))}
      </div>
    );
  }

  if (clips.length === 0) {
    return (
      <div className="card p-8 text-center">
        <p className="text-[13px] text-ink-dim">Todavía no hay clips.</p>
        <p className="mt-1 text-[12px] text-ink-faint">
          Encola un directo: aparecerán aquí en cuanto el backend los renderice, sin
          recargar la página.
        </p>
      </div>
    );
  }

  if (!open) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-[11px] uppercase tracking-wide text-ink-faint">
          {streams.length} directo{streams.length > 1 ? "s" : ""} · {clips.length} clips
        </p>
        <StreamGrid streams={streams} posters={urls} onOpen={setOpenKey} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <header className="flex flex-wrap items-center gap-3 border-b border-line pb-3">
        <Button variant="outline" size="sm" onClick={() => setOpenKey(null)}>
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          Directos
        </Button>
        <div className="min-w-0">
          <h2 className="truncate text-[13px] font-medium leading-snug text-ink">
            {open.title}
          </h2>
          <p className="tnum text-[11px] text-ink-faint">
            {streamDate(open.date) || "sin fecha"}
            {open.vodId ? ` · ${open.vodId}` : ""}
            {` · ${open.clips.length} clip${open.clips.length > 1 ? "s" : ""}`}
            {" · mejores primero"}
          </p>
        </div>
        {open.url ? (
          <a
            href={open.url}
            target="_blank"
            rel="noreferrer"
            className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-ink-faint hover:text-ink-dim"
          >
            ver el directo
            <ExternalLink className="h-3 w-3" aria-hidden />
          </a>
        ) : null}
      </header>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(168px,1fr))] gap-3">
        {open.clips.map((clip) => (
          <ClipCard key={clip.id} clip={clip} src={urls[clip.storage_path]} />
        ))}
      </div>
    </div>
  );
}

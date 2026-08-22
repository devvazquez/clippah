"use client";

import { ChevronRight, Film } from "lucide-react";
import type { Stream } from "@/lib/streams";
import { streamDate } from "@/lib/utils";

/**
 * El primer nivel de la galeria: una tarjeta por directo con su portada y cuantos clips
 * tiene. Al pulsarla se abren sus clips, ordenados por puntuacion.
 */
export function StreamGrid({
  streams,
  posters,
  onOpen,
}: {
  streams: Stream[];
  posters: Record<string, string>;
  onOpen: (key: string) => void;
}) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(232px,1fr))] gap-3">
      {streams.map((stream) => {
        const poster = stream.poster ? posters[stream.poster] : undefined;
        const n = stream.clips.length;
        return (
          <button
            key={stream.key}
            type="button"
            onClick={() => onOpen(stream.key)}
            className="card animate-fade-in group flex flex-col overflow-hidden text-left transition-colors hover:border-ink-faint focus-visible:border-accent focus-visible:outline-none"
          >
            <div className="relative bg-surface-2">
              {poster ? (
                // Un <img> normal: la portada es un jpg de 30 kB firmado desde Storage, y
                // el export estatico no lleva optimizador de imagenes detras.
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={poster}
                  alt=""
                  loading="lazy"
                  className="aspect-video w-full object-cover"
                />
              ) : (
                <div className="grid aspect-video w-full place-items-center">
                  <Film className="h-5 w-5 text-ink-faint" aria-hidden />
                </div>
              )}
              <span className="tnum absolute right-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-medium text-ink">
                {n} clip{n > 1 ? "s" : ""}
              </span>
            </div>

            <div className="flex flex-1 flex-col gap-1 p-3">
              <h3 className="line-clamp-2 text-[13px] font-medium leading-snug text-ink">
                {stream.title}
              </h3>
              <p className="tnum mt-auto flex items-center gap-1.5 text-[11px] text-ink-faint">
                {streamDate(stream.date) || "sin fecha"}
                {stream.vodId ? ` · ${stream.vodId}` : ""}
                <ChevronRight
                  className="ml-auto h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5"
                  aria-hidden
                />
              </p>
            </div>
          </button>
        );
      })}
    </div>
  );
}

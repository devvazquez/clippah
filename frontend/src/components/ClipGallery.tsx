"use client";

import * as React from "react";
import { ClipCard } from "@/components/ClipCard";
import type { Clip } from "@/lib/types";

/**
 * Los clips agrupados por directo, que es como se piensan al subirlos: primero eliges de
 * que directo tiras y luego cual de sus momentos.
 */
export function ClipGallery({
  clips,
  urls,
  loading,
}: {
  clips: Clip[];
  urls: Record<string, string>;
  loading: boolean;
}) {
  const groups = React.useMemo(() => {
    const byVideo = new Map<string, Clip[]>();
    for (const clip of clips) {
      const key = clip.video_url || clip.video_title || "otros";
      const list = byVideo.get(key);
      if (list) list.push(clip);
      else byVideo.set(key, [clip]);
    }
    return [...byVideo.entries()].map(([key, list]) => ({
      key,
      title: list[0].video_title || key,
      url: list[0].video_url,
      // Lo unico que distingue un directo de otro cuando todos se titulan igual.
      vodId: (list[0].video_url ?? "").match(/(\d{6,})/)?.[1] ?? "",
      clips: [...list].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)),
    }));
  }, [clips]);

  if (loading && clips.length === 0) {
    return (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(168px,1fr))] gap-3">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="card aspect-[9/16] animate-pulse bg-surface-2" />
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

  return (
    <div className="flex flex-col gap-6">
      {groups.map((group) => (
        // Cabecera a la izquierda y clips a la derecha: los directos de este canal se
        // llaman todos igual, asi que lo que distingue al grupo es el id del VOD, y de
        // paso un directo con un solo clip no deja media fila vacia.
        <section
          key={group.key}
          className="grid gap-3 border-t border-line pt-3 lg:grid-cols-[190px_minmax(0,1fr)] lg:gap-5"
        >
          <header className="flex flex-col gap-0.5">
            <h2 className="text-[13px] font-medium leading-snug text-ink">{group.title}</h2>
            <p className="tnum text-[11px] text-ink-faint">
              {group.vodId ? `${group.vodId} · ` : ""}
              {group.clips.length} clip{group.clips.length > 1 ? "s" : ""}
            </p>
            {group.url ? (
              <a
                href={group.url}
                target="_blank"
                rel="noreferrer"
                className="text-[11px] text-ink-faint hover:text-ink-dim"
              >
                ver el directo
              </a>
            ) : null}
          </header>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(168px,1fr))] gap-3">
            {group.clips.map((clip) => (
              <ClipCard key={clip.id} clip={clip} src={urls[clip.storage_path]} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

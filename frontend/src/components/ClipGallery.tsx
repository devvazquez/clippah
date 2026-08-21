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
      clips: [...list].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)),
    }));
  }, [clips]);

  if (loading && clips.length === 0) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
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
    <div className="flex flex-col gap-8">
      {groups.map((group) => (
        <section key={group.key} className="flex flex-col gap-3">
          <header className="flex items-baseline justify-between gap-3 border-b border-line pb-2">
            <h2 className="truncate text-[13px] font-medium text-ink">{group.title}</h2>
            <span className="tnum shrink-0 text-[11px] text-ink-faint">
              {group.clips.length} clip{group.clips.length > 1 ? "s" : ""}
              {group.url ? (
                <>
                  {" · "}
                  <a href={group.url} target="_blank" rel="noreferrer" className="hover:text-ink-dim">
                    ver el directo
                  </a>
                </>
              ) : null}
            </span>
          </header>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
            {group.clips.map((clip) => (
              <ClipCard key={clip.id} clip={clip} src={urls[clip.storage_path]} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

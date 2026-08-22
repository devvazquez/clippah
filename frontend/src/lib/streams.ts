/**
 * Los clips agrupados por directo.
 *
 * La tabla `clips` guarda de que VOD sale cada uno, pero no hay tabla de directos: no
 * hace falta, porque un directo *es* el conjunto de sus clips. Agrupar aqui deja la
 * galeria con dos niveles (directos → clips de ese directo) sin nada mas en Supabase.
 */

import type { Clip } from "@/lib/types";

export interface Stream {
  /** El VOD, que es lo que de verdad identifica al directo. */
  key: string;
  title: string;
  url: string | null;
  /** Lo unico que distingue un directo de otro cuando todos se titulan igual. */
  vodId: string;
  date: string;
  /** Ordenados por puntuacion: el mejor clip del directo va primero. */
  clips: Clip[];
  /** Portada: la del mejor clip que tenga una. */
  poster: string | null;
  best: number;
}

export function groupByStream(clips: Clip[]): Stream[] {
  const byVideo = new Map<string, Clip[]>();
  for (const clip of clips) {
    const key = clip.video_url || clip.video_title || "otros";
    const list = byVideo.get(key);
    if (list) list.push(clip);
    else byVideo.set(key, [clip]);
  }

  const streams = [...byVideo.entries()].map(([key, list]) => {
    const sorted = [...list].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    const head = sorted[0];
    return {
      key,
      title: head.video_title || key,
      url: head.video_url,
      vodId: (head.video_url ?? "").match(/(\d{6,})/)?.[1] ?? "",
      date: sorted.find((c) => c.video_date)?.video_date ?? "",
      clips: sorted,
      poster: sorted.find((c) => c.poster_path)?.poster_path ?? null,
      best: head.score ?? 0,
    };
  });

  // El directo mas reciente primero. Los clips viejos no tienen fecha del directo
  // guardada, asi que de reserva vale cuando se genero el clip.
  return streams.sort((a, b) => {
    if (a.date && b.date && a.date !== b.date) return b.date.localeCompare(a.date);
    return b.clips[0].created_at.localeCompare(a.clips[0].created_at);
  });
}

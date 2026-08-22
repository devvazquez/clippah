"use client";

import { Download, Film, Loader2 } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { downloadUrl, isConfigured, listClips, signClips, watchTable } from "@/lib/supabase";
import { groupByStream } from "@/lib/streams";
import type { Clip } from "@/lib/types";
import { clipFilename, hhmmss, streamDate } from "@/lib/utils";

/**
 * La pagina que se le pasa al streamer: solo descargar.
 *
 * Es la misma tabla de Supabase que la galeria, pero sin cola, sin editor y sin nada que
 * se pueda tocar: se entra, se bajan los mp4 tal cual salieron del render (1080x1920, sin
 * recomprimir) y se suben. Cada clip lleva la fecha del directo en el nombre para que en
 * la carpeta de descargas queden en orden.
 */
export default function Descargas() {
  const [clips, setClips] = React.useState<Clip[]>([]);
  const [urls, setUrls] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(true);
  const [bulk, setBulk] = React.useState(0);
  const configured = isConfigured();

  const refresh = React.useCallback(async () => {
    try {
      const rows = (await listClips()).filter((c) => c.render_status !== "rendering");
      setClips(rows);
      const paths = rows.flatMap((c) =>
        c.poster_path ? [c.storage_path, c.poster_path] : [c.storage_path],
      );
      const signed = await signClips(paths);
      setUrls((prev) => ({ ...prev, ...signed }));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudieron leer los clips");
    }
  }, []);

  React.useEffect(() => {
    if (!configured) return;
    void refresh().then(() => setLoading(false));
    return watchTable("clips", () => void refresh());
  }, [configured, refresh]);

  function save(clip: Clip) {
    const src = urls[clip.storage_path];
    if (!src) return;
    const a = document.createElement("a");
    a.href = downloadUrl(src, clipFilename(clip));
    a.rel = "noreferrer";
    a.click();
  }

  async function saveAll(list: Clip[]) {
    // De uno en uno y con un respiro entre medias: los navegadores cancelan la tanda si
    // se disparan todas a la vez, y el primero pide permiso para bajar varios ficheros.
    setBulk(list.length);
    for (const [i, clip] of list.entries()) {
      save(clip);
      setBulk(list.length - i - 1);
      await new Promise((r) => setTimeout(r, 900));
    }
    setBulk(0);
    toast.success(`${list.length} clips enviados a tus descargas`);
  }

  const streams = React.useMemo(() => groupByStream(clips), [clips]);
  const megas = clips.reduce((n, c) => n + c.size_bytes, 0) / 1048576;

  if (!configured) {
    return (
      <p className="card mx-auto mt-16 max-w-md p-6 text-[13px] text-ink-dim">
        Esta página se construyó sin las claves de Supabase, así que no hay nada que
        descargar.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3 border-b border-line pb-4">
        <div>
          <h1 className="text-[15px] font-semibold tracking-tight text-ink">
            Clips de rexxyconh
          </h1>
          <p className="mt-1 text-[12px] text-ink-dim">
            Verticales 1080×1920 con subtítulos, título y música ya montados. Se descargan
            tal cual salen del render, sin recomprimir.
          </p>
        </div>
        {clips.length > 0 ? (
          <Button onClick={() => void saveAll(clips)} disabled={bulk > 0}>
            {bulk > 0 ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Download className="h-3.5 w-3.5" aria-hidden />
            )}
            {bulk > 0
              ? `bajando… quedan ${bulk}`
              : `Descargar los ${clips.length} (${megas.toFixed(0)} MB)`}
          </Button>
        ) : null}
      </header>

      {loading && clips.length === 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="card h-24 animate-pulse bg-surface-2" />
          ))}
        </div>
      ) : null}

      {!loading && clips.length === 0 ? (
        <p className="card p-8 text-center text-[13px] text-ink-dim">
          Todavía no hay clips subidos.
        </p>
      ) : null}

      {streams.map((stream) => (
        <section key={stream.key} className="flex flex-col gap-2">
          <header className="flex flex-wrap items-center gap-2">
            <h2 className="text-[13px] font-medium text-ink">
              {streamDate(stream.date) || stream.title}
            </h2>
            <span className="tnum text-[11px] text-ink-faint">
              {stream.clips.length} clip{stream.clips.length > 1 ? "s" : ""}
            </span>
            {stream.clips.length > 1 ? (
              <button
                type="button"
                onClick={() => void saveAll(stream.clips)}
                disabled={bulk > 0}
                className="text-[11px] text-ink-faint underline-offset-2 hover:text-ink-dim hover:underline disabled:opacity-50"
              >
                descargar este directo
              </button>
            ) : null}
          </header>

          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {stream.clips.map((clip) => {
              const poster = clip.poster_path ? urls[clip.poster_path] : undefined;
              return (
                <li key={clip.id} className="card flex items-center gap-3 p-2.5">
                  {poster ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={poster}
                      alt=""
                      loading="lazy"
                      className="h-14 w-24 shrink-0 rounded object-cover"
                    />
                  ) : (
                    <div className="grid h-14 w-24 shrink-0 place-items-center rounded bg-surface-2">
                      <Film className="h-4 w-4 text-ink-faint" aria-hidden />
                    </div>
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="line-clamp-2 text-[12.5px] leading-snug text-ink">
                      {clip.title}
                    </p>
                    <p className="tnum mt-0.5 text-[11px] text-ink-faint">
                      {hhmmss(clip.duration_s)} · {(clip.size_bytes / 1048576).toFixed(1)} MB
                    </p>
                  </div>
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={() => save(clip)}
                    disabled={!urls[clip.storage_path]}
                    title={`Descargar ${clipFilename(clip)}`}
                  >
                    <Download className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}

      <p className="text-[11px] leading-relaxed text-ink-faint">
        Al pulsar «descargar todos» el navegador pide permiso para guardar varios ficheros:
        acéptalo y van cayendo solos. Los enlaces caducan cada 12 h; si alguno falla,
        recarga la página. Música de Kevin MacLeod (CC BY 3.0): acredítala al publicar.
      </p>
    </div>
  );
}

"use client";

import * as React from "react";
import { toast } from "sonner";
import { ClipGallery } from "@/components/ClipGallery";
import { QueueForm } from "@/components/QueueForm";
import { QueueList } from "@/components/QueueList";
import { isConfigured, listClips, listRequests, signClips, watchTable } from "@/lib/supabase";
import type { Clip, ClipRequest } from "@/lib/types";

/** Aviso cuando la pagina se ha construido sin claves: sin ellas no hay nada que mostrar. */
function MissingKeys() {
  return (
    <div className="card mx-auto mt-16 max-w-lg p-6">
      <h1 className="text-[15px] font-semibold text-ink">Faltan las claves de Supabase</h1>
      <p className="mt-2 text-[13px] text-ink-dim">
        Esta interfaz lee y escribe directamente en el proyecto de Supabase. Define estas
        dos variables antes de construirla:
      </p>
      <pre className="mt-3 overflow-x-auto rounded-md border border-line bg-surface-2 p-3 text-[12px] text-ink-dim">
        NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co{"\n"}
        NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ…
      </pre>
      <p className="mt-3 text-[12px] text-ink-faint">
        Las dos son públicas: la clave anon va en el JavaScript, y lo que puede hacer lo
        limitan las políticas RLS de <code>supabase/schema.sql</code>.
      </p>
    </div>
  );
}

export default function Page() {
  const [requests, setRequests] = React.useState<ClipRequest[]>([]);
  const [clips, setClips] = React.useState<Clip[]>([]);
  const [urls, setUrls] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(true);
  const [live, setLive] = React.useState(false);
  const configured = isConfigured();

  const refreshRequests = React.useCallback(async () => {
    try {
      setRequests(await listRequests());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo leer la cola");
    }
  }, []);

  const refreshClips = React.useCallback(async () => {
    try {
      const rows = await listClips();
      setClips(rows);
      // Las URLs firmadas caducan, asi que se piden con cada refresco de la lista. Los
      // mp4 y las portadas van en la misma tanda: son el mismo bucket y una sola llamada.
      const paths = rows.flatMap((c) => (c.poster_path ? [c.storage_path, c.poster_path] : [c.storage_path]));
      const signed = await signClips(paths);
      setUrls((prev) => ({ ...prev, ...signed }));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudieron leer los clips");
    }
  }, []);

  React.useEffect(() => {
    if (!configured) return;
    let alive = true;
    void (async () => {
      await Promise.all([refreshRequests(), refreshClips()]);
      if (alive) setLoading(false);
    })();
    const offRequests = watchTable("clip_requests", () => void refreshRequests(), setLive);
    const offClips = watchTable("clips", () => void refreshClips());
    return () => {
      alive = false;
      offRequests();
      offClips();
      setLive(false);
    };
  }, [configured, refreshRequests, refreshClips]);

  if (!configured) return <MissingKeys />;

  const running = requests.find((r) => r.status === "running" || r.status === "claimed");
  const waiting = requests.filter((r) => r.status === "queued").length;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-4">
        <div className="flex items-baseline gap-2">
          <span className="text-[15px] font-semibold tracking-tight text-ink">clipper</span>
          <span className="text-xs text-ink-faint">clips verticales de tus directos</span>
          {/* La pagina que se le pasa al streamer: los mismos clips, solo para bajar. */}
          <a
            href="/descargas/"
            className="text-xs text-ink-faint underline-offset-2 hover:text-ink-dim hover:underline"
          >
            descargas
          </a>
        </div>
        <p className="tnum flex items-center gap-2 text-[11px] text-ink-faint">
          <span
            className={`h-1.5 w-1.5 rounded-full ${live ? "bg-ok" : "bg-ink-faint"}`}
            aria-hidden
          />
          {live ? "en directo" : "sin conexión"}
          {running ? ` · analizando ${Math.round(running.progress * 100)}%` : ""}
          {waiting ? ` · ${waiting} en espera` : ""}
          {` · ${clips.length} clips`}
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[340px_minmax(0,1fr)]">
        <div className="flex flex-col gap-4 lg:sticky lg:top-5 lg:self-start">
          <QueueForm onQueued={refreshRequests} />
          <QueueList requests={requests} loading={loading} onChange={refreshRequests} />
          <p className="px-1 text-[11px] leading-relaxed text-ink-faint">
            Lo que guardas aquí se queda en la cola aunque el backend esté apagado. Cuando
            arranca, coge la más antigua, la analiza y sube los clips: aparecen abajo sin
            recargar.
          </p>
        </div>

        <div className="flex flex-col gap-4">
          <ClipGallery clips={clips} urls={urls} loading={loading} />
        </div>
      </div>
    </div>
  );
}

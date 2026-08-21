"use client";

import { Loader2, Plus } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { queueRequest } from "@/lib/supabase";
import { isSupportedUrl } from "@/lib/utils";

const MAX_CLIPS = 10;

/** Encolar un directo: enlace y cuantos clips quieres de el. */
export function QueueForm({ onQueued }: { onQueued: () => void }) {
  const [url, setUrl] = React.useState("");
  const [clips, setClips] = React.useState(3);
  const [saving, setSaving] = React.useState(false);

  const trimmed = url.trim();
  const looksOk = trimmed.length > 12 && isSupportedUrl(trimmed);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !looksOk) return;
    setSaving(true);
    try {
      await queueRequest(trimmed, clips);
      setUrl("");
      toast.success(`Guardado en la cola: ${clips} clip${clips > 1 ? "s" : ""}`);
      onQueued();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={submit} className="card flex flex-col gap-3 p-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="vod" className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Enlace del directo
        </label>
        <Input
          id="vod"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://www.twitch.tv/videos/2851488297"
          spellCheck={false}
          autoComplete="off"
          aria-invalid={trimmed.length > 12 && !looksOk}
        />
        {trimmed.length > 12 && !looksOk ? (
          <p className="text-[11px] text-warn">
            Tiene que ser un VOD de Twitch o un vídeo de YouTube.
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Cuántos clips
        </label>
        <div className="flex flex-wrap gap-1.5">
          {Array.from({ length: MAX_CLIPS }, (_, i) => i + 1).map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setClips(n)}
              aria-pressed={clips === n}
              className={`tnum h-8 w-8 rounded-md border text-[13px] transition-colors ${
                clips === n
                  ? "border-accent bg-accent/15 text-ink"
                  : "border-line text-ink-dim hover:bg-surface-2"
              }`}
            >
              {n}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-ink-faint">
          Los mejores {clips} momentos del directo, ya renderizados en vertical.
        </p>
      </div>

      <Button type="submit" size="lg" disabled={!looksOk || saving}>
        {saving ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        ) : (
          <Plus className="h-4 w-4" aria-hidden />
        )}
        Guardar en la cola
      </Button>
    </form>
  );
}

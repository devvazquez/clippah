"use client";

import { Loader2, Scissors } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { isSupportedUrl } from "@/lib/utils";

export function UrlForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const touched = url.trim().length > 0;
  const valid = touched && isSupportedUrl(url);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.createJob(url.trim());
      router.push(`/job/${res.job_id}`);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "No se pudo iniciar el análisis";
      setError(message);
      toast.error(message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setError(null);
          }}
          placeholder="https://twitch.tv/videos/..."
          spellCheck={false}
          autoComplete="off"
          aria-invalid={touched && !valid}
          className={touched && !valid ? "border-danger/50" : undefined}
          disabled={busy}
        />
        <Button type="submit" size="lg" disabled={!valid || busy} className="sm:w-36">
          {busy ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" /> Analizando
            </>
          ) : (
            <>
              <Scissors className="h-4 w-4" /> Analizar
            </>
          )}
        </Button>
      </div>
      <p className="min-h-[16px] text-xs">
        {error ? (
          <span className="text-danger">{error}</span>
        ) : touched && !valid ? (
          <span className="text-warn">
            Pega un VOD de Twitch (twitch.tv/videos/…) o un vídeo de YouTube.
          </span>
        ) : (
          <span className="text-ink-faint">
            Twitch: <code className="text-ink-dim">twitch.tv/videos/123456</code> · YouTube:{" "}
            <code className="text-ink-dim">youtube.com/watch?v=…</code>
          </span>
        )}
      </p>
    </form>
  );
}

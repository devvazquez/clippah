/**
 * Todo lo que la interfaz necesita de Supabase.
 *
 * Esta interfaz no habla con el backend: escribe en `clip_requests`, lee `clips` y pide
 * URLs firmadas a Storage. El backend de la sandbox mira esa misma cola. Asi la pagina es
 * un estatico que funciona desde cualquier sitio y no le hace falta que la sandbox este
 * encendida para encolar trabajo.
 */

import { type SupabaseClient, createClient } from "@supabase/supabase-js";
import type { Clip, ClipRequest, Cue, SfxCue } from "@/lib/types";

const URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";
export const BUCKET = process.env.NEXT_PUBLIC_SUPABASE_BUCKET ?? "clips";

/** Cuanto viven las URLs firmadas que se ponen en los <video> y en las descargas. */
const SIGNED_TTL_S = 60 * 60 * 12;

let client: SupabaseClient | null = null;

export function supabase(): SupabaseClient | null {
  if (!URL || !ANON) return null;
  if (!client) {
    client = createClient(URL, ANON, {
      auth: { persistSession: false },
      // Un evento por segundo sobra: lo que llega son cambios de progreso.
      realtime: { params: { eventsPerSecond: 2 } },
    });
  }
  return client;
}

export const isConfigured = () => Boolean(URL && ANON);

// --------------------------------------------------------------------- la cola

export async function queueRequest(url: string, clips: number): Promise<ClipRequest> {
  const sb = supabase();
  if (!sb) throw new Error("Falta configurar las claves de Supabase");
  const { data, error } = await sb
    .from("clip_requests")
    .insert({ url, clips, status: "queued" })
    .select()
    .single();
  if (error) throw new Error(error.message);
  return data as ClipRequest;
}

export async function cancelRequest(id: string): Promise<void> {
  const sb = supabase();
  if (!sb) return;
  // La politica solo deja cancelar lo que aun no ha empezado, asi que si el backend ya
  // la cogio esto no cambia nada (y no hace falta avisar de nada raro).
  const { error } = await sb
    .from("clip_requests")
    .update({ status: "canceled", message: "Cancelada" })
    .eq("id", id)
    .eq("status", "queued");
  if (error) throw new Error(error.message);
}

export async function listRequests(limit = 12): Promise<ClipRequest[]> {
  const sb = supabase();
  if (!sb) return [];
  const { data, error } = await sb
    .from("clip_requests")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error) throw new Error(error.message);
  return (data ?? []) as ClipRequest[];
}

// ---------------------------------------------------------------------- clips

export async function listClips(limit = 60): Promise<Clip[]> {
  const sb = supabase();
  if (!sb) return [];
  const { data, error } = await sb
    .from("clips")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error) throw new Error(error.message);
  return (data ?? []) as Clip[];
}

/** URLs firmadas para todos los clips de golpe: una peticion en vez de una por tarjeta. */
export async function signClips(paths: string[]): Promise<Record<string, string>> {
  const sb = supabase();
  if (!sb || paths.length === 0) return {};
  const { data, error } = await sb.storage.from(BUCKET).createSignedUrls(paths, SIGNED_TTL_S);
  if (error) throw new Error(error.message);
  const out: Record<string, string> = {};
  for (const row of data ?? []) {
    if (row.signedUrl && row.path) out[row.path] = row.signedUrl;
  }
  return out;
}

/**
 * La misma URL firmada, pero forzando la descarga con el nombre del clip.
 *
 * Storage respeta `?download=<nombre>` poniendo `Content-Disposition: attachment`, que es
 * lo que hace que el navegador guarde el mp4 en vez de abrirlo en una pestaña.
 */
export function downloadUrl(signed: string, filename: string): string {
  const sep = signed.includes("?") ? "&" : "?";
  return `${signed}${sep}download=${encodeURIComponent(filename)}`;
}

/**
 * Guarda los subtitulos corregidos y pide que se vuelva a quemar el clip.
 *
 * De un clip, la interfaz solo puede escribir estas dos columnas: los permisos de la
 * tabla no le dan el resto (ver `supabase/schema.sql`). El worker de la sandbox ve el
 * `rerender_queued`, rehace el mp4 y devuelve la fila a `ready`.
 */
export async function saveClipEdit(
  clipId: string,
  edit: { cues?: Cue[]; sfx?: SfxCue[]; music?: string },
): Promise<void> {
  const sb = supabase();
  if (!sb) throw new Error("Falta configurar las claves de Supabase");
  const patch: Record<string, unknown> = { render_status: "rerender_queued" };
  if (edit.cues) {
    const clean = edit.cues
      .map((c) => ({ text: c.text.trim(), start: c.start, end: c.end }))
      .filter((c) => c.text.length > 0);
    if (clean.length === 0) throw new Error("No queda ninguna frase con texto");
    patch.captions_edited = clean;
  }
  // Una lista vacia es una edicion valida ("ningun efecto"), asi que se compara con
  // undefined y no por si esta vacia.
  if (edit.sfx !== undefined) {
    patch.sfx_edited = edit.sfx.map((c) => ({
      name: c.name, t: Math.max(0, Number(c.t.toFixed(2))), gain_db: c.gain_db,
    }));
  }
  if (edit.music !== undefined) patch.music_edited = edit.music;
  const { error } = await sb.from("clips").update(patch).eq("id", clipId);
  if (error) throw new Error(error.message);
}

// -------------------------------------------------------------------- realtime

/**
 * Se suscribe a los cambios de una tabla y avisa. No intenta parchear la fila en local:
 * vuelve a pedir la lista, que son cuatro filas, y asi lo que se ve es siempre lo que hay.
 */
export function watchTable(
  table: "clip_requests" | "clips",
  onChange: () => void,
  onLive?: (live: boolean) => void,
): () => void {
  const sb = supabase();
  if (!sb) return () => {};
  const channel = sb
    .channel(`clipper:${table}`)
    .on("postgres_changes", { event: "*", schema: "public", table }, () => onChange())
    .subscribe((status) => {
      // Solo se dice "en directo" cuando el canal esta de verdad suscrito: si el
      // websocket se cae, la pagina tiene que reconocerlo en vez de mentir.
      onLive?.(status === "SUBSCRIBED");
      if (status === "SUBSCRIBED") onChange();
    });
  return () => {
    void sb.removeChannel(channel);
  };
}

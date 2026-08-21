/** Las dos tablas que comparten la interfaz y el backend (ver `supabase/schema.sql`). */

export type RequestStatus =
  | "queued"
  | "claimed"
  | "running"
  | "done"
  | "error"
  | "canceled";

export interface ClipRequest {
  id: string;
  url: string;
  clips: number;
  status: RequestStatus;
  stage: string;
  progress: number;
  message: string;
  error: string | null;
  video_title: string | null;
  video_url: string | null;
  duration_s: number | null;
  clips_done: number;
  job_id: string | null;
  worker: string | null;
  created_at: string;
  claimed_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export type RenderStatus = "ready" | "rerender_queued" | "rendering" | "error";

/** Una frase de subtitulo, en segundos desde el inicio del clip. */
export interface Cue {
  text: string;
  start: number;
  end: number;
}

export interface Clip {
  id: string;
  request_id: string | null;
  moment_id: string;
  rank: number;
  title: string;
  storage_path: string;
  size_bytes: number;
  duration_s: number;
  width: number;
  height: number;
  video_url: string | null;
  video_title: string | null;
  t_start: number | null;
  t_end: number | null;
  score: number | null;
  clip_score: number | null;
  source: string | null;
  category: string | null;
  sfx: string | null;
  music: string | null;
  transcript: string | null;
  reason: string | null;
  created_at: string;
  // Subtitulos: `captions` es lo que hay quemado; `captions_edited` lo que espera render.
  captions: Cue[];
  captions_edited: Cue[] | null;
  render_status: RenderStatus;
  render_error: string | null;
  version: number;
}

export const MUSIC_LABELS: Record<string, string> = {
  fluffing_a_duck: "Fluffing a Duck",
  sneaky_snitch: "Sneaky Snitch",
  sneaky_adventure: "Sneaky Adventure",
  ninguna: "",
};

export const SFX_LABELS: Record<string, string> = {
  golpe: "golpe seco",
  riser_golpe: "riser + golpe",
  ninguno: "",
};

/** Lo que esta pasando ahora mismo, en palabras, para cada etapa del pipeline. */
export const STAGE_LABELS: Record<string, string> = {
  queued: "En cola",
  ingest: "Leyendo el VOD",
  audio: "Extrayendo audio",
  chat: "Descargando chat",
  signals: "Midiendo señales",
  vision: "Mirando la pantalla",
  candidates: "Eligiendo candidatos",
  transcribe: "Transcribiendo",
  score: "Puntuando",
  frames: "Sacando fotogramas",
  render: "Renderizando",
  done: "Listo",
  error: "Error",
};

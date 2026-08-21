/** Tipos y clientes de la API. El frontend siempre habla con rutas relativas /api/*. */

export type Category =
  | "reaccion"
  | "gracioso"
  | "habilidad"
  | "fail"
  | "polemica"
  | "informativo"
  | "otro";

export type JobStatus = "queued" | "running" | "done" | "error" | "cancelled";

export type MomentSignals = {
  chat_z: number;
  audio_z: number;
  unique_users: number;
  msg_count: number;
  combo: boolean;
};

export type Moment = {
  id: string;
  video_id: string;
  t_start: number;
  t_end: number;
  t_peak: number;
  duration: number;
  title: string;
  description: string;
  category: Category;
  final_score: number;
  signals: MomentSignals;
  transcript: string;
  enriched: boolean;
  thumbnail_url: string;
  /** "signals" = lo encontró la reacción de audio/chat; "vision" = lo vio en pantalla. */
  source: "signals" | "vision";
  vision_note: string;
  /** Qué engancha en los primeros 2 s. Vacío = el clip arranca flojo. */
  hook: string;
};

export type Video = {
  id: string;
  platform: "twitch" | "youtube";
  ext_id: string;
  url: string;
  title: string;
  duration: number;
  uploader: string;
  upload_date: string;
  thumbnail: string;
};

export type Job = {
  id: string;
  url: string;
  status: JobStatus;
  stage: string;
  progress: number;
  message: string;
  error: string | null;
  chat_available: boolean;
  chat_messages: number;
  enriched: boolean;
  transcribed: boolean;
  warnings: string[];
  providers: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  video: Video | null;
  moments: Moment[];
};

export type JobListItem = {
  id: string;
  url: string;
  status: JobStatus;
  stage: string;
  progress: number;
  title: string;
  thumbnail: string;
  uploader: string;
  duration: number;
  moments: number;
  created_at: number;
};

export type ProviderHealth = {
  name: string;
  configured: boolean;
  available: boolean;
  requests_today: number;
  requests_limit: number | null;
  units_today: number;
  units_limit: number | null;
  note: string;
};

export type Health = {
  status: "ok";
  mode: "cloud" | "hybrid" | "local";
  ffmpeg: boolean;
  ytdlp: boolean;
  faster_whisper: boolean;
  transcriber: string;
  scorer: string;
  vision: boolean;
  providers: ProviderHealth[];
};

export type ProgressEvent = {
  stage: string;
  progress?: number;
  message?: string;
  moments?: number;
  seq?: number;
  ts?: number;
  video?: Partial<Video>;
};

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function detailToMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const msg = (detail as { message?: unknown }).message;
    if (typeof msg === "string") return msg;
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const text = await res.text();
  const body = text ? (JSON.parse(text) as unknown) : null;
  if (!res.ok) {
    const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : null;
    throw new ApiError(res.status, detailToMessage(detail, `Error ${res.status}`), detail);
  }
  return body as T;
}

export const api = {
  health: () => request<Health>("/api/health"),
  createJob: (url: string) =>
    request<{ job_id: string; video: Video | null }>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),
  listJobs: (limit = 12) =>
    request<{ items: JobListItem[]; total: number }>(`/api/jobs?limit=${limit}`),
  cancelJob: (id: string) => request<{ ok: boolean }>(`/api/jobs/${id}`, { method: "DELETE" }),
  render: (momentId: string) =>
    request<unknown>(`/api/moments/${momentId}/render`, { method: "POST" }),
};

/** URL del reproductor embebido, arrancando en `t_start`. */
export function embedUrl(video: Video, tStart: number): string {
  const t = Math.max(0, Math.floor(tStart));
  if (video.platform === "twitch") {
    const id = video.ext_id.replace(/^v/, "");
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = t % 60;
    const parent = typeof window === "undefined" ? "localhost" : window.location.hostname;
    return `https://player.twitch.tv/?video=v${id}&parent=${parent}&autoplay=true&time=${h}h${String(
      m,
    ).padStart(2, "0")}m${String(s).padStart(2, "0")}s`;
  }
  return `https://www.youtube.com/embed/${video.ext_id}?start=${t}&autoplay=1`;
}

/** Enlace al VOD en la plataforma, con el timestamp del momento. */
export function watchUrl(video: Video, tStart: number): string {
  const t = Math.max(0, Math.floor(tStart));
  if (video.platform === "twitch") {
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = t % 60;
    return `${video.url}?t=${h}h${String(m).padStart(2, "0")}m${String(s).padStart(2, "0")}s`;
  }
  return `https://www.youtube.com/watch?v=${video.ext_id}&t=${t}`;
}

export const CATEGORY_LABELS: Record<Category, string> = {
  reaccion: "reacción",
  gracioso: "gracioso",
  habilidad: "habilidad",
  fail: "fail",
  polemica: "polémica",
  informativo: "informativo",
  otro: "otro",
};

export const STAGE_LABELS: Record<string, string> = {
  queued: "En cola",
  ingest: "Ingesta",
  chat: "Chat",
  signals: "Señales",
  candidates: "Candidatos",
  transcribe: "Transcripción",
  score: "Puntuación",
  vision: "Visión",
  frames: "Fotogramas",
  done: "Listo",
  error: "Error",
  cancelled: "Cancelado",
  warning: "Aviso",
};

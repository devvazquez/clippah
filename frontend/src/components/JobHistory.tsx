"use client";

import { Clock, Film } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { type JobListItem, api } from "@/lib/api";
import { durationLabel, relativeDate } from "@/lib/utils";

const STATUS_TONE = {
  done: "ok",
  error: "danger",
  cancelled: "default",
  running: "accent",
  queued: "default",
} as const;

const STATUS_LABEL = {
  done: "listo",
  error: "error",
  cancelled: "cancelado",
  running: "analizando",
  queued: "en cola",
} as const;

export function JobHistory() {
  const [items, setItems] = useState<JobListItem[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listJobs(12)
        .then((r) => alive && setItems(r.items))
        .catch(() => alive && setItems([]));
    load();
    // Refresco suave: en `/` no hay SSE, solo la lista.
    const timer = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (items === null) {
    return (
      <div className="space-y-1.5">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <p className="rounded-md border border-dashed border-line px-3 py-6 text-center text-xs text-ink-faint">
        Todavía no has analizado ningún VOD.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line">
      {items.map((job) => (
        <li key={job.id}>
          <Link
            href={`/job/${job.id}`}
            className="flex items-center gap-3 bg-surface px-3 py-2.5 transition-colors hover:bg-surface-2"
          >
            <div className="h-10 w-[70px] shrink-0 overflow-hidden rounded bg-surface-2">
              {job.thumbnail ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={job.thumbnail}
                  alt=""
                  className="h-full w-full object-cover"
                  loading="lazy"
                />
              ) : null}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium text-ink">
                {job.title || job.url}
              </p>
              <p className="flex items-center gap-2 truncate text-[11px] text-ink-faint tnum">
                {job.uploader ? <span>{job.uploader}</span> : null}
                {job.duration ? <span>· {durationLabel(job.duration)}</span> : null}
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" /> {relativeDate(job.created_at)}
                </span>
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {job.moments > 0 ? (
                <span className="flex items-center gap-1 text-[11px] text-ink-dim tnum">
                  <Film className="h-3 w-3" />
                  {job.moments}
                </span>
              ) : null}
              <Badge tone={STATUS_TONE[job.status]}>
                {job.status === "running"
                  ? `${Math.round(job.progress * 100)}%`
                  : STATUS_LABEL[job.status]}
              </Badge>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}

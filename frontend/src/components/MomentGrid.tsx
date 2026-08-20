"use client";

import { useMemo, useState } from "react";
import { MomentCard } from "@/components/MomentCard";
import { PreviewDialog } from "@/components/PreviewDialog";
import { Button } from "@/components/ui/button";
import { CATEGORY_LABELS, type Category, type Moment, type Video } from "@/lib/api";
import { cn } from "@/lib/utils";

type Sort = "score" | "chrono";

export function MomentGrid({ moments, video }: { moments: Moment[]; video: Video | null }) {
  const [filter, setFilter] = useState<Category | "all">("all");
  const [sort, setSort] = useState<Sort>("score");
  const [preview, setPreview] = useState<Moment | null>(null);

  const categories = useMemo(() => {
    const counts = new Map<Category, number>();
    for (const m of moments) counts.set(m.category, (counts.get(m.category) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [moments]);

  const visible = useMemo(() => {
    const list = filter === "all" ? moments : moments.filter((m) => m.category === filter);
    return [...list].sort((a, b) =>
      sort === "score" ? b.final_score - a.final_score : a.t_start - b.t_start,
    );
  }, [moments, filter, sort]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip active={filter === "all"} onClick={() => setFilter("all")}>
            todos <span className="tnum text-ink-faint">{moments.length}</span>
          </Chip>
          {categories.map(([category, count]) => (
            <Chip
              key={category}
              active={filter === category}
              onClick={() => setFilter(category)}
            >
              {CATEGORY_LABELS[category]} <span className="tnum text-ink-faint">{count}</span>
            </Chip>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <span className="mr-1 text-[11px] text-ink-faint">orden</span>
          <Button
            variant={sort === "score" ? "subtle" : "ghost"}
            size="sm"
            onClick={() => setSort("score")}
          >
            score
          </Button>
          <Button
            variant={sort === "chrono" ? "subtle" : "ghost"}
            size="sm"
            onClick={() => setSort("chrono")}
          >
            cronológico
          </Button>
        </div>
      </div>

      {visible.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-8 text-center text-xs text-ink-faint">
          Ningún momento en esta categoría.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((moment) => (
            <MomentCard key={moment.id} moment={moment} onPreview={setPreview} />
          ))}
        </div>
      )}

      <PreviewDialog moment={preview} video={video} onClose={() => setPreview(null)} />
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors",
        active
          ? "border-accent/40 bg-accent/10 text-accent-soft"
          : "border-line bg-surface text-ink-dim hover:bg-surface-2 hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

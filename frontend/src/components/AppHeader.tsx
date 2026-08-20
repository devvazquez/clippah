import Link from "next/link";
import { ProviderStatus } from "@/components/ProviderStatus";

export function AppHeader() {
  return (
    <header className="mb-6 flex items-center justify-between gap-4 border-b border-line pb-4">
      <Link href="/" className="group flex items-baseline gap-2">
        <span className="text-[15px] font-semibold tracking-tight text-ink">clipper</span>
        <span className="text-xs text-ink-faint group-hover:text-ink-dim">
          momentos destacados de VODs
        </span>
      </Link>
      <ProviderStatus />
    </header>
  );
}

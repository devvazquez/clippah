import { AppHeader } from "@/components/AppHeader";
import { JobHistory } from "@/components/JobHistory";
import { UrlForm } from "@/components/UrlForm";

export default function HomePage() {
  return (
    <main>
      <AppHeader />
      <div className="mx-auto max-w-2xl">
        <h1 className="text-lg font-semibold tracking-tight text-ink">
          Encuentra los momentos de un directo
        </h1>
        <p className="mb-4 mt-1 text-[13px] text-ink-dim">
          Analiza el chat y el audio del VOD y devuelve los picos de reacción con su
          fotograma, su timestamp y una descripción de lo que pasa.
        </p>
        <UrlForm />

        <section className="mt-8">
          <h2 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-ink-faint">
            Análisis recientes
          </h2>
          <JobHistory />
        </section>
      </div>
    </main>
  );
}

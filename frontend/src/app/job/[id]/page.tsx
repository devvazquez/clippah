import { AppHeader } from "@/components/AppHeader";
import { JobView } from "@/components/JobView";

export default async function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main>
      <AppHeader />
      <JobView jobId={id} />
    </main>
  );
}

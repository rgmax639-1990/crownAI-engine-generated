import WorkspaceBar from "@/components/WorkspaceBar";
import { LoadingState } from "@/components/States";

// Shown instantly after "Generate Project", so navigation (and the URL change)
// happens right away instead of waiting for the project route to render.
export default function ProjectLoading() {
  return (
    <div>
      <WorkspaceBar subtitle="Loading project…" />
      <div className="container-page py-10">
        <LoadingState label="Opening your project…" rows={3} />
      </div>
    </div>
  );
}

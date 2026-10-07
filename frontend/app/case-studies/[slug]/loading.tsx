// Shown instantly when a case study link is clicked, so navigation (and the
// URL change) happens right away instead of waiting for the page render.
export default function CaseStudyLoading() {
  return (
    <div className="container-page section" aria-busy="true">
      <p role="status" className="body-muted">
        <span className="inline-flex items-center gap-2">
          <span className="spinner" aria-hidden="true" /> Loading case study…
        </span>
      </p>
      <div className="skeleton mt-4 h-8 w-2/3 max-w-md" />
      <div className="skeleton mt-4 h-4 w-full max-w-2xl" />
      <div className="skeleton mt-2 h-4 w-5/6 max-w-2xl" />
      <div className="mt-10 grid gap-6 lg:grid-cols-3">
        <div className="skeleton h-64 lg:col-span-2" />
        <div className="skeleton h-64" />
      </div>
    </div>
  );
}

import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="page-container flex min-h-[70vh] flex-col items-center justify-center py-16 text-center">
      <p className="text-sm font-semibold uppercase tracking-wide text-primary">404</p>
      <h1 className="mt-2 text-3xl font-bold text-fg">Page not found</h1>
      <p className="mt-2 max-w-md text-fg-muted">
        The page you are looking for does not exist or has moved. यह पृष्ठ उपलब्ध नहीं है।
      </p>
      <div className="mt-6 flex flex-wrap justify-center gap-3">
        <Link href="/en" className="inline-flex min-h-tap items-center rounded-md bg-primary px-5 font-semibold text-primary-fg hover:bg-primary-hover">
          Go to home
        </Link>
        <Link href="/hi" className="inline-flex min-h-tap items-center rounded-md border border-border bg-surface px-5 font-semibold text-fg hover:bg-surface-2">
          होम पेज
        </Link>
      </div>
    </main>
  );
}

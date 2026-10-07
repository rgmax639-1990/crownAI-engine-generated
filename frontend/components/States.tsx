import type { ReactNode } from "react";

// Shared loading / empty / error / alert building blocks so every data view
// presents those states the same way.

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="spinner" aria-hidden="true" />
      {label && <span>{label}</span>}
    </span>
  );
}

export function LoadingState({ label = "Loading…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="space-y-3">
      <p className="body-muted">
        <Spinner label={label} />
      </p>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-16 w-full" />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  message,
  action,
  icon = "♛",
}: {
  title: string;
  message: string;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-line-strong bg-subtle px-6 py-10 text-center">
      <span className="icon-tile mx-auto text-lg" aria-hidden="true">
        {icon}
      </span>
      <h3 className="card-title mt-4">{title}</h3>
      <p className="body-muted mx-auto mt-1 max-w-sm">{message}</p>
      {action && <div className="mt-5 flex justify-center">{action}</div>}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  retrying = false,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  return (
    <div role="alert" className="alert alert-error flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <p className="font-bold">{title}</p>
        <p>{message}</p>
      </div>
      {onRetry && (
        <button type="button" className="btn-secondary btn-sm shrink-0" onClick={onRetry} disabled={retrying}>
          {retrying ? "Retrying…" : "Try again"}
        </button>
      )}
    </div>
  );
}

export function Alert({
  kind,
  children,
  testId,
}: {
  kind: "error" | "success" | "warning" | "info";
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div role={kind === "error" ? "alert" : "status"} data-testid={testId} className={`alert alert-${kind}`}>
      <div>{children}</div>
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  intro,
  children,
  centered = false,
}: {
  eyebrow?: string;
  title: string;
  intro?: ReactNode;
  children?: ReactNode;
  centered?: boolean;
}) {
  return (
    <header className={`border-b border-line hero-glow ${centered ? "text-center" : ""}`}>
      <div className="container-page py-12 md:py-16">
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1 className={`page-title ${eyebrow ? "mt-4" : ""}`}>{title}</h1>
        {intro && <p className={`lead mt-4 max-w-2xl ${centered ? "mx-auto" : ""}`}>{intro}</p>}
        {children}
      </div>
    </header>
  );
}

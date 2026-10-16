"use client";

/**
 * The shared UI kit. Every page builds from these so buttons, cards, forms, tables and
 * states look and behave the same everywhere. All colours come from the design tokens in
 * app/globals.css (bg-surface, text-fg-muted, border-border, bg-primary ...).
 */
import Link from "next/link";
import {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  KeyboardEvent,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ApiError, ApiState } from "@/lib/api";
import { formatINR, formatLakhCrore, timeUntil } from "@/lib/format";
import type { Locale } from "@/lib/i18n";

export function cx(...c: Array<string | false | null | undefined>): string {
  return c.filter(Boolean).join(" ");
}

// ------------------------------------------------------------------ buttons
export type ButtonVariant = "primary" | "secondary" | "outline" | "ghost" | "danger" | "accent";
export type ButtonSize = "sm" | "md" | "lg";

const btnBase =
  "inline-flex items-center justify-center gap-2 rounded-md font-semibold transition-colors min-h-tap " +
  "disabled:cursor-not-allowed disabled:opacity-60 select-none text-center";
const btnSize: Record<ButtonSize, string> = {
  sm: "px-3 text-sm",
  md: "px-4 text-sm sm:text-base",
  lg: "px-6 text-base",
};
const btnVariant: Record<ButtonVariant, string> = {
  primary: "bg-primary text-primary-fg hover:bg-primary-hover shadow-sm",
  secondary: "bg-surface text-fg border border-border hover:bg-surface-2",
  outline: "border border-primary text-primary bg-transparent hover:bg-primary-soft",
  ghost: "text-fg bg-transparent hover:bg-surface-2",
  danger: "bg-danger text-danger-fg hover:bg-danger-hover shadow-sm",
  accent: "bg-accent text-accent-fg hover:opacity-90 shadow-sm",
};

export function buttonClass(variant: ButtonVariant = "primary", size: ButtonSize = "md", fullWidth = false, extra?: string) {
  return cx(btnBase, btnSize[size], btnVariant[variant], fullWidth && "w-full", extra);
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  fullWidth?: boolean;
};

/** Disabled only while `loading` (saving) or when explicitly disabled. Never moves on hover. */
export function Button({ variant = "primary", size = "md", loading = false, fullWidth, className, children, disabled, type, ...rest }: ButtonProps) {
  return (
    <button
      type={type ?? "button"}
      className={buttonClass(variant, size, fullWidth, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && <Spinner size="sm" />}
      {children}
    </button>
  );
}

export function LinkButton({
  href, variant = "primary", size = "md", fullWidth, className, children, ...rest
}: { href: string; variant?: ButtonVariant; size?: ButtonSize; fullWidth?: boolean; className?: string; children: ReactNode; target?: string; rel?: string; "aria-label"?: string }) {
  const external = /^https?:\/\//.test(href) || href.startsWith("tel:") || href.startsWith("mailto:");
  if (external) {
    return (
      <a href={href} className={buttonClass(variant, size, fullWidth, className)} {...rest}>
        {children}
      </a>
    );
  }
  return (
    <Link href={href} className={buttonClass(variant, size, fullWidth, className)} {...rest}>
      {children}
    </Link>
  );
}

/** Inline text link with accessible contrast and focus. */
export function TextLink({ href, children, className, external }: { href: string; children: ReactNode; className?: string; external?: boolean }) {
  const cls = cx("font-medium text-primary underline underline-offset-2 hover:no-underline", className);
  if (external || /^https?:\/\//.test(href)) {
    return (
      <a href={href} className={cls} target="_blank" rel="noopener noreferrer">
        {children}
        <span className="sr-only"> (opens in a new tab)</span>
      </a>
    );
  }
  return (
    <Link href={href} className={cls}>
      {children}
    </Link>
  );
}

// ------------------------------------------------------------------ layout
export function Card({ children, className, as = "div", padded = true, id }: { children: ReactNode; className?: string; as?: "div" | "section" | "article" | "aside"; padded?: boolean; id?: string }) {
  const Tag = as;
  return (
    <Tag id={id} className={cx("min-w-0 rounded-lg border border-border bg-surface shadow-sm", padded && "p-4 sm:p-5", className)}>
      {children}
    </Tag>
  );
}

export function CardHeader({ title, description, actions, level = 2, className }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; level?: 2 | 3; className?: string }) {
  const H = level === 2 ? "h2" : "h3";
  return (
    <div className={cx("mb-4 flex flex-wrap items-start justify-between gap-3", className)}>
      <div className="min-w-0">
        <H className={cx("font-semibold text-fg", level === 2 ? "text-lg" : "text-base")}>{title}</H>
        {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export type Crumb = { label: string; href?: string };

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-3 text-sm text-fg-muted">
      <ol className="flex flex-wrap items-center gap-1">
        {items.map((c, i) => (
          <li key={i} className="flex items-center gap-1">
            {i > 0 && <span aria-hidden="true">/</span>}
            {c.href && i < items.length - 1 ? (
              <Link href={c.href} className="rounded px-1 py-0.5 hover:text-fg hover:underline">
                {c.label}
              </Link>
            ) : (
              <span aria-current={i === items.length - 1 ? "page" : undefined} className="px-1 text-fg">
                {c.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

export function PageHeader({ title, description, actions, breadcrumbs, eyebrow }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; breadcrumbs?: Crumb[]; eyebrow?: ReactNode }) {
  return (
    <header className="mb-6">
      {breadcrumbs && <Breadcrumbs items={breadcrumbs} />}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          {eyebrow && <div className="mb-1 text-sm font-semibold uppercase tracking-wide text-primary">{eyebrow}</div>}
          <h1 className="text-2xl font-bold text-fg sm:text-3xl">{title}</h1>
          {description && <p className="mt-2 max-w-3xl text-fg-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}

// ------------------------------------------------------------------ feedback
export function Spinner({ size = "md", label }: { size?: "sm" | "md" | "lg"; label?: string }) {
  const s = size === "sm" ? "h-4 w-4 border-2" : size === "lg" ? "h-10 w-10 border-4" : "h-6 w-6 border-2";
  return (
    <span className="inline-flex items-center gap-2" role={label ? "status" : undefined}>
      <span aria-hidden="true" className={cx("inline-block animate-spin rounded-full border-current border-t-transparent", s)} />
      {label && <span className="text-sm text-fg-muted">{label}</span>}
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cx("animate-pulse rounded-md bg-surface-3", className)} />;
}

export function LoadingState({ label = "Loading…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" className="space-y-3 py-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={cx("h-12", i % 2 ? "w-11/12" : "w-full")} />
      ))}
    </div>
  );
}

export function EmptyState({ title, description, action, icon }: { title: string; description?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border-strong bg-surface-2 px-4 py-10 text-center">
      <div aria-hidden="true" className="mb-3 text-3xl text-fg-subtle">
        {icon ?? "○"}
      </div>
      <p className="font-semibold text-fg">{title}</p>
      {description && <p className="mt-1 max-w-md text-sm text-fg-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "We couldn't load this" }: { error: ApiError | Error | string | null; onRetry?: () => void; title?: string }) {
  const message = typeof error === "string" ? error : error?.message || "Please check your connection and try again.";
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-danger bg-danger-soft p-4 text-fg sm:flex-row sm:items-center sm:justify-between">
      <div>
        <p className="font-semibold">{title}</p>
        <p className="text-sm text-fg-muted">{message}</p>
      </div>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/** Renders loading / error-with-retry / empty / data for a `useApi` result. */
export function DataView<T>({
  state, children, isEmpty, empty, loadingLabel, loadingRows, errorTitle,
}: {
  state: Pick<ApiState<T>, "data" | "error" | "loading" | "reload">;
  children: (data: T) => ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  loadingLabel?: string;
  loadingRows?: number;
  errorTitle?: string;
}) {
  if (state.loading && state.data === null) return <LoadingState label={loadingLabel} rows={loadingRows} />;
  if (state.error && state.data === null) return <ErrorState error={state.error} onRetry={state.reload} title={errorTitle} />;
  if (state.data === null) return <LoadingState label={loadingLabel} rows={loadingRows} />;
  if (isEmpty && isEmpty(state.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  return <>{children(state.data)}</>;
}

export type Tone = "neutral" | "primary" | "success" | "warning" | "danger" | "info" | "accent" | "mock";

const toneBadge: Record<Tone, string> = {
  neutral: "bg-surface-3 text-fg",
  primary: "bg-primary-soft text-primary",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
  accent: "bg-accent-soft text-fg",
  mock: "bg-warning-soft text-warning border border-warning",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1 whitespace-nowrap rounded-pill px-2.5 py-0.5 text-xs font-semibold", toneBadge[tone], className)}>
      {children}
    </span>
  );
}

/** Visible label on any data that came from a mock provider. */
export function MockBadge({ label = "MOCK" }: { label?: string }) {
  return (
    <Badge tone="mock" className="uppercase tracking-wide">
      <span aria-hidden="true">⚑</span> {label}
    </Badge>
  );
}

const toneAlert: Record<"info" | "success" | "warning" | "danger", string> = {
  info: "border-info bg-info-soft",
  success: "border-success bg-success-soft",
  warning: "border-warning bg-warning-soft",
  danger: "border-danger bg-danger-soft",
};
const toneIcon = { info: "ℹ", success: "✓", warning: "!", danger: "✕" };

export function Alert({ tone = "info", title, children, action, className }: { tone?: "info" | "success" | "warning" | "danger"; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cx("flex gap-3 rounded-lg border-l-4 p-4 text-fg", toneAlert[tone], className)}>
      <span aria-hidden="true" className="mt-0.5 font-bold">
        {toneIcon[tone]}
      </span>
      <div className="min-w-0 flex-1">
        {title && <p className="font-semibold">{title}</p>}
        {children && <div className={cx("text-sm", title ? "mt-1 text-fg-muted" : "")}>{children}</div>}
        {action && <div className="mt-3">{action}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ form controls
const controlBase =
  "block w-full min-h-tap rounded-md border bg-surface px-3 py-2 text-base text-fg placeholder:text-fg-subtle " +
  "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-fg-muted read-only:bg-surface-2";

export function controlClass(invalid?: boolean, extra?: string) {
  return cx(controlBase, invalid ? "border-danger" : "border-border-strong", extra);
}

export function Input({ invalid, className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }) {
  return <input aria-invalid={invalid || undefined} className={controlClass(invalid, className)} {...rest} />;
}

export function Select({ invalid, className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }) {
  return (
    <select aria-invalid={invalid || undefined} className={controlClass(invalid, cx("appearance-none pr-9 bg-no-repeat", className))}
      style={{ backgroundImage: "linear-gradient(45deg, transparent 50%, currentColor 50%), linear-gradient(135deg, currentColor 50%, transparent 50%)", backgroundPosition: "calc(100% - 18px) 55%, calc(100% - 13px) 55%", backgroundSize: "5px 5px, 5px 5px" }}
      {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ invalid, className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }) {
  return <textarea aria-invalid={invalid || undefined} className={controlClass(invalid, cx("min-h-28", className))} {...rest} />;
}

/** Label + control + hint + inline error, wired with aria-describedby. `id` must match the control's id. */
export function Field({ id, label, hint, error, required, children, className }: { id: string; label: ReactNode; hint?: ReactNode; error?: string; required?: boolean; children: ReactNode; className?: string }) {
  return (
    <div className={cx("min-w-0", className)}>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium text-fg">
        {label}
        {required && <span className="text-danger" aria-hidden="true"> *</span>}
        {required && <span className="sr-only"> (required)</span>}
      </label>
      {children}
      {hint && !error && (
        <p id={`${id}-hint`} className="mt-1 text-xs text-fg-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="mt-1 text-sm font-medium text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "onChange" | "value" | "prefix"> & {
  id: string;
  label: ReactNode;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  hint?: ReactNode;
  prefix?: ReactNode;
  suffix?: ReactNode;
  className?: string;
};

export function TextField({ id, label, value, onChange, error, hint, required, prefix, suffix, className, ...rest }: TextFieldProps) {
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <Field id={id} label={label} hint={hint} error={error} required={required} className={className}>
      <div className="relative flex items-stretch">
        {prefix && (
          <span className="inline-flex items-center rounded-l-md border border-r-0 border-border-strong bg-surface-2 px-3 text-fg-muted">
            {prefix}
          </span>
        )}
        <Input
          id={id}
          name={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          invalid={!!error}
          required={required}
          aria-describedby={describedBy}
          className={cx(prefix ? "rounded-l-none" : "", suffix ? "rounded-r-none" : "")}
          {...rest}
        />
        {suffix && (
          <span className="inline-flex items-center rounded-r-md border border-l-0 border-border-strong bg-surface-2 px-3 text-fg-muted">
            {suffix}
          </span>
        )}
      </div>
    </Field>
  );
}

export type Option = { value: string; label: string; disabled?: boolean };

export function SelectField({ id, label, value, onChange, options, placeholder, error, hint, required, disabled, className }: {
  id: string; label: ReactNode; value: string; onChange: (v: string) => void; options: Option[]; placeholder?: string;
  error?: string; hint?: ReactNode; required?: boolean; disabled?: boolean; className?: string;
}) {
  return (
    <Field id={id} label={label} hint={hint} error={error} required={required} className={className}>
      <Select id={id} name={id} value={value} onChange={(e) => onChange(e.target.value)} invalid={!!error} disabled={disabled}
        required={required} aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}>
        {placeholder !== undefined && <option value="">{placeholder}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </Select>
    </Field>
  );
}

export function TextAreaField({ id, label, value, onChange, error, hint, required, rows = 4, maxLength, placeholder, className }: {
  id: string; label: ReactNode; value: string; onChange: (v: string) => void; error?: string; hint?: ReactNode; required?: boolean;
  rows?: number; maxLength?: number; placeholder?: string; className?: string;
}) {
  return (
    <Field id={id} label={label} hint={hint ?? (maxLength ? `${value.length}/${maxLength}` : undefined)} error={error} required={required} className={className}>
      <Textarea id={id} name={id} value={value} rows={rows} maxLength={maxLength} placeholder={placeholder} invalid={!!error}
        aria-describedby={error ? `${id}-error` : `${id}-hint`} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

export function Checkbox({ id, checked, onChange, label, description, error, disabled }: {
  id: string; checked: boolean; onChange: (v: boolean) => void; label: ReactNode; description?: ReactNode; error?: string; disabled?: boolean;
}) {
  return (
    <div>
      <div className="flex min-h-tap items-start gap-3">
        <input id={id} name={id} type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)}
          aria-invalid={!!error || undefined} aria-describedby={error ? `${id}-error` : description ? `${id}-desc` : undefined}
          className="mt-1 h-5 w-5 shrink-0 cursor-pointer rounded border-border-strong" />
        <label htmlFor={id} className="cursor-pointer text-sm text-fg">
          {label}
          {description && (
            <span id={`${id}-desc`} className="mt-0.5 block text-xs text-fg-muted">
              {description}
            </span>
          )}
        </label>
      </div>
      {error && (
        <p id={`${id}-error`} className="ml-8 mt-1 text-sm font-medium text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

/** Accessible on/off switch (role="switch"). */
export function Switch({ id, checked, onChange, label, description, disabled }: {
  id: string; checked: boolean; onChange: (v: boolean) => void; label: ReactNode; description?: ReactNode; disabled?: boolean;
}) {
  return (
    <div className="flex min-h-tap items-center justify-between gap-4">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-fg">
          {label}
        </label>
        {description && (
          <p id={`${id}-desc`} className="text-xs text-fg-muted">
            {description}
          </p>
        )}
      </div>
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled}
        aria-describedby={description ? `${id}-desc` : undefined} onClick={() => onChange(!checked)}
        className={cx("relative inline-flex h-7 w-12 shrink-0 items-center rounded-pill border-2 transition-colors disabled:opacity-50",
          checked ? "border-primary bg-primary" : "border-border-strong bg-surface-3")}>
        <span className="sr-only">{checked ? "On" : "Off"}</span>
        <span aria-hidden="true" className={cx("inline-block h-5 w-5 rounded-full bg-surface shadow-sm transition-transform",
          checked ? "translate-x-5" : "translate-x-0.5")} />
      </button>
    </div>
  );
}

export function RadioGroup({ name, legend, value, onChange, options, error, layout = "cards", hint }: {
  name: string; legend: ReactNode; value: string; onChange: (v: string) => void;
  options: Array<{ value: string; label: ReactNode; description?: ReactNode; disabled?: boolean }>; error?: string;
  layout?: "cards" | "list" | "inline"; hint?: ReactNode;
}) {
  return (
    <fieldset id={name} aria-describedby={error ? `${name}-error` : undefined} className="min-w-0">
      <legend className="mb-2 text-sm font-medium text-fg">{legend}</legend>
      {hint && <p className="-mt-1 mb-2 text-xs text-fg-muted">{hint}</p>}
      <div className={cx(layout === "cards" ? "grid gap-2 sm:grid-cols-2" : layout === "inline" ? "flex flex-wrap gap-2" : "space-y-2")}>
        {options.map((o) => {
          const id = `${name}-${o.value}`;
          const selected = value === o.value;
          return (
            <label key={o.value} htmlFor={id}
              className={cx("flex min-h-tap cursor-pointer items-start gap-3 rounded-md border p-3",
                selected ? "border-primary bg-primary-soft" : "border-border bg-surface hover:bg-surface-2",
                o.disabled && "cursor-not-allowed opacity-60")}>
              <input id={id} type="radio" name={name} value={o.value} checked={selected} disabled={o.disabled}
                onChange={() => onChange(o.value)} className="mt-1 h-4 w-4 shrink-0" />
              <span className="min-w-0">
                <span className="block text-sm font-medium text-fg">{o.label}</span>
                {o.description && <span className="block text-xs text-fg-muted">{o.description}</span>}
              </span>
            </label>
          );
        })}
      </div>
      {error && (
        <p id={`${name}-error`} className="mt-1 text-sm font-medium text-danger">
          {error}
        </p>
      )}
    </fieldset>
  );
}

// ------------------------------------------------------------------ tabs
export type TabDef = { id: string; label: ReactNode; badge?: ReactNode };

/** WAI-ARIA tabs with arrow-key navigation. Render panels with <TabPanel>. */
export function Tabs({ tabs, value, onChange, ariaLabel, idPrefix = "tab", className }: {
  tabs: TabDef[]; value: string; onChange: (id: string) => void; ariaLabel: string; idPrefix?: string; className?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
    if (e.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = tabs.length - 1;
    if (next >= 0) {
      e.preventDefault();
      onChange(tabs[next].id);
      refs.current[next]?.focus();
    }
  };
  return (
    <div role="tablist" aria-label={ariaLabel} className={cx("-mx-1 mb-4 flex gap-1 overflow-x-auto border-b border-border px-1", className)}>
      {tabs.map((tab, i) => {
        const selected = tab.id === value;
        return (
          <button key={tab.id} ref={(el) => { refs.current[i] = el; }} id={`${idPrefix}-${tab.id}`} role="tab" type="button"
            aria-selected={selected} aria-controls={`${idPrefix}-panel-${tab.id}`} tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)} onKeyDown={(e) => onKey(e, i)}
            className={cx("-mb-px inline-flex min-h-tap shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 text-sm font-semibold",
              selected ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg")}>
            {tab.label}
            {tab.badge}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ id, value, idPrefix = "tab", children }: { id: string; value: string; idPrefix?: string; children: ReactNode }) {
  if (id !== value) return null;
  return (
    <div role="tabpanel" id={`${idPrefix}-panel-${id}`} aria-labelledby={`${idPrefix}-${id}`} tabIndex={0} className="min-w-0 focus-visible:outline-offset-4">
      {children}
    </div>
  );
}

// ------------------------------------------------------------------ modal
/** Native <dialog>: focus is trapped, Esc closes, focus returns to the opener. */
export function Modal({ open, onClose, title, description, children, footer, size = "md" }: {
  open: boolean; onClose: () => void; title: ReactNode; description?: ReactNode; children: ReactNode; footer?: ReactNode; size?: "sm" | "md" | "lg";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    const handle = (e: Event) => {
      e.preventDefault();
      onClose();
    };
    d.addEventListener("cancel", handle);
    return () => d.removeEventListener("cancel", handle);
  }, [onClose]);
  const width = size === "sm" ? "sm:max-w-md" : size === "lg" ? "sm:max-w-3xl" : "sm:max-w-xl";
  return (
    <dialog ref={ref} aria-labelledby="modal-title"
      className={cx("m-auto w-[calc(100%-1.5rem)] max-h-[90vh] rounded-xl border border-border bg-surface p-0 text-fg shadow-lg backdrop:bg-overlay", width)}>
      {open && (
        <div className="flex max-h-[90vh] flex-col">
          <div className="flex items-start justify-between gap-4 border-b border-border p-4 sm:p-5">
            <div className="min-w-0">
              <h2 id="modal-title" className="text-lg font-semibold">
                {title}
              </h2>
              {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
            </div>
            <button type="button" onClick={onClose} aria-label="Close dialog"
              className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-xl text-fg-muted hover:bg-surface-2 hover:text-fg">
              ×
            </button>
          </div>
          <div className="overflow-y-auto p-4 sm:p-5">{children}</div>
          {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-border p-4">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

// ------------------------------------------------------------------ tables
export type Column<T> = {
  key: string;
  header: ReactNode;
  render?: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
};

/** Responsive table: scrolls horizontally inside its card on narrow screens. */
export function DataTable<T>({ columns, rows, rowKey, caption, onRowClick, selectedKey }: {
  columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; caption?: string; onRowClick?: (row: T) => void; selectedKey?: string | null;
}) {
  return (
    <div className="-mx-4 overflow-x-auto sm:mx-0">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-border bg-surface-2 text-left">
            {columns.map((c) => (
              <th key={c.key} scope="col" className={cx("px-4 py-3 font-semibold text-fg-muted", c.align === "right" && "text-right", c.align === "center" && "text-center", c.className)}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const k = rowKey(r);
            return (
              <tr key={k}
                className={cx("border-b border-border last:border-0", onRowClick && "cursor-pointer hover:bg-surface-2", selectedKey === k && "bg-primary-soft")}
                onClick={onRowClick ? () => onRowClick(r) : undefined}
                onKeyDown={onRowClick ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRowClick(r); } } : undefined}
                tabIndex={onRowClick ? 0 : undefined}
                aria-selected={onRowClick ? selectedKey === k : undefined}>
                {columns.map((c) => (
                  <td key={c.key} className={cx("px-4 py-3 align-top text-fg", c.align === "right" && "text-right tabular-nums", c.align === "center" && "text-center", c.className)}>
                    {c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? "—")}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ misc
export function Stepper({ steps, current, ariaLabel = "Progress" }: { steps: string[]; current: number; ariaLabel?: string }) {
  return (
    <nav aria-label={ariaLabel} className="mb-6">
      <p className="mb-2 text-sm text-fg-muted sm:hidden">
        Step {Math.min(current + 1, steps.length)} of {steps.length}: <span className="font-semibold text-fg">{steps[current]}</span>
      </p>
      <ol className="hidden gap-2 sm:flex">
        {steps.map((s, i) => {
          const state = i < current ? "done" : i === current ? "current" : "todo";
          return (
            <li key={s} aria-current={state === "current" ? "step" : undefined} className="flex min-w-0 flex-1 flex-col gap-1">
              <span className={cx("h-1.5 rounded-pill", state === "todo" ? "bg-surface-3" : "bg-primary")} />
              <span className={cx("truncate text-xs font-medium", state === "current" ? "text-primary" : state === "done" ? "text-fg" : "text-fg-muted")}>
                <span className="sr-only">{state === "done" ? "Completed: " : state === "current" ? "Current: " : "Not started: "}</span>
                {i + 1}. {s}
              </span>
            </li>
          );
        })}
      </ol>
      <div className="h-1.5 rounded-pill bg-surface-3 sm:hidden" aria-hidden="true">
        <div className="h-1.5 rounded-pill bg-primary" style={{ width: `${((current + 1) / steps.length) * 100}%` }} />
      </div>
    </nav>
  );
}

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button variant="ghost" size="sm" aria-label={`${label} ${value}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          setCopied(false);
        }
      }}>
      {copied ? "Copied" : label}
      <span className="sr-only" aria-live="polite">{copied ? "Copied to clipboard" : ""}</span>
    </Button>
  );
}

export function StatTile({ label, value, hint, tone = "neutral" }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className={cx("min-w-0 rounded-lg border border-border bg-surface p-4", tone !== "neutral" && "border-l-4",
      tone === "success" && "border-l-success", tone === "warning" && "border-l-warning", tone === "danger" && "border-l-danger",
      tone === "primary" && "border-l-primary", tone === "info" && "border-l-info", tone === "accent" && "border-l-accent")}>
      <p className="text-sm text-fg-muted">{label}</p>
      <p className="mt-1 truncate text-xl font-bold tabular-nums text-fg sm:text-2xl">{value}</p>
      {hint && <p className="mt-1 text-xs text-fg-muted">{hint}</p>}
    </div>
  );
}

/** Live countdown to an ISO timestamp (e.g. TAT, CERT-In 6-hour deadline). */
export function Countdown({ to, prefix, overdueTone = "danger" }: { to: string | null | undefined; prefix?: string; overdueTone?: Tone }) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!to) return <span>—</span>;
  if (now === null) return <span className="tabular-nums">…</span>;
  const r = timeUntil(to, now);
  return (
    <Badge tone={r.overdue ? overdueTone : r.ms < 3600000 ? "warning" : "info"}>
      <span className="tabular-nums">{prefix ? `${prefix} ` : ""}{r.label}</span>
    </Badge>
  );
}

export function Accordion({ items, defaultOpen }: { items: Array<{ id: string; title: ReactNode; content: ReactNode }>; defaultOpen?: string }) {
  return (
    <div className="divide-y divide-border rounded-lg border border-border bg-surface">
      {items.map((it) => (
        <details key={it.id} open={defaultOpen === it.id} className="group">
          <summary className="flex min-h-tap cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 font-medium text-fg hover:bg-surface-2">
            <span>{it.title}</span>
            <span aria-hidden="true" className="text-fg-muted transition-transform group-open:rotate-180">⌄</span>
          </summary>
          <div className="px-4 pb-4 text-sm text-fg-muted">{it.content}</div>
        </details>
      ))}
    </div>
  );
}

/** Money in paise. `compact` renders lakh/crore. */
export function Money({ paise, compact, locale = "en", className }: { paise: number | null | undefined; compact?: boolean; locale?: Locale; className?: string }) {
  return <span className={cx("tabular-nums", className)}>{compact ? formatLakhCrore(paise, locale) : formatINR(paise)}</span>;
}

export function KeyValue({ items, columns = 2 }: { items: Array<{ label: ReactNode; value: ReactNode }>; columns?: 1 | 2 | 3 }) {
  return (
    <dl className={cx("grid gap-x-6 gap-y-3", columns === 2 && "sm:grid-cols-2", columns === 3 && "sm:grid-cols-3")}>
      {items.map((it, i) => (
        <div key={i} className="min-w-0">
          <dt className="text-xs font-medium uppercase tracking-wide text-fg-muted">{it.label}</dt>
          <dd className="mt-0.5 break-words text-fg">{it.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function VisuallyHidden({ children }: { children: ReactNode }) {
  return <span className="sr-only">{children}</span>;
}

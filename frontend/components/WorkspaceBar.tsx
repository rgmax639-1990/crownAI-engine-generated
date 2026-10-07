import type { ReactNode } from "react";
import Logo from "./Logo";
import { BRAND_NAME, PRODUCT_NAME } from "@/lib/brand";

// Branded strip at the top of every Crown AI tool screen: the same Crownwright
// crown as the company site, on the tool's gold theme.
// The title is the page's bold <h1> on screens where the bar carries the only
// heading (the workspace list). Project pages pass headingTitle={false}
// because their own <h1> is the project name -- one <h1> per page either way.
export default function WorkspaceBar({
  subtitle,
  actions,
  headingTitle = true,
}: {
  subtitle?: ReactNode;
  actions?: ReactNode;
  headingTitle?: boolean;
}) {
  const title = (
    <>
      <span className="text-gold-gradient">{PRODUCT_NAME}</span> <span className="text-ink">Workspace</span>
    </>
  );
  return (
    <div className="border-b border-line bg-subtle">
      <div className="container-page flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <Logo variant="mark" height={44} decorative />
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wider text-muted">{BRAND_NAME}</p>
            {headingTitle ? (
              <h1 className="text-lg font-bold leading-tight">{title}</h1>
            ) : (
              <div className="text-lg font-bold leading-tight">{title}</div>
            )}
            {subtitle && <div className="mt-0.5 truncate text-sm text-muted">{subtitle}</div>}
          </div>
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

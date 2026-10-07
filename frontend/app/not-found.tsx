import Link from "next/link";
import Logo from "@/components/Logo";
import { pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Page not found") };

export default function NotFound() {
  return (
    <div className="hero-glow">
      <div className="container-page flex min-h-[60vh] flex-col items-center justify-center py-16 text-center">
        <Logo variant="mark" height={80} decorative />
        <p className="eyebrow mt-6">404</p>
        <h1 className="page-title mt-4">We couldn&apos;t find that page</h1>
        <p className="body-muted mt-3 max-w-md">The link may be out of date, or the page may have moved.</p>
        <div className="mt-6 flex flex-col gap-3 sm:flex-row">
          <Link href="/" className="btn-primary">
            Go to the home page
          </Link>
          <Link href="/contact" className="btn-secondary">
            Contact us
          </Link>
        </div>
      </div>
    </div>
  );
}

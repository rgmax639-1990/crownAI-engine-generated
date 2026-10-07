import Link from "next/link";
import Logo from "./Logo";
import { ADDRESS_LINES, BRAND_NAME, LEGAL_NAME, PRODUCT_NAME } from "@/lib/brand";

const COLUMNS = [
  {
    title: "Company",
    links: [
      { href: "/about", label: "About Us" },
      { href: "/team", label: "Team" },
      { href: "/careers", label: "Careers" },
      { href: "/case-studies", label: "Case Studies" },
      { href: "/blog", label: "Blog" },
    ],
  },
  {
    title: "Product",
    links: [
      { href: "/services", label: "Services" },
      { href: "/services/crown-ai", label: PRODUCT_NAME },
      { href: "/crown-ai", label: `${PRODUCT_NAME} Workspace` },
      { href: "/pricing", label: "Pricing" },
      { href: "/contact", label: "Contact" },
    ],
  },
  {
    title: "Legal",
    links: [
      { href: "/legal/privacy", label: "Privacy Policy" },
      { href: "/legal/terms", label: "Terms of Service" },
    ],
  },
];

export default function Footer() {
  return (
    <footer className="mt-auto border-t border-line bg-subtle">
      <div className="container-page grid gap-10 py-12 sm:grid-cols-2 lg:grid-cols-[1.4fr_1fr_1fr_1fr]">
        <div>
          <Link href="/" aria-label={`${BRAND_NAME} home`} className="inline-block rounded-md">
            <Logo height={52} decorative />
          </Link>
          <p className="body-muted mt-4 max-w-xs">
            {BRAND_NAME} is the software delivery brand of {LEGAL_NAME} — premium engineering services and{" "}
            {PRODUCT_NAME}, our SDLC/STLC automation tool.
          </p>
        </div>

        {COLUMNS.map((col) => (
          <nav key={col.title} aria-label={col.title}>
            <h2 className="text-sm font-bold uppercase tracking-wider text-ink">{col.title}</h2>
            <ul className="mt-4 space-y-2 text-sm">
              {col.links.map((l) => (
                <li key={l.href}>
                  <Link href={l.href} className="text-muted transition-colors hover:text-brand">
                    {l.label}
                  </Link>
                </li>
              ))}
            </ul>
            {col.title === "Legal" && (
              <address className="body-muted mt-5 not-italic">
                <span className="font-semibold text-ink">{LEGAL_NAME}</span>
                <br />
                {ADDRESS_LINES.map((line) => (
                  <span key={line}>
                    {line}
                    <br />
                  </span>
                ))}
              </address>
            )}
          </nav>
        ))}
      </div>
      <div className="border-t border-line">
        <p className="container-page py-5 text-center text-xs text-muted">
          © {new Date().getFullYear()} {LEGAL_NAME}. {BRAND_NAME} and {PRODUCT_NAME} are brands of {LEGAL_NAME}. All rights
          reserved.
        </p>
      </div>
    </footer>
  );
}

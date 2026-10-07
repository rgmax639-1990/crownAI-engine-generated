"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import Logo from "./Logo";
import ThemeToggle from "./ThemeToggle";
import { BRAND_NAME, PRODUCT_NAME } from "@/lib/brand";

const NAV = [
  { href: "/", label: "Home" },
  { href: "/about", label: "About Us" },
  { href: "/services", label: "Services" },
  { href: "/case-studies", label: "Case Studies" },
  { href: "/blog", label: "Blog" },
  { href: "/careers", label: "Careers" },
  { href: "/team", label: "Team" },
  { href: "/pricing", label: "Pricing" },
  { href: "/contact", label: "Contact" },
];

function isActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export default function Header() {
  const pathname = usePathname() || "/";
  const [open, setOpen] = useState(false);
  // The toggle stays disabled until hydration attaches its onClick, so an
  // early tap can't land on the inert server-rendered button.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);

  // Close the mobile menu after navigating.
  useEffect(() => setOpen(false), [pathname]);

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-canvas/90 backdrop-blur">
      <div className="container-page flex h-16 items-center justify-between gap-3 md:h-[4.5rem]">
        <Link href="/" aria-label={`${BRAND_NAME} home`} className="shrink-0 rounded-md">
          {/* Mark only on the narrowest phones; full logo from 400px up. */}
          <span className="block min-[400px]:hidden">
            <Logo variant="mark" height={38} decorative />
          </span>
          <span className="hidden min-[400px]:block">
            <Logo height={40} decorative />
          </span>
        </Link>

        <nav aria-label="Main" className="hidden items-center gap-1 xl:flex">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-md px-2.5 py-2 text-sm font-semibold transition-colors ${
                  active ? "bg-brand-soft text-brand-soft-ink" : "text-muted hover:bg-subtle-2 hover:text-ink"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          <Link href="/crown-ai" className="btn-primary hidden sm:inline-flex">
            Launch {PRODUCT_NAME}
          </Link>
          <button
            type="button"
            className="icon-btn xl:hidden"
            onClick={() => setOpen((o) => !o)}
            disabled={!hydrated}
            aria-label={open ? "Close menu" : "Open menu"}
            aria-expanded={open}
            aria-controls="mobile-nav"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
              {open ? (
                <path d="M6 6l12 12M18 6L6 18" strokeWidth="2" strokeLinecap="round" />
              ) : (
                <path d="M4 6h16M4 12h16M4 18h16" strokeWidth="2" strokeLinecap="round" />
              )}
            </svg>
          </button>
        </div>
      </div>

      {open && (
        <nav id="mobile-nav" aria-label="Main" className="border-t border-line bg-canvas xl:hidden">
          <ul className="container-page grid gap-1 py-3 sm:grid-cols-2">
            {NAV.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`block rounded-md px-3 py-2.5 text-sm font-semibold ${
                      active ? "bg-brand-soft text-brand-soft-ink" : "text-ink hover:bg-subtle-2"
                    }`}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
            <li className="sm:col-span-2">
              <Link href="/crown-ai" className="btn-primary mt-2 w-full">
                Launch {PRODUCT_NAME}
              </Link>
            </li>
          </ul>
        </nav>
      )}
    </header>
  );
}

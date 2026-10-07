import Link from "next/link";
import Logo from "@/components/Logo";
import { BRAND_NAME, BRAND_TAGLINE, LEGAL_NAME, PRODUCT_NAME } from "@/lib/brand";

const STAGES = [
  ["Requirements", "Turn a plain-language brief into a structured SRS."],
  ["Design", "Generate architecture and component design docs."],
  ["Code", "Produce a working scaffold for your requirements."],
  ["Test Cases", "STLC-aligned test cases, ready to execute."],
  ["NFR Testing", "Performance, security and reliability reports."],
];

const STATS = [
  ["5", "SDLC/STLC stages automated"],
  ["< 3s", "to first generation progress"],
  ["2", "sign-in options: Google & Microsoft"],
];

export default function HomePage() {
  return (
    <div>
      <section className="hero-glow border-b border-line">
        <div className="container-page grid items-center gap-10 py-14 md:py-20 lg:grid-cols-[1.15fr_1fr]">
          <div>
            <span className="eyebrow">Chennai, India · {BRAND_TAGLINE}</span>
            <h1 className="display-title mt-5">
              {BRAND_NAME} builds software.
              <br />
              <span className="text-gold-gradient">{PRODUCT_NAME} automates how you build it.</span>
            </h1>
            <p className="lead mt-6 max-w-xl">
              From requirements gathering to non-functional testing, {PRODUCT_NAME} is our flagship SDLC/STLC
              automation tool, turning a plain-language spec into requirements, design, code and test artifacts in
              minutes.
            </p>
            <div className="mt-8 flex flex-col gap-3 sm:flex-row">
              <Link href="/crown-ai" className="btn-primary btn-lg">
                Try {PRODUCT_NAME} free
              </Link>
              <Link href="/services/crown-ai" className="btn-secondary btn-lg">
                How it works
              </Link>
            </div>
          </div>

          <div className="card relative overflow-hidden p-8 text-center shadow-lg">
            <div className="flex justify-center">
              <Logo height={96} className="max-w-full h-auto" decorative />
            </div>
            <p className="body-muted mt-6">
              One identity for the company and the tool: {BRAND_NAME} by {LEGAL_NAME}, home of {PRODUCT_NAME}.
            </p>
            <dl className="mt-6 grid grid-cols-3 gap-3 border-t border-line pt-6">
              {STATS.map(([value, label]) => (
                <div key={label}>
                  <dt className="sr-only">{label}</dt>
                  <dd className="text-2xl font-bold text-brand">{value}</dd>
                  <dd className="mt-1 text-xs text-muted">{label}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container-page">
          <div className="text-center">
            <span className="eyebrow">{PRODUCT_NAME}</span>
            <h2 className="section-title mt-4">One tool, every SDLC/STLC stage</h2>
          </div>
          <ol className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-5">
            {STAGES.map(([title, desc], i) => (
              <li key={title} className="card card-interactive">
                <span className="icon-tile" aria-hidden="true">
                  {i + 1}
                </span>
                <h3 className="card-title mt-4">{title}</h3>
                <p className="body-muted mt-2">{desc}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="section section-subtle">
        <div className="container-page grid gap-6 lg:grid-cols-3">
          {[
            {
              title: `Why ${BRAND_NAME}`,
              body: "A Chennai-based engineering partner delivering production-grade software, digital transformation, and QA services to clients across industries.",
              href: "/about",
              cta: "About us",
            },
            {
              title: "Proven delivery",
              body: "Explore case studies from real engagements across web, cloud and QA automation projects.",
              href: "/case-studies",
              cta: "Case studies",
            },
            {
              title: "Start free, upgrade anytime",
              body: `Generate code with ${PRODUCT_NAME} on the free tier. Upgrade to unlock downloads and unlimited daily generations.`,
              href: "/pricing",
              cta: "View pricing",
            },
          ].map((c) => (
            <div key={c.title} className="card flex flex-col">
              <h3 className="card-title">{c.title}</h3>
              <p className="body-muted mt-2 flex-1">{c.body}</p>
              <Link href={c.href} className="link mt-4 text-sm">
                {c.cta} →
              </Link>
            </div>
          ))}
        </div>
      </section>

      <section className="section">
        <div className="container-page">
          <div className="card flex flex-col items-start gap-6 p-8 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="section-title">Ready to see {PRODUCT_NAME} on your own spec?</h2>
              <p className="body-muted mt-2">Sign in with Google or Microsoft. No card needed for the free tier.</p>
            </div>
            <Link href="/crown-ai" className="btn-primary btn-lg w-full md:w-auto">
              Open the workspace
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}

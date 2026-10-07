import Link from "next/link";
import Logo from "@/components/Logo";
import SignInOptions from "@/components/SignInOptions";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle(PRODUCT_NAME) };

const STAGES = [
  { name: "Requirements Gathering", desc: "Turns a plain-language brief into a structured SRS with functional and non-functional requirements." },
  { name: "Design", desc: "Produces architecture and component-level design documentation." },
  { name: "Coding", desc: "Generates a working code scaffold aligned to the design." },
  { name: "Testing (STLC)", desc: "Creates test cases covering the happy path and key edge cases." },
  { name: "Non-Functional Testing", desc: "Reports on performance, security, and reliability characteristics." },
];

export default function CrownAiServicePage() {
  return (
    <div>
      <section className="hero-glow border-b border-line">
        <div className="container-page py-14 text-center md:py-20">
          <div className="flex justify-center">
            <Logo variant="mark" height={88} decorative />
          </div>
          <span className="eyebrow mt-6">Flagship product by {BRAND_NAME}</span>
          <h1 className="display-title mt-4">
            <span className="text-gold-gradient">{PRODUCT_NAME}</span>
          </h1>
          <p className="lead mx-auto mt-4 max-w-2xl">
            Our SDLC/STLC automation tool. Sign in with Google or Microsoft to generate requirements, design, code and
            test artifacts for your own projects, independently, in your own workspace.
          </p>
          <div className="mt-8 flex justify-center">
            <SignInOptions
              label="Sign in & start free"
              triggerClassName="btn-primary btn-lg"
              optionsClassName="flex flex-col gap-3 sm:flex-row sm:justify-center"
              helperText="Choose a provider to sign in and start your free workspace:"
            />
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container-page">
          <h2 className="section-title">Every stage, automated</h2>
          <ol className="mt-8 grid gap-4 md:grid-cols-2">
            {STAGES.map((s, i) => (
              <li key={s.name} className={`card flex gap-4 ${i === STAGES.length - 1 ? "md:col-span-2" : ""}`}>
                <span className="icon-tile" aria-hidden="true">
                  {i + 1}
                </span>
                <div>
                  <h3 className="card-title">{s.name}</h3>
                  <p className="body-muted mt-1">{s.desc}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="section section-subtle">
        <div className="container-page text-center">
          <h2 className="section-title">Free to try, simple to upgrade</h2>
          <p className="body-muted mx-auto mt-3 max-w-2xl">
            The free tier lets you generate code and see every stage in action. Downloading generated code and advanced
            outputs requires a paid plan.
          </p>
          <Link href="/pricing" className="btn-secondary mt-6">
            See pricing
          </Link>
        </div>
      </section>
    </div>
  );
}

import Link from "next/link";
import { PageHeader } from "@/components/States";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Services") };

const SERVICES = [
  {
    title: `${PRODUCT_NAME} — SDLC/STLC Automation`,
    href: "/services/crown-ai",
    flagship: true,
    cta: `Explore ${PRODUCT_NAME}`,
    desc: "Our flagship product: generate requirements, design, code, and test artifacts from a plain-language brief.",
  },
  {
    title: "Custom Software Development",
    href: "/contact",
    cta: "Talk to us",
    desc: "End-to-end web and cloud application development for enterprise and startup clients.",
  },
  {
    title: "Quality Engineering & Test Automation",
    href: "/contact",
    cta: "Talk to us",
    desc: "Manual and automated testing, including performance and security non-functional testing.",
  },
  {
    title: "Digital Transformation Consulting",
    href: "/contact",
    cta: "Talk to us",
    desc: "Modernizing legacy systems and processes with cloud-native architectures.",
  },
];

export default function ServicesPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Services"
        title="What we do"
        intro={`From flagship product to hands-on delivery, here's how ${BRAND_NAME} helps engineering teams ship better software.`}
      />
      <section className="section">
        <div className="container-page grid gap-6 lg:grid-cols-2">
          {SERVICES.map((s) => (
            <Link
              key={s.title}
              href={s.href}
              className={`card card-interactive flex flex-col ${s.flagship ? "card-featured lg:col-span-2" : ""}`}
            >
              {s.flagship && <span className="badge badge-gold mb-3">♛ Flagship product</span>}
              <h2 className="card-title">{s.title}</h2>
              <p className="body-muted mt-2 flex-1">{s.desc}</p>
              <span className="mt-4 text-sm font-bold text-brand">{s.cta} →</span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}

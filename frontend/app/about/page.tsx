import { PageHeader } from "@/components/States";
import { ADDRESS_ONE_LINE, BRAND_NAME, LEGAL_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("About Us") };

export default function AboutPage() {
  return (
    <div>
      <PageHeader
        eyebrow="About us"
        title={`About ${BRAND_NAME}`}
        intro={`${BRAND_NAME} is the software delivery brand of ${LEGAL_NAME}, a Chennai-based software engineering company delivering full-lifecycle development, quality engineering, and digital transformation services to clients across India and abroad. Our flagship product, ${PRODUCT_NAME}, is built from the same disciplines we apply to every client engagement.`}
      />

      <section className="section">
        <div className="container-page grid gap-6 sm:grid-cols-3">
          {[
            [
              "Our mission",
              "Make high-quality software delivery faster and more predictable by automating the repetitive parts of the SDLC and STLC, so engineering teams can focus on judgment calls, not busywork.",
            ],
            [
              "How we work",
              "Discovery, design, build, test, and support. Every engagement follows a disciplined, transparent process with clear milestones and measurable quality gates.",
            ],
            ["Where we are", `Headquartered in ${ADDRESS_ONE_LINE}, with engineers serving clients across time zones.`],
          ].map(([title, body]) => (
            <div key={title} className="card">
              <h2 className="card-title text-brand">{title}</h2>
              <p className="body-muted mt-2">{body}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

import Link from "next/link";
import { EmptyState, PageHeader } from "@/components/States";
import { CASE_STUDIES } from "@/lib/caseStudies";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Case Studies") };

export default function CaseStudiesPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Case studies"
        title="Proof in delivery"
        intro={`A sample of engagements where ${BRAND_NAME} and ${PRODUCT_NAME} delivered measurable outcomes.`}
      />
      <section className="section">
        <div className="container-page">
          {CASE_STUDIES.length === 0 ? (
            <EmptyState title="Case studies coming soon" message="We're preparing our first published client stories." />
          ) : (
            <div className="grid gap-6 lg:grid-cols-3">
              {CASE_STUDIES.map((c) => (
                <article key={c.slug} className="card card-interactive flex flex-col">
                  <h2 className="card-title text-brand">{c.client}</h2>
                  <p className="body-muted mt-2 flex-1">{c.summary}</p>
                  <Link
                    href={`/case-studies/${c.slug}`}
                    className="link mt-4 text-sm"
                    aria-label={`View case study: ${c.client}`}
                  >
                    View case study →
                  </Link>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

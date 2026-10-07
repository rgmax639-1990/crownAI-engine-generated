import Link from "next/link";
import { notFound } from "next/navigation";
import { CASE_STUDIES, getCaseStudy } from "@/lib/caseStudies";
import { PRODUCT_NAME, pageTitle } from "@/lib/brand";

// Only the known case studies exist: they're all pre-rendered at build time
// (so a click never waits on an on-demand render) and any other slug is a 404.
export const dynamicParams = false;

export function generateStaticParams() {
  return CASE_STUDIES.map((c) => ({ slug: c.slug }));
}

// Next 15+ passes route params as a Promise; they must be awaited.
type PageProps = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: PageProps) {
  const { slug } = await params;
  const caseStudy = getCaseStudy(slug);
  return { title: pageTitle(caseStudy ? `${caseStudy.client} case study` : "Case Study") };
}

export default async function CaseStudyDetailPage({ params }: PageProps) {
  const { slug } = await params;
  const caseStudy = getCaseStudy(slug);
  if (!caseStudy) notFound();

  return (
    <div>
      <header className="hero-glow border-b border-line">
        <div className="container-page py-12 md:py-16">
          <Link href="/case-studies" className="link text-sm">
            ← Back to case studies
          </Link>
          <h1 className="page-title mt-4">{caseStudy.client}</h1>
          <p className="lead mt-4 max-w-2xl">{caseStudy.summary}</p>
        </div>
      </header>

      <section className="section">
        <div className="container-page grid gap-6 lg:grid-cols-3">
          <div className="card lg:col-span-2">
            <h2 className="card-title">The challenge</h2>
            <p className="body-muted mt-2">{caseStudy.challenge}</p>

            <h2 className="card-title mt-6">How {PRODUCT_NAME} helped</h2>
            <p className="body-muted mt-2">{caseStudy.approach}</p>

            <h2 className="card-title mt-6">Sample {PRODUCT_NAME} output</h2>
            <pre className="code-block mt-2">{caseStudy.sampleOutput}</pre>
          </div>

          <aside className="card h-fit">
            <h2 className="card-title">Results</h2>
            <ul className="mt-3 space-y-2 text-sm text-muted">
              {caseStudy.results.map((r) => (
                <li key={r} className="flex gap-2">
                  <span className="font-bold text-brand" aria-hidden="true">
                    ✓
                  </span>{" "}
                  {r}
                </li>
              ))}
            </ul>
            <Link href="/services/crown-ai" className="btn-secondary mt-6 w-full">
              See how {PRODUCT_NAME} works
            </Link>
          </aside>
        </div>
      </section>
    </div>
  );
}

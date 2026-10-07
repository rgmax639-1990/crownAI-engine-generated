import { EmptyState, PageHeader } from "@/components/States";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Blog") };

type Post = { title: string; date: string; tag: string; readMins: number; excerpt: string };

const POSTS: Post[] = [
  {
    title: "Why we automated the STLC, not just the SDLC",
    date: "2026-08-14",
    tag: "Engineering",
    readMins: 6,
    excerpt: `Most 'AI coding' tools stop at generating code. ${PRODUCT_NAME} carries the same rigor into test design and non-functional testing.`,
  },
  {
    title: `Free tier, real payments: designing ${PRODUCT_NAME}'s upgrade path`,
    date: "2026-07-02",
    tag: "Product",
    readMins: 5,
    excerpt: `How we designed a free tier that lets you evaluate ${PRODUCT_NAME} fully, while keeping downloads and advanced output behind a live Stripe checkout.`,
  },
  {
    title: "Building for India's DPDP Act and GDPR at the same time",
    date: "2026-05-20",
    tag: "Compliance",
    readMins: 8,
    excerpt:
      "Geo-aware consent isn't a checkbox: it changes what data you collect, how long you keep it, and what rights you expose to users.",
  },
];

function formatDate(iso: string) {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

export default function BlogPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Blog"
        title={`The ${BRAND_NAME} blog`}
        intro={`Engineering notes from the team building ${PRODUCT_NAME} and delivering client software.`}
      />
      <section className="section">
        <div className="container-page">
          {POSTS.length === 0 ? (
            <EmptyState title="No articles yet" message="We're writing our first posts. Check back soon." />
          ) : (
            <div className="grid gap-6 lg:grid-cols-3">
              {POSTS.map((post) => (
                <article key={post.title} className="card card-interactive flex flex-col">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span className="badge">{post.tag}</span>
                    <time dateTime={post.date}>{formatDate(post.date)}</time>
                    <span aria-hidden="true">·</span>
                    <span>{post.readMins} min read</span>
                  </div>
                  <h2 className="card-title mt-3">{post.title}</h2>
                  <p className="body-muted mt-2 flex-1">{post.excerpt}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

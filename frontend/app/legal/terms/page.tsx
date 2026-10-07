import { PageHeader } from "@/components/States";
import { ADDRESS_ONE_LINE, BRAND_NAME, LEGAL_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Terms of Service") };

const SECTIONS = [
  {
    title: "1. Accounts",
    body: `${PRODUCT_NAME} accounts are created via Google or Microsoft sign-in only. You are responsible for maintaining the security of the account you sign in with.`,
  },
  {
    title: "2. Free and paid tiers",
    body: "The free tier allows artifact generation subject to a daily usage cap and does not include downloads of generated code or advanced outputs. Paid plans are billed through Stripe upon completion of a lead-capture form and live checkout, and take effect immediately on successful payment.",
  },
  {
    title: "3. Acceptable use",
    body: `You may not use ${PRODUCT_NAME} to generate content that is unlawful, infringing, or intended to bypass usage caps through multiple accounts or automated abuse.`,
  },
  {
    title: "4. Termination",
    body: "You may delete your projects and data at any time. We may suspend accounts that violate these terms.",
  },
];

export default function TermsPage() {
  return (
    <div>
      <PageHeader eyebrow="Legal" title="Terms of Service" intro="Last updated: 26 September 2026" />
      <article className="container-page section max-w-3xl">
        <div className="card space-y-6 text-sm leading-7 text-muted">
          <p>
            These terms govern your use of the {BRAND_NAME} website and the {PRODUCT_NAME} tool, operated by {LEGAL_NAME}{" "}
            from {ADDRESS_ONE_LINE}.
          </p>
          {SECTIONS.map((s) => (
            <section key={s.title}>
              <h2 className="card-title">{s.title}</h2>
              <p className="mt-1">{s.body}</p>
            </section>
          ))}
        </div>
      </article>
    </div>
  );
}

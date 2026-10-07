import { PageHeader } from "@/components/States";
import { ADDRESS_ONE_LINE, BRAND_NAME, LEGAL_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Privacy Policy") };

const SECTIONS = [
  {
    title: "1. What we collect",
    body: `Account details from your chosen sign-in provider (Google or Microsoft), ${PRODUCT_NAME} project inputs and generated artifacts, contact/lead-capture form submissions, and payment metadata (never raw card numbers: those are handled entirely by our PCI-DSS compliant payment processor, Stripe).`,
  },
  {
    title: "2. Jurisdiction-specific rights",
    body: "Depending on your location, additional rights apply: the Digital Personal Data Protection Act, 2023 for users in India, the GDPR for users in the EU/EEA, and the CCPA (and similar state laws) for users in the United States. The consent banner shown on this site adapts automatically based on your detected region.",
  },
  {
    title: `3. Your ${PRODUCT_NAME} data`,
    body: `You can view and permanently delete your own ${PRODUCT_NAME} projects and their generated artifacts at any time from your workspace, or use "Delete my account" there to erase your account and all of its data in one step. Deletion is immediate and irreversible; payment records required for statutory accounting are retained without being linked to a deleted account.`,
  },
  {
    title: "4. Contact",
    body: `For data rights requests, contact us via the Contact page or write to our registered office at ${ADDRESS_ONE_LINE}.`,
  },
];

export default function PrivacyPage() {
  return (
    <div>
      <PageHeader eyebrow="Legal" title="Privacy Policy" intro="Last updated: 26 September 2026" />
      <article className="container-page section max-w-3xl">
        <div className="card space-y-6 text-sm leading-7 text-muted">
          <p>
            {LEGAL_NAME} (&quot;we&quot;, &quot;us&quot;), operating as {BRAND_NAME} and registered at {ADDRESS_ONE_LINE}, is
            the data controller for personal data collected through this website and the {PRODUCT_NAME} tool.
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

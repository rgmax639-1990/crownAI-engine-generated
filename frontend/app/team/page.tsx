import { PageHeader } from "@/components/States";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Team") };

const TEAM = [
  { name: "Ishanvi Ramanathan", role: "Founder & CEO", focus: "Company strategy & client partnerships" },
  { name: "Arun Kumar", role: "VP Engineering", focus: `${PRODUCT_NAME} platform architecture` },
  { name: "Priya Venkatesan", role: "Head of QA", focus: "STLC automation & non-functional testing" },
  { name: "Karthik Subramaniam", role: "Head of Delivery", focus: "Client engagements & program management" },
];

export default function TeamPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Team"
        title={`The people behind ${BRAND_NAME}`}
        intro={`A senior team of engineers, QA leads, and delivery managers based in Chennai, building both client software and the ${PRODUCT_NAME} product.`}
      />
      <section className="section">
        <ul className="container-page grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {TEAM.map((member) => (
            <li key={member.name} className="card text-center">
              <div
                className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-gold-gradient text-lg font-bold text-on-gold shadow-gold"
                aria-hidden="true"
              >
                {member.name
                  .split(" ")
                  .map((n) => n[0])
                  .join("")}
              </div>
              <h2 className="card-title mt-4">{member.name}</h2>
              <p className="text-sm font-semibold text-brand">{member.role}</p>
              <p className="body-muted mt-2">{member.focus}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

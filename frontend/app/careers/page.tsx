import Link from "next/link";
import { EmptyState, PageHeader } from "@/components/States";
import { BRAND_NAME, PRODUCT_NAME, pageTitle } from "@/lib/brand";

export const metadata = { title: pageTitle("Careers") };

const OPENINGS = [
  { title: "Senior Full-Stack Engineer", team: "Engineering", location: "Chennai / Remote", type: "Full-time" },
  { title: "QA Automation Engineer (STLC)", team: "Quality", location: "Chennai", type: "Full-time" },
  { title: `AI/ML Engineer — ${PRODUCT_NAME}`, team: PRODUCT_NAME, location: "Chennai / Remote", type: "Full-time" },
  { title: "DevOps Engineer", team: "Platform", location: "Chennai", type: "Full-time" },
  { title: "Product Designer", team: "Design", location: "Remote", type: "Contract" },
];

export default function CareersPage() {
  return (
    <div>
      <PageHeader
        eyebrow="Careers"
        title={`Build with ${BRAND_NAME}`}
        intro={`Join the team building ${PRODUCT_NAME} and delivering SDLC/STLC engineering for clients worldwide, from our Chennai headquarters.`}
      />
      <section className="section">
        <div className="container-page">
          <h2 className="section-title">Open positions ({OPENINGS.length})</h2>
          {OPENINGS.length === 0 ? (
            <div className="mt-6">
              <EmptyState
                title="No open roles right now"
                message="We're not hiring for specific roles at the moment, but we'd still love to hear from you."
                action={
                  <Link href="/contact" className="btn-primary">
                    Send us your profile
                  </Link>
                }
              />
            </div>
          ) : (
            <div className="table-wrap mt-6">
              <table className="table">
                <caption className="sr-only">Open positions at {BRAND_NAME}</caption>
                <thead>
                  <tr>
                    <th scope="col">Role</th>
                    <th scope="col" className="hidden sm:table-cell">
                      Team
                    </th>
                    <th scope="col" className="hidden md:table-cell">
                      Location
                    </th>
                    <th scope="col" className="hidden md:table-cell">
                      Type
                    </th>
                    <th scope="col">
                      <span className="sr-only">Apply</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {OPENINGS.map((job) => (
                    <tr key={job.title}>
                      <td>
                        <p className="font-semibold text-ink">{job.title}</p>
                        <p className="text-xs text-muted md:hidden">
                          {job.location} · {job.type}
                        </p>
                      </td>
                      <td className="hidden text-muted sm:table-cell">{job.team}</td>
                      <td className="hidden text-muted md:table-cell">{job.location}</td>
                      <td className="hidden md:table-cell">
                        <span className={job.type === "Full-time" ? "badge" : "badge badge-neutral"}>{job.type}</span>
                      </td>
                      <td className="text-right">
                        <Link
                          href="/contact"
                          className="btn-secondary btn-sm"
                          aria-label={`Apply for ${job.title}`}
                        >
                          Apply
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export type CaseStudy = {
  slug: string;
  client: string;
  summary: string;
  challenge: string;
  approach: string;
  results: string[];
  sampleOutput: string;
};

export const CASE_STUDIES: CaseStudy[] = [
  {
    slug: "regional-nbfc-chennai",
    client: "Regional NBFC (Chennai)",
    summary:
      "Rebuilt a loan-origination workflow, cutting manual QA cycle time by 40% using Crown AI-generated test suites.",
    challenge:
      "A Chennai-based non-banking financial company was manually writing test cases for every release of its loan-origination system, slowing releases and missing edge cases in KYC and credit-check flows.",
    approach:
      "Crownwright Technologies used Crown AI to generate structured requirements from the client's process documents, then produced STLC-aligned test cases (happy path, boundary, and negative scenarios) and non-functional test reports for every sprint.",
    results: [
      "40% reduction in manual QA cycle time",
      "Test coverage extended to previously untested edge cases (partial KYC, concurrent applications)",
      "Release cadence moved from monthly to bi-weekly",
    ],
    sampleOutput:
      "## TC-14: Reject application with expired KYC document\nSteps: submit loan application with a KYC document past its validity date -> Expected: application rejected with a clear \"KYC expired\" error, no partial record persisted.",
  },
  {
    slug: "saas-logistics-bengaluru",
    client: "SaaS logistics platform (Bengaluru)",
    summary:
      "Used Crown AI to generate an SRS and design docs from customer interviews in under a day, kicking off a 6-week MVP build.",
    challenge:
      "A Bengaluru logistics SaaS startup had raw customer-interview notes but no formal requirements or design documentation, delaying the start of development.",
    approach:
      "Crown AI turned the interview notes into a structured Software Requirements Specification and system design (architecture, components, key decisions) within a single working day, which the engineering team used directly to scope the MVP build.",
    results: [
      "SRS and design documentation produced in under a day (previously a multi-week effort)",
      "6-week MVP build started immediately, scoped directly from the generated design",
      "Reduced back-and-forth with stakeholders over requirements ambiguity",
    ],
    sampleOutput:
      "## Architecture\n- Client (web) <-> API (stateless service) <-> Persistent store\n\n## Key Design Decisions\n- Layered architecture chosen for testability and independent scaling.",
  },
  {
    slug: "eu-retail-gdpr",
    client: "European retail client (GDPR scope)",
    summary:
      "Delivered a GDPR-compliant customer portal with automated non-functional test reporting for performance and security sign-off.",
    challenge:
      "An EU-based retail client needed a customer-facing portal that could pass a formal GDPR and performance/security review before launch, with limited internal QA capacity.",
    approach:
      "Crown AI generated non-functional test reports covering performance (response-time targets), security (authN/authZ, input validation, secrets handling) and reliability (error/empty/loading states) alongside the functional test suite, giving the compliance team a ready-made audit trail.",
    results: [
      "Passed GDPR and security sign-off on the first review cycle",
      "Automated non-functional test reports replaced a manual audit checklist",
      "p95 response times verified under 2.5s ahead of launch",
    ],
    sampleOutput:
      "## Security\n- Auth required on all mutating endpoints -- PASS\n- Input validation on all fields -- PASS\n- No secrets in logs or responses -- PASS",
  },
];

export function getCaseStudy(slug: string): CaseStudy | undefined {
  return CASE_STUDIES.find((c) => c.slug === slug);
}

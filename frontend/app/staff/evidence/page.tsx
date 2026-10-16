"use client";

import { FormEvent, useState } from "react";
import {
  Alert, Badge, Button, Card, CardHeader, DataTable, DataView, EmptyState, Field, Input, PageHeader, SelectField, StatTile,
  TextAreaField, TextField, Tone,
} from "@/components/ui";
import { api, ApiError, downloadFile, uploadFile, useApi } from "@/lib/api";
import { formatDateTime, formatNumber, humanize } from "@/lib/format";

type Metrics = Record<string, unknown>;

type EvidenceRecord = {
  id: string;
  evidence_type: string;
  srs_ids: string[];
  environment: string;
  executed_at: string;
  result: "passed" | "failed" | "partial";
  metrics: Metrics;
  artifact_file_name: string | null;
  artifact_sha256: string | null;
  has_artifact: boolean;
  recorded_by_name: string | null;
  created_at: string;
};

type MatrixItem = {
  key: string;
  srs_id: string | null;
  evidence_type: string;
  title: string;
  requirement: string;
  metrics_hint: string[];
  evidenced: boolean;
  status: string;
  result: string | null;
  record_id: string | null;
  executed_at: string | null;
  environment: string | null;
  metrics: Metrics;
  target_met: boolean | null;
};

type EvidenceResponse = {
  records: EvidenceRecord[];
  matrix: MatrixItem[];
  evidenced_count: number;
  expected_count: number;
  note: string;
  evidence_types: string[];
  environments: string[];
  results: string[];
};

const RESULT_TONE: Record<string, Tone> = { passed: "success", failed: "danger", partial: "warning" };
const METRIC_FIELDS: Array<{ key: string; label: string; suffix?: string; types: string[] }> = [
  { key: "rps", label: "Throughput", suffix: "req/s", types: ["load_test"] },
  { key: "p95_ms", label: "p95 latency", suffix: "ms", types: ["load_test"] },
  { key: "rto_minutes", label: "RTO achieved", suffix: "min", types: ["dr_drill"] },
  { key: "rpo_minutes", label: "RPO achieved", suffix: "min", types: ["dr_drill"] },
  { key: "critical_findings", label: "Open critical findings", types: ["vapt"] },
  { key: "high_findings", label: "Open high findings", types: ["vapt"] },
  { key: "coverage_percent", label: "Code coverage", suffix: "%", types: ["test_run"] },
];

function nowIstLocal(): string {
  return new Date(Date.now() + 330 * 60000).toISOString().slice(0, 16);
}

function metricNum(m: Metrics, k: string): string | null {
  const v = m?.[k];
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? formatNumber(n, Number.isInteger(n) ? 0 : 1) : String(v);
}

function KeyMetrics({ m }: { m: Metrics }) {
  const parts = METRIC_FIELDS.map((f) => {
    const v = metricNum(m, f.key);
    return v === null ? null : `${f.label.replace(" achieved", "")}: ${v}${f.suffix ? ` ${f.suffix}` : ""}`;
  }).filter(Boolean) as string[];
  if (!parts.length) return <span className="text-fg-muted">—</span>;
  return (
    <ul className="space-y-0.5 text-xs">
      {parts.map((p) => <li key={p} className="whitespace-nowrap tabular-nums">{p}</li>)}
    </ul>
  );
}

export default function EvidencePage() {
  const [srsDraft, setSrsDraft] = useState("");
  const [srs, setSrs] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [dlError, setDlError] = useState<string | null>(null);
  const state = useApi<EvidenceResponse>(`/admin/evidence${srs ? `?srs_id=${encodeURIComponent(srs)}` : ""}`);

  const download = async (r: EvidenceRecord) => {
    setDlError(null);
    try {
      await downloadFile(`/admin/evidence/${r.id}/artifact`, r.artifact_file_name || `evidence-${r.id}`);
    } catch (e) {
      setDlError(e instanceof ApiError ? e.message : "Download failed. Please try again.");
    }
  };

  return (
    <>
      <PageHeader title="Evidence register" eyebrow="Traceability"
        description="Executed load tests, DR drills, VAPT, accessibility audits, test runs and architecture reviews, traced to SRS requirements. A requirement is only shown as evidenced when a run has been recorded with its raw report attached."
        actions={!showForm ? <Button onClick={() => setShowForm(true)}>Record evidence</Button> : undefined} />

      <Alert tone="info" className="mb-4" title="Engineering targets">
        Figures such as 15,000 req/s at p95 under 500 ms, RTO 2 hours / RPO 15 minutes and 80% coverage are engineering targets until the bank approves them. Nothing is marked as evidenced without a recorded run.
      </Alert>
      {notice && <Alert tone="success" className="mb-4" action={<Button size="sm" variant="ghost" onClick={() => setNotice(null)}>Dismiss</Button>}>{notice}</Alert>}
      {dlError && <Alert tone="danger" className="mb-4">{dlError}</Alert>}

      {showForm && state.data && (
        <EvidenceForm data={state.data} onCancel={() => setShowForm(false)}
          onCreated={(r) => { setShowForm(false); setNotice(`Evidence recorded for ${r.srs_ids.join(", ")} (${humanize(r.evidence_type)}).`); state.reload(); }} />
      )}

      <Card className="mb-4">
        <form className="flex flex-wrap items-end gap-3" aria-label="Filter by SRS ID"
          onSubmit={(e) => { e.preventDefault(); setSrs(srsDraft.trim().toUpperCase()); }}>
          <TextField id="srs_filter" label="SRS ID" value={srsDraft} onChange={setSrsDraft} placeholder="e.g. US-28"
            error={state.error?.fieldErrors?.srs_id} className="w-48" />
          <Button type="submit">Filter</Button>
          {srs && <Button variant="secondary" onClick={() => { setSrs(""); setSrsDraft(""); }}>Clear</Button>}
        </form>
      </Card>

      <DataView state={state} loadingRows={6}>
        {(d) => (
          <div className="space-y-6">
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile label="Expected evidence items" value={d.expected_count} />
              <StatTile label="Evidenced" value={d.evidenced_count} tone={d.evidenced_count === d.expected_count && d.expected_count > 0 ? "success" : "neutral"} />
              <StatTile label="Not evidenced" value={d.expected_count - d.evidenced_count} tone={d.expected_count - d.evidenced_count ? "warning" : "neutral"} />
            </div>

            <Card padded={false} as="section">
              <div className="p-4 sm:p-5"><CardHeader title="Traceability matrix" description={d.note} className="mb-0" /></div>
              {d.matrix.length === 0 ? (
                <div className="p-4"><EmptyState title="No expected evidence items for this SRS ID" /></div>
              ) : (
                <DataTable<MatrixItem>
                  caption="Traceability matrix"
                  rows={d.matrix}
                  rowKey={(m) => m.key}
                  columns={[
                    { key: "srs", header: "SRS", render: (m) => m.srs_id ?? <span className="text-fg-muted">NFR</span> },
                    {
                      key: "req", header: "Requirement",
                      render: (m) => (<div className="min-w-[14rem]"><p className="font-medium">{m.title}</p><p className="text-xs text-fg-muted">{m.requirement}</p></div>),
                    },
                    {
                      key: "status", header: "Status",
                      render: (m) => <Badge tone={m.evidenced ? RESULT_TONE[m.result ?? ""] ?? "info" : "neutral"}>{m.status}</Badge>,
                    },
                    {
                      key: "target", header: "Target check",
                      render: (m) => !m.evidenced ? <span className="text-fg-muted">—</span>
                        : m.target_met === true ? <Badge tone="success">Metrics meet target</Badge>
                          : m.target_met === false ? <Badge tone="danger">Metrics miss target</Badge>
                            : <span className="text-xs text-fg-muted">See report</span>,
                    },
                    {
                      key: "latest", header: "Latest run",
                      render: (m) => m.executed_at ? (
                        <div className="text-xs"><p>{formatDateTime(m.executed_at)}</p><p className="text-fg-muted">{m.environment?.toUpperCase()}</p></div>
                      ) : <span className="text-fg-muted">—</span>,
                    },
                    { key: "metrics", header: "Key metrics", render: (m) => <KeyMetrics m={m.metrics} /> },
                  ]}
                />
              )}
            </Card>

            <Card padded={false} as="section">
              <div className="p-4 sm:p-5"><CardHeader title="Evidence records" className="mb-0" /></div>
              {d.records.length === 0 ? (
                <div className="p-4">
                  <EmptyState title="No evidence recorded yet" description="Record an executed run with its raw report to evidence a requirement." />
                </div>
              ) : (
                <DataTable<EvidenceRecord>
                  caption="Evidence records"
                  rows={d.records}
                  rowKey={(r) => r.id}
                  columns={[
                    { key: "type", header: "Type", render: (r) => humanize(r.evidence_type) },
                    { key: "srs", header: "SRS IDs", render: (r) => <div className="flex flex-wrap gap-1">{r.srs_ids.map((s) => <Badge key={s} tone="primary">{s}</Badge>)}</div> },
                    { key: "env", header: "Environment", render: (r) => r.environment.toUpperCase() },
                    { key: "executed", header: "Executed", render: (r) => <span className="whitespace-nowrap">{formatDateTime(r.executed_at)}</span> },
                    { key: "result", header: "Result", render: (r) => <Badge tone={RESULT_TONE[r.result]}>{humanize(r.result)}</Badge> },
                    { key: "metrics", header: "Key metrics", render: (r) => <KeyMetrics m={r.metrics} /> },
                    {
                      key: "artifact", header: "Report",
                      render: (r) => r.has_artifact ? (
                        <div className="text-xs">
                          <Button size="sm" variant="ghost" onClick={() => download(r)}>Download</Button>
                          <p className="font-mono text-fg-muted" title={r.artifact_sha256 ?? ""}>sha256 {r.artifact_sha256?.slice(0, 10)}…</p>
                        </div>
                      ) : "—",
                    },
                    { key: "by", header: "Recorded by", render: (r) => <span className="text-xs">{r.recorded_by_name ?? "—"}<br />{formatDateTime(r.created_at)}</span> },
                  ]}
                />
              )}
            </Card>
          </div>
        )}
      </DataView>
    </>
  );
}

function EvidenceForm({ data, onCancel, onCreated }: { data: EvidenceResponse; onCancel: () => void; onCreated: (r: EvidenceRecord) => void }) {
  const [v, setV] = useState({ evidence_type: "", srs_ids: "", environment: "", executed_at: "", result: "", extra_metrics: "" });
  const [metrics, setMetrics] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof v) => (val: string) => {
    setV((x) => ({ ...x, [k]: val }));
    setErrors((e) => ({ ...e, [k]: "" }));
  };
  const visibleMetrics = METRIC_FIELDS.filter((f) => f.types.includes(v.evidence_type));

  const buildMetrics = (): { json: string | null; error?: string } => {
    let obj: Record<string, unknown> = {};
    if (v.extra_metrics.trim()) {
      try {
        const parsed = JSON.parse(v.extra_metrics);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { json: null, error: "Additional metrics must be a JSON object, e.g. {\"error_rate\": 0.1}" };
        obj = parsed as Record<string, unknown>;
      } catch {
        return { json: null, error: "Additional metrics are not valid JSON" };
      }
    }
    for (const f of visibleMetrics) {
      const raw = (metrics[f.key] ?? "").trim();
      if (!raw) continue;
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) return { json: null, error: `${f.label} must be a non-negative number` };
      obj[f.key] = n;
    }
    return { json: Object.keys(obj).length ? JSON.stringify(obj) : null };
  };

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const e: Record<string, string> = {};
    if (!v.evidence_type) e.evidence_type = "Choose the evidence type";
    const ids = v.srs_ids.split(",").map((s) => s.trim()).filter(Boolean);
    if (!ids.length) e.srs_ids = "Enter at least one SRS ID, e.g. US-28";
    else if (ids.some((s) => !/^[A-Za-z][A-Za-z0-9]{1,9}(-[A-Za-z0-9]{1,10}){1,3}$/.test(s))) e.srs_ids = "Use comma-separated IDs like US-28,US-29";
    if (!v.environment) e.environment = "Choose the environment";
    if (!v.executed_at) e.executed_at = "Enter when the run was executed";
    else if (v.executed_at > nowIstLocal()) e.executed_at = "Evidence must come from a run that has already been executed";
    if (!v.result) e.result = "Choose the result";
    if (!file) e.artifact_upload_ref = "Attach the raw report from the run";
    const m = buildMetrics();
    if (m.error) e.metrics_json = m.error;
    setErrors(e);
    setFormError(null);
    const first = Object.keys(e)[0];
    if (first) {
      document.getElementById(first === "metrics_json" ? "extra_metrics" : first)?.focus();
      return;
    }
    setBusy(true);
    try {
      const up = await uploadFile(file as File);
      const rec = await api<EvidenceRecord>("/admin/evidence", {
        method: "POST",
        body: {
          evidence_type: v.evidence_type, srs_ids: ids.map((s) => s.toUpperCase()).join(","), environment: v.environment,
          executed_at: v.executed_at, result: v.result, metrics_json: m.json, artifact_upload_ref: up.upload_ref,
        },
      });
      onCreated(rec);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors.file ? { artifact_upload_ref: err.fieldErrors.file } : err.fieldErrors);
        setFormError(err.message);
      } else setFormError("Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="mb-4" as="section">
      <CardHeader title="Record evidence" description="Record a run that has already been executed, with its raw report. Times are IST." />
      <form onSubmit={submit} noValidate className="space-y-4" aria-label="Record evidence">
        {formError && <Alert tone="danger">{formError}</Alert>}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <SelectField id="evidence_type" label="Evidence type" required value={v.evidence_type} onChange={set("evidence_type")} placeholder="Choose"
            error={errors.evidence_type || undefined} options={data.evidence_types.map((t) => ({ value: t, label: humanize(t) }))} />
          <TextField id="srs_ids" label="SRS IDs" required value={v.srs_ids} onChange={set("srs_ids")} placeholder="US-28,US-29"
            error={errors.srs_ids || undefined} hint="Comma-separated" />
          <SelectField id="environment" label="Environment" required value={v.environment} onChange={set("environment")} placeholder="Choose"
            error={errors.environment || undefined} options={data.environments.map((t) => ({ value: t, label: t.toUpperCase() }))} />
          <TextField id="executed_at" label="Executed at (IST)" type="datetime-local" required value={v.executed_at} max={nowIstLocal()}
            onChange={set("executed_at")} error={errors.executed_at || undefined} />
          <SelectField id="result" label="Result" required value={v.result} onChange={set("result")} placeholder="Choose"
            error={errors.result || undefined} options={data.results.map((t) => ({ value: t, label: humanize(t) }))} />
          <Field id="artifact_upload_ref" label="Raw report" required error={errors.artifact_upload_ref || undefined}
            hint="PDF, CSV, JSON or TXT up to 10 MB (e.g. k6/JMeter output, DR runbook log, VAPT report)">
            <Input id="artifact_upload_ref" type="file" accept=".pdf,.csv,.json,.txt,application/pdf,text/csv,application/json,text/plain"
              invalid={!!errors.artifact_upload_ref}
              aria-describedby={errors.artifact_upload_ref ? "artifact_upload_ref-error" : "artifact_upload_ref-hint"}
              onChange={(e) => { setFile(e.target.files?.[0] ?? null); setErrors((x) => ({ ...x, artifact_upload_ref: "" })); }} />
          </Field>
        </div>
        {visibleMetrics.length > 0 && (
          <fieldset className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <legend className="mb-2 text-sm font-medium text-fg">Key metrics from the report</legend>
            {visibleMetrics.map((f) => (
              <TextField key={f.key} id={`metric_${f.key}`} label={f.label} inputMode="decimal" suffix={f.suffix}
                value={metrics[f.key] ?? ""} onChange={(x) => setMetrics((mm) => ({ ...mm, [f.key]: x }))} />
            ))}
          </fieldset>
        )}
        <TextAreaField id="extra_metrics" label="Additional metrics (JSON object, optional)" value={v.extra_metrics} onChange={set("extra_metrics")}
          rows={3} placeholder='{"error_rate_percent": 0.05, "duration_minutes": 60}' error={errors.metrics_json || undefined} />
        <div className="flex gap-2">
          <Button type="submit" loading={busy}>Record evidence</Button>
          <Button variant="secondary" onClick={onCancel}>Cancel</Button>
        </div>
      </form>
    </Card>
  );
}

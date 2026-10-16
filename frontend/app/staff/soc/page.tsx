"use client";

import { FormEvent, useMemo, useState } from "react";
import {
  Alert, Badge, Button, Card, CardHeader, Countdown, DataTable, DataView, EmptyState, Modal, PageHeader, SelectField,
  TabPanel, Tabs, TextAreaField, TextField, Tone,
} from "@/components/ui";
import { api, ApiError, downloadFile, useApi } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";
import { useSession } from "@/lib/session";

// ------------------------------------------------------------------ types
type AuditItem = {
  seq: number;
  id: string;
  category: string;
  actor_type: string;
  actor_id: string | null;
  actor_name: string | null;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  details: Record<string, unknown>;
  ip: string | null;
  occurred_at: string;
  hash: string;
  retain_until: string;
};

type AuditResponse = {
  items: AuditItem[];
  total: number;
  page: number;
  page_size: number;
  chain: { verified: boolean; checked: number; broken_at_seq: number | null };
  retention: { retention_days: number; earliest_retain_until: string | null; policy: string };
};

type IncidentNote = { at: string; actor_name: string | null; from_status: string | null; to_status: string; note: string | null; cert_in_reference?: string | null };

type Incident = {
  id: string;
  title: string;
  severity: "low" | "medium" | "high" | "critical";
  cert_in_category: string | null;
  cert_in_category_label: string | null;
  detected_at: string;
  cert_in_due_at: string;
  reported_at: string | null;
  cert_in_reference: string | null;
  status: string;
  notes: IncidentNote[];
  overdue: boolean;
  minutes_remaining: number | null;
  reported_late: boolean;
  is_open: boolean;
  allowed_transitions: string[];
  created_by_name: string | null;
};

type IncidentsResponse = {
  items: Incident[];
  open_count: number;
  overdue_count: number;
  reporting_hours: number;
  cert_in_categories: Array<{ value: string; label: string }>;
  cert_in_categories_note: string;
  severities: string[];
};

const CATEGORIES = ["login", "financial", "admin", "content", "consent", "integration"];
const ACTOR_TYPES = ["customer", "staff", "system", "visitor"];
const SEVERITY_TONE: Record<string, Tone> = { low: "neutral", medium: "info", high: "warning", critical: "danger" };
const TRANSITION_LABEL: Record<string, string> = {
  triaged: "Mark triaged",
  reported_to_cert_in: "Mark reported to CERT-In",
  contained: "Mark contained",
  resolved: "Mark resolved",
  closed: "Close",
};

/** Current IST wall-clock time as a datetime-local value (YYYY-MM-DDTHH:MM). */
function nowIstLocal(): string {
  return new Date(Date.now() + 330 * 60000).toISOString().slice(0, 16);
}

export default function SocPage() {
  const [tab, setTab] = useState("audit");
  return (
    <>
      <PageHeader title="Audit & incidents" eyebrow="Security operations"
        description="Search the append-only, hash-chained audit log and track cyber incidents against the CERT-In reporting deadline." />
      <Tabs ariaLabel="Security operations" value={tab} onChange={setTab} idPrefix="soc"
        tabs={[{ id: "audit", label: "Audit log" }, { id: "incidents", label: "Incidents" }]} />
      <TabPanel id="audit" value={tab} idPrefix="soc"><AuditTab /></TabPanel>
      <TabPanel id="incidents" value={tab} idPrefix="soc"><IncidentsTab /></TabPanel>
    </>
  );
}

// ------------------------------------------------------------------ audit log
type AuditFilters = { category: string; actor_type: string; actor_id: string; entity_type: string; entity_id: string; action: string; from: string; to: string };
const EMPTY_FILTERS: AuditFilters = { category: "", actor_type: "", actor_id: "", entity_type: "", entity_id: "", action: "", from: "", to: "" };

function toQuery(f: AuditFilters): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v.trim()) q.set(k, v.trim());
  return q.toString();
}

function AuditTab() {
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const base = toQuery(applied);
  const state = useApi<AuditResponse>(`/admin/audit-events?${base}${base ? "&" : ""}page=${page}&page_size=50`);
  const set = (k: keyof AuditFilters) => (v: string) => setDraft((d) => ({ ...d, [k]: v }));

  const apply = (e: FormEvent) => {
    e.preventDefault();
    setApplied(draft);
    setPage(1);
  };

  const exportCsv = async () => {
    setExporting(true);
    setExportError(null);
    try {
      await downloadFile(`/admin/audit-events/export?format=csv${base ? `&${base}` : ""}`, "audit-events.csv");
    } catch (e) {
      setExportError(e instanceof ApiError ? e.message : "Export failed. Please try again.");
    } finally {
      setExporting(false);
    }
  };

  const fieldErr = state.error?.fieldErrors ?? {};

  return (
    <div className="space-y-4">
      <Card>
        <form onSubmit={apply} className="space-y-4" aria-label="Audit log filters">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <SelectField id="category" label="Category" value={draft.category} onChange={set("category")} placeholder="All categories"
              options={CATEGORIES.map((c) => ({ value: c, label: humanize(c) }))} />
            <SelectField id="actor_type" label="Actor type" value={draft.actor_type} onChange={set("actor_type")} placeholder="Any actor"
              options={ACTOR_TYPES.map((c) => ({ value: c, label: humanize(c) }))} />
            <TextField id="actor_id" label="Actor ID" value={draft.actor_id} onChange={set("actor_id")} />
            <TextField id="action" label="Action starts with" value={draft.action} onChange={set("action")} placeholder="e.g. integration." />
            <TextField id="entity_type" label="Entity type" value={draft.entity_type} onChange={set("entity_type")} placeholder="e.g. incident" />
            <TextField id="entity_id" label="Entity ID" value={draft.entity_id} onChange={set("entity_id")} />
            <TextField id="from" label="From (IST)" type="datetime-local" value={draft.from} onChange={set("from")} error={fieldErr.from} />
            <TextField id="to" label="To (IST)" type="datetime-local" value={draft.to} onChange={set("to")} error={fieldErr.to} />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="submit">Search</Button>
            <Button variant="secondary" onClick={() => { setDraft(EMPTY_FILTERS); setApplied(EMPTY_FILTERS); setPage(1); }}>Clear</Button>
            <Button variant="outline" onClick={exportCsv} loading={exporting}>Export CSV</Button>
          </div>
          {exportError && <Alert tone="danger">{exportError}</Alert>}
        </form>
      </Card>

      <DataView state={state} loadingRows={6}>
        {(d) => (
          <Card padded={false}>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
              <div className="flex flex-wrap items-center gap-2">
                {d.chain.verified ? (
                  <Badge tone="success"><span aria-hidden="true">✓</span> Chain verified · {d.chain.checked.toLocaleString("en-IN")} events</Badge>
                ) : (
                  <Badge tone="danger"><span aria-hidden="true">✕</span> Chain broken at #{d.chain.broken_at_seq}</Badge>
                )}
                <span className="text-sm text-fg-muted">{d.total.toLocaleString("en-IN")} matching events</span>
              </div>
              <p className="text-xs text-fg-muted">Retention: {d.retention.retention_days} days</p>
            </div>
            {!d.chain.verified && (
              <div className="p-4">
                <Alert tone="danger" title="Audit chain integrity failure">
                  Event #{d.chain.broken_at_seq} does not match the hash chain. Treat this as a security incident and record it in the Incidents tab.
                </Alert>
              </div>
            )}
            {d.items.length === 0 ? (
              <div className="p-4"><EmptyState title="No audit events match these filters" /></div>
            ) : (
              <DataTable<AuditItem>
                caption="Audit events"
                rows={d.items}
                rowKey={(r) => r.id}
                columns={[
                  { key: "seq", header: "#", align: "right", render: (r) => r.seq },
                  { key: "occurred_at", header: "Time (IST)", render: (r) => <span className="whitespace-nowrap">{formatDateTime(r.occurred_at)}</span> },
                  { key: "category", header: "Category", render: (r) => <Badge>{humanize(r.category)}</Badge> },
                  {
                    key: "actor", header: "Actor",
                    render: (r) => (
                      <div className="text-xs">
                        <p className="font-medium text-fg">{r.actor_name ?? humanize(r.actor_type)}</p>
                        {r.actor_id && <p className="font-mono text-fg-muted">{r.actor_id.slice(0, 8)}…</p>}
                      </div>
                    ),
                  },
                  { key: "action", header: "Action", render: (r) => <span className="font-mono text-xs">{r.action}</span> },
                  {
                    key: "entity", header: "Entity",
                    render: (r) => r.entity_type ? (
                      <div className="text-xs"><p>{r.entity_type}</p>{r.entity_id && <p className="font-mono text-fg-muted">{r.entity_id.slice(0, 8)}…</p>}</div>
                    ) : "—",
                  },
                  {
                    key: "details", header: "Details",
                    render: (r) => Object.keys(r.details || {}).length ? (
                      <details className="max-w-xs text-xs">
                        <summary className="cursor-pointer">View</summary>
                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-surface-2 p-2">{JSON.stringify(r.details, null, 2)}</pre>
                        <p className="mt-1 font-mono text-fg-muted">hash {r.hash.slice(0, 12)}… · retain until {r.retain_until}</p>
                      </details>
                    ) : <span className="text-fg-muted">—</span>,
                  },
                ]}
              />
            )}
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border p-4 text-sm">
              <span className="text-fg-muted">Page {d.page} of {Math.max(1, Math.ceil(d.total / d.page_size))}</span>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" disabled={d.page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Previous</Button>
                <Button size="sm" variant="secondary" disabled={d.page * d.page_size >= d.total} onClick={() => setPage((p) => p + 1)}>Next</Button>
              </div>
            </div>
            <p className="border-t border-border p-4 text-xs text-fg-muted">{d.retention.policy}</p>
          </Card>
        )}
      </DataView>
    </div>
  );
}

// ------------------------------------------------------------------ incidents
function IncidentsTab() {
  const { hasRole } = useSession();
  const canEdit = hasRole("soc_analyst", "ops_admin");
  const state = useApi<IncidentsResponse>("/admin/incidents");
  const [showForm, setShowForm] = useState(false);
  const [transition, setTransition] = useState<{ incident: Incident; to: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  return (
    <div className="space-y-4">
      {notice && <Alert tone="success" action={<Button size="sm" variant="ghost" onClick={() => setNotice(null)}>Dismiss</Button>}>{notice}</Alert>}
      <DataView state={state} loadingRows={4}>
        {(d) => (
          <>
            <Card>
              <CardHeader title="Incidents"
                description={`Report cyber incidents to CERT-In within ${d.reporting_hours} hours of detection. ${d.open_count} open, ${d.overdue_count} overdue.`}
                actions={canEdit && !showForm ? <Button onClick={() => setShowForm(true)}>Record incident</Button> : undefined} />
              {showForm && (
                <IncidentForm data={d} onCancel={() => setShowForm(false)}
                  onCreated={(i) => { setShowForm(false); setNotice(`Incident recorded. CERT-In report due by ${formatDateTime(i.cert_in_due_at)}.`); state.reload(); }} />
              )}
            </Card>
            {d.items.length === 0 ? (
              <EmptyState title="No incidents recorded" />
            ) : (
              <ul className="space-y-3">
                {d.items.map((i) => (
                  <li key={i.id}>
                    <Card>
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone={SEVERITY_TONE[i.severity]}>{humanize(i.severity)}</Badge>
                            <Badge tone={i.is_open ? "warning" : "success"}>{humanize(i.status)}</Badge>
                            {i.reported_late && <Badge tone="danger">Reported late</Badge>}
                          </div>
                          <h3 className="mt-2 font-semibold text-fg">{i.title}</h3>
                          <p className="text-sm text-fg-muted">{i.cert_in_category_label ?? "—"}</p>
                          <p className="mt-1 text-sm text-fg-muted">
                            Detected {formatDateTime(i.detected_at)} · CERT-In due {formatDateTime(i.cert_in_due_at)}
                          </p>
                          {i.reported_at && (
                            <p className="text-sm text-fg-muted">Reported {formatDateTime(i.reported_at)} · Ref {i.cert_in_reference}</p>
                          )}
                        </div>
                        <div className="text-right">
                          {i.reported_at ? (
                            <Badge tone={i.reported_late ? "danger" : "success"}>Reported to CERT-In</Badge>
                          ) : (
                            <div>
                              <p className="mb-1 text-xs text-fg-muted">CERT-In {d.reporting_hours}-hour deadline</p>
                              <Countdown to={i.cert_in_due_at} />
                            </div>
                          )}
                        </div>
                      </div>
                      {canEdit && i.allowed_transitions.length > 0 && (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {i.allowed_transitions.map((t) => (
                            <Button key={t} size="sm" variant={t === "reported_to_cert_in" ? "primary" : "secondary"}
                              onClick={() => setTransition({ incident: i, to: t })}>
                              {TRANSITION_LABEL[t] ?? humanize(t)}
                            </Button>
                          ))}
                        </div>
                      )}
                      <details className="mt-3 text-sm">
                        <summary className="cursor-pointer text-fg-muted">Timeline ({i.notes.length})</summary>
                        <ol className="mt-2 space-y-2 border-l border-border pl-4">
                          {i.notes.map((n, idx) => (
                            <li key={idx}>
                              <p className="text-xs text-fg-muted">{formatDateTime(n.at)} · {n.actor_name ?? "—"}</p>
                              <p>
                                {n.from_status ? `${humanize(n.from_status)} → ` : ""}{humanize(n.to_status)}
                                {n.cert_in_reference ? ` (ref ${n.cert_in_reference})` : ""}
                              </p>
                              {n.note && <p className="text-fg-muted">{n.note}</p>}
                            </li>
                          ))}
                        </ol>
                      </details>
                    </Card>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </DataView>
      {transition && (
        <TransitionModal key={`${transition.incident.id}-${transition.to}`} incident={transition.incident} to={transition.to}
          onClose={() => setTransition(null)}
          onDone={(msg) => { setTransition(null); setNotice(msg); state.reload(); }} />
      )}
    </div>
  );
}

function IncidentForm({ data, onCancel, onCreated }: { data: IncidentsResponse; onCancel: () => void; onCreated: (i: Incident) => void }) {
  const [v, setV] = useState({ title: "", severity: "", cert_in_category: "", detected_at: nowIstLocal(), note: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof v) => (val: string) => {
    setV((x) => ({ ...x, [k]: val }));
    setErrors((e) => ({ ...e, [k]: "" }));
  };

  const validate = () => {
    const e: Record<string, string> = {};
    if (v.title.trim().length < 5) e.title = "Enter a title of at least 5 characters";
    if (!v.severity) e.severity = "Choose a severity";
    if (!v.cert_in_category) e.cert_in_category = "Choose a CERT-In category";
    if (!v.detected_at) e.detected_at = "Enter when the incident was detected";
    else if (v.detected_at > nowIstLocal()) e.detected_at = "Detection time cannot be in the future";
    return e;
  };

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const e = validate();
    setErrors(e);
    setFormError(null);
    const first = Object.keys(e)[0];
    if (first) {
      document.getElementById(first)?.focus();
      return;
    }
    setBusy(true);
    try {
      const inc = await api<Incident>("/admin/incidents", { method: "POST", body: v });
      onCreated(inc);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors);
        setFormError(err.message);
      } else setFormError("Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate className="space-y-4 border-t border-border pt-4" aria-label="Record incident">
      {formError && <Alert tone="danger">{formError}</Alert>}
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField id="title" label="Title (internal)" required value={v.title} onChange={set("title")} error={errors.title || undefined} maxLength={200}
          hint="Internal only. The public status page shows a generic summary." className="sm:col-span-2" />
        <SelectField id="severity" label="Severity" required value={v.severity} onChange={set("severity")} placeholder="Choose"
          error={errors.severity || undefined} options={data.severities.map((s) => ({ value: s, label: humanize(s) }))} />
        <TextField id="detected_at" label="Detected at (IST)" type="datetime-local" required value={v.detected_at} max={nowIstLocal()}
          onChange={set("detected_at")} error={errors.detected_at || undefined} />
        <SelectField id="cert_in_category" label="CERT-In category" required value={v.cert_in_category} onChange={set("cert_in_category")}
          placeholder="Choose" error={errors.cert_in_category || undefined} hint={data.cert_in_categories_note} className="sm:col-span-2"
          options={data.cert_in_categories.map((c) => ({ value: c.value, label: c.label }))} />
        <TextAreaField id="note" label="Initial note" value={v.note} onChange={set("note")} rows={3} maxLength={2000} className="sm:col-span-2" />
      </div>
      <p className="text-sm text-fg-muted">The CERT-In deadline is {data.reporting_hours} hours after the detection time.</p>
      <div className="flex gap-2">
        <Button type="submit" loading={busy}>Record incident</Button>
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

function TransitionModal({ incident, to, onClose, onDone }: { incident: Incident; to: string; onClose: () => void; onDone: (msg: string) => void }) {
  const [note, setNote] = useState("");
  const [ref, setRef] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const needsRef = to === "reported_to_cert_in";
  const title = useMemo(() => TRANSITION_LABEL[to] ?? humanize(to), [to]);

  const submit = async () => {
    if (needsRef && ref.trim().length < 4) {
      setErrors({ cert_in_reference: "Enter the CERT-In acknowledgement / reference number" });
      document.getElementById("cert_in_reference")?.focus();
      return;
    }
    setErrors({});
    setFormError(null);
    setBusy(true);
    try {
      await api(`/admin/incidents/${incident.id}/transition`, {
        method: "POST", body: { to_status: to, note, cert_in_reference: needsRef ? ref : undefined },
      });
      onDone(`Incident moved to ${humanize(to)}.`);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors);
        setFormError(err.message);
      } else setFormError("Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={title} description={incident.title}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button onClick={submit} loading={busy}>{title}</Button></>}>
      <div className="space-y-4">
        {formError && <Alert tone="danger">{formError}</Alert>}
        {needsRef && (
          <TextField id="cert_in_reference" label="CERT-In reference" required value={ref} onChange={(x) => { setRef(x); setErrors({}); }}
            error={errors.cert_in_reference} maxLength={100} hint="The acknowledgement number received from CERT-In" />
        )}
        <TextAreaField id="transition_note" label="Note" value={note} onChange={setNote} rows={3} maxLength={2000} />
      </div>
    </Modal>
  );
}

"use client";

import { FormEvent, Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, downloadFile, friendlyDetail, upgradeUrlFrom } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";
import SignInOptions from "@/components/SignInOptions";
import Logo from "@/components/Logo";
import WorkspaceBar from "@/components/WorkspaceBar";
import { Alert, EmptyState, ErrorState, LoadingState } from "@/components/States";
import { BRAND_NAME, PRODUCT_NAME } from "@/lib/brand";
import { tierName } from "@/lib/plans";

type Me = { id: string; email: string; name: string; provider: string; tier: string };
type Project = { id: string; name: string; requirements: string; status: string; created_at: string };

const MIN_CREATE_PROGRESS_MS = 400;
const NAME_MAX = 200;
const REQ_MAX = 8000;

const STAGE_LABELS: Record<string, string> = {
  requirements: "Requirements",
  design: "Design",
  code: "Code",
  tests: "Test Cases",
  nfr: "NFR Testing",
};

function formatDate(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function CrownAiWorkspaceInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const signInFailed = searchParams.get("error") === "sign_in_failed";
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [name, setName] = useState("");
  const [requirements, setRequirements] = useState("");
  const [formTouched, setFormTouched] = useState<{ name?: boolean; requirements?: boolean }>({});
  const [formSubmitted, setFormSubmitted] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  const [confirmAccountDelete, setConfirmAccountDelete] = useState(false);
  const [deletingAccount, setDeletingAccount] = useState(false);
  const [accountDeleteError, setAccountDeleteError] = useState<string | null>(null);

  useEffect(() => {
    setToken(getToken());
    setReady(true);
  }, []);

  const load = useCallback(async (t: string) => {
    setLoading(true);
    setLoadError(null);
    try {
      const [meRes, projectsRes] = await Promise.all([api.get<Me>("/auth/me", t), api.get<Project[]>("/projects", t)]);
      setMe(meRes);
      setProjects(projectsRes);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        setToken(null);
      } else {
        setLoadError(
          err instanceof ApiError && err.status === 0
            ? friendlyDetail(err.detail)
            : "Could not load your workspace. Please try again."
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (ready && token) load(token);
  }, [ready, token, load]);

  // Warm the project route while the user fills in the form, so the redirect
  // after "Generate Project" lands right away instead of waiting for the dev
  // server to build /crown-ai/projects/[id] on demand. The plain fetch covers
  // `next dev`, where router.prefetch may be skipped; errors are irrelevant.
  useEffect(() => {
    if (!ready || !token) return;
    router.prefetch("/crown-ai/projects/warm-up");
    fetch("/crown-ai/projects/warm-up", { cache: "no-store" }).catch(() => {});
  }, [ready, token, router]);

  // Inline validation, mirroring the API's rules (non-blank, length caps).
  const nameError = !name.trim()
    ? "Give your project a name."
    : name.trim().length > NAME_MAX
    ? `Name must be ${NAME_MAX} characters or fewer.`
    : null;
  const reqError = !requirements.trim()
    ? "Describe what you want to build — this drives every generated stage."
    : requirements.trim().length > REQ_MAX
    ? `Requirements must be ${REQ_MAX} characters or fewer.`
    : null;
  const showNameError = (formSubmitted || formTouched.name) && nameError;
  const showReqError = (formSubmitted || formTouched.requirements) && reqError;

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    if (!token || creating) return;
    setFormSubmitted(true);
    if (nameError || reqError) {
      document.getElementById(nameError ? "proj-name" : "proj-reqs")?.focus();
      return;
    }
    setCreating(true);
    setCreateError(null);
    const started = Date.now();
    try {
      const project = await api.post<Project>("/projects", { name: name.trim(), requirements: requirements.trim() }, token);
      // Keep the "Generating…" state visible briefly even when the API
      // answers instantly, so the click always gets visible feedback.
      const remaining = MIN_CREATE_PROGRESS_MS - (Date.now() - started);
      if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, remaining));
      // Take the user straight into the pipeline and kick off generation
      // automatically instead of leaving them to click "Generate" five
      // separate times, one per stage.
      router.push(`/crown-ai/projects/${project.id}?autogenerate=1`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        setToken(null);
        return;
      }
      setCreateError(err instanceof ApiError ? friendlyDetail(err.detail) : "Could not create project.");
      setCreating(false);
    }
  }

  async function onConfirmDelete() {
    if (!token || !confirmDeleteId) return;
    const id = confirmDeleteId;
    setDeletingId(id);
    setActionError(null);
    try {
      await api.del(`/projects/${id}`, token);
      setProjects((prev) => (prev || []).filter((p) => p.id !== id));
      setDeleteNotice("Project deleted. All of its generated artifacts were removed too.");
      setTimeout(() => setDeleteNotice(null), 5000);
    } catch (err) {
      // Already gone (e.g. deleted in another tab): reflect that locally.
      if (err instanceof ApiError && err.status === 404) {
        setProjects((prev) => (prev || []).filter((p) => p.id !== id));
      } else {
        setActionError("Could not delete this project. Please try again.");
      }
    } finally {
      setDeletingId(null);
      setConfirmDeleteId(null);
    }
  }

  async function onDownloadClick(id: string) {
    if (!token) return;
    setDownloadingId(id);
    setActionError(null);
    const result = await downloadFile(`/projects/${id}/download`, token);
    setDownloadingId(null);
    if ("error" in result) {
      if (result.error.status === 402) {
        router.push(upgradeUrlFrom(result.error.detail));
        return;
      }
      setActionError(friendlyDetail(result.error.detail));
      return;
    }
    const url = URL.createObjectURL(result.blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = result.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function onDeleteAccount() {
    if (!token) return;
    setDeletingAccount(true);
    setAccountDeleteError(null);
    try {
      await api.del("/auth/me", token);
      clearToken();
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        setToken(null);
        return;
      }
      setAccountDeleteError(
        err instanceof ApiError ? friendlyDetail(err.detail) : "Could not delete your account. Please try again."
      );
      setDeletingAccount(false);
    }
  }

  function signOut() {
    clearToken();
    setToken(null);
    setMe(null);
    setProjects(null);
  }

  if (!ready) {
    return (
      <div className="container-page section">
        <h1 className="page-title mb-6">
          <span className="text-gold-gradient">{PRODUCT_NAME}</span> Workspace
        </h1>
        <LoadingState label="Opening your workspace…" rows={2} />
      </div>
    );
  }

  if (!token) {
    return (
      <div className="hero-glow">
        <div className="container-page flex min-h-[65vh] flex-col items-center justify-center py-16 text-center">
          <Logo variant="mark" height={84} decorative />
          <h1 className="page-title mt-6">
            Sign in to <span className="text-gold-gradient">{PRODUCT_NAME}</span>
          </h1>
          <p className="body-muted mt-3 max-w-md">
            {PRODUCT_NAME} sessions are independent and tied to your account. Sign in with Google or Microsoft to
            continue. Your {BRAND_NAME} account works for the whole site.
          </p>
          {signInFailed && (
            <div className="mt-4 w-full max-w-md">
              <Alert kind="error">Sign-in didn&apos;t complete. Please try again.</Alert>
            </div>
          )}
          <div className="mt-6">
            <SignInOptions optionsClassName="flex flex-col gap-3 sm:flex-row sm:justify-center" />
          </div>
        </div>
      </div>
    );
  }

  const isPro = !!me && me.tier !== "free";
  const projectToDelete = projects?.find((p) => p.id === confirmDeleteId);

  return (
    <div>
      <WorkspaceBar
        subtitle={
          me ? (
            <>
              {me.name} · {me.email}
            </>
          ) : loading ? (
            "Loading account…"
          ) : undefined
        }
        actions={
          <>
            {me && (
              <span className={isPro ? "badge badge-gold" : "badge badge-neutral"}>
                {isPro ? `♛ ${tierName(me.tier)} plan` : "Free tier"}
              </span>
            )}
            {me && !isPro && (
              <Link href="/pricing" className="btn-ghost btn-sm">
                Upgrade
              </Link>
            )}
            <button type="button" className="btn-secondary btn-sm" onClick={signOut}>
              Sign out
            </button>
          </>
        }
      />

      {confirmDeleteId && (
        <div className="modal-backdrop">
          <div className="modal-panel max-w-sm" role="alertdialog" aria-modal="true" aria-labelledby="del-title" aria-describedby="del-desc">
            <h2 id="del-title" className="text-lg font-bold">
              Delete {projectToDelete ? `“${projectToDelete.name}”` : "this project"}?
            </h2>
            <p id="del-desc" className="body-muted mt-2">
              This permanently removes the project and all generated artifacts (requirements, design, code, tests, NFR
              reports). This can&apos;t be undone.
            </p>
            <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmDeleteId(null)}
                disabled={deletingId !== null}
                autoFocus
              >
                Cancel
              </button>
              <button type="button" className="btn-danger" onClick={onConfirmDelete} disabled={deletingId !== null}>
                {deletingId ? "Deleting…" : "Delete permanently"}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmAccountDelete && (
        <div className="modal-backdrop">
          <div
            className="modal-panel max-w-sm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="acct-del-title"
            aria-describedby="acct-del-desc"
          >
            <h2 id="acct-del-title" className="text-lg font-bold">
              Delete your account and data?
            </h2>
            <p id="acct-del-desc" className="body-muted mt-2">
              This permanently erases your {PRODUCT_NAME} account, every project and every generated artifact, and
              signs you out. Paid-plan access on this account ends. This can&apos;t be undone.
            </p>
            {accountDeleteError && (
              <div className="mt-4">
                <Alert kind="error">{accountDeleteError}</Alert>
              </div>
            )}
            <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmAccountDelete(false)}
                disabled={deletingAccount}
                autoFocus
              >
                Cancel
              </button>
              <button type="button" className="btn-danger" onClick={onDeleteAccount} disabled={deletingAccount}>
                {deletingAccount ? "Deleting…" : "Delete everything"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="container-page py-10">
        <div className="space-y-3">
          {deleteNotice && <Alert kind="success">{deleteNotice}</Alert>}
          {actionError && (
            <Alert kind="error" testId="workspace-error">
              {actionError}
            </Alert>
          )}
        </div>

        <div className="mt-2 grid gap-8 lg:grid-cols-3">
          <aside className="lg:col-span-1">
            {!showCreateForm ? (
              <div className="card text-center">
                <h2 className="card-title">Start a new project</h2>
                <p className="body-muted mt-2">
                  Describe your idea in plain language and {PRODUCT_NAME} generates all five SDLC/STLC stages.
                </p>
                <button type="button" className="btn-primary mt-5 w-full" onClick={() => setShowCreateForm(true)}>
                  + New Project
                </button>
              </div>
            ) : (
              <form onSubmit={onCreate} noValidate className="card space-y-4">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="card-title">New project</h2>
                  <button
                    type="button"
                    className="btn-ghost btn-sm"
                    onClick={() => setShowCreateForm(false)}
                    disabled={creating}
                  >
                    Cancel
                  </button>
                </div>
                <div>
                  <label className="label" htmlFor="proj-name">
                    Project name
                  </label>
                  <input
                    id="proj-name"
                    className="input"
                    required
                    maxLength={NAME_MAX}
                    value={name}
                    disabled={creating}
                    aria-invalid={showNameError ? true : undefined}
                    aria-describedby={showNameError ? "proj-name-error" : undefined}
                    // Touched on change, not blur: a blur-triggered error would
                    // shift the submit button between mousedown and mouseup.
                    onChange={(e) => { setName(e.target.value); setFormTouched((t) => ({ ...t, name: true })); }}
                    autoFocus
                  />
                  {showNameError && (
                    <p id="proj-name-error" className="field-error">
                      {nameError}
                    </p>
                  )}
                </div>
                <div>
                  <label className="label" htmlFor="proj-reqs">
                    Requirements / spec
                  </label>
                  <textarea
                    id="proj-reqs"
                    className="input"
                    rows={6}
                    required
                    maxLength={REQ_MAX}
                    placeholder="e.g. A customer loyalty points system for a retail app…"
                    value={requirements}
                    disabled={creating}
                    aria-invalid={showReqError ? true : undefined}
                    aria-describedby={showReqError ? "proj-reqs-error proj-reqs-count" : "proj-reqs-count"}
                    onChange={(e) => {
                      setRequirements(e.target.value);
                      setFormTouched((t) => ({ ...t, requirements: true }));
                    }}
                  />
                  {showReqError && (
                    <p id="proj-reqs-error" className="field-error">
                      {reqError}
                    </p>
                  )}
                  <p id="proj-reqs-count" className="field-hint text-right">
                    {requirements.length.toLocaleString()} / {REQ_MAX.toLocaleString()}
                  </p>
                </div>
                {createError && <Alert kind="error">{createError}</Alert>}
                <button type="submit" className="btn-primary w-full" disabled={creating}>
                  {creating && <span className="spinner" aria-hidden="true" />}
                  {creating ? "Generating…" : "Generate Project"}
                </button>
                <p role="status" aria-live="polite" className="min-h-[1.25rem] text-xs text-muted">
                  {creating ? "Creating your project — all 5 stages will start generating automatically…" : ""}
                </p>
              </form>
            )}
            {me && !isPro && (
              <p className="body-muted mt-4 text-center text-xs">
                Free tier: up to 5 generations per day. Downloads need a{" "}
                <Link href="/pricing" className="link">
                  paid plan
                </Link>
                .
              </p>
            )}
            {me && (
              <div className="card mt-6 p-5">
                <h2 className="text-sm font-bold">Your data</h2>
                <p className="body-muted mt-1 text-xs">
                  Delete single projects from the list, or erase your whole account and everything in it. See our{" "}
                  <Link href="/legal/privacy" className="link">
                    Privacy Policy
                  </Link>{" "}
                  for your rights.
                </p>
                <button
                  type="button"
                  className="btn-ghost btn-sm mt-3 text-danger"
                  onClick={() => {
                    setAccountDeleteError(null);
                    setConfirmAccountDelete(true);
                  }}
                >
                  Delete my account
                </button>
              </div>
            )}
          </aside>

          <section className="lg:col-span-2" aria-labelledby="projects-heading">
            <div className="flex items-center justify-between gap-2">
              <h2 id="projects-heading" className="section-title text-xl">
                Your projects
              </h2>
              {projects && projects.length > 0 && <span className="badge badge-neutral">{projects.length}</span>}
            </div>

            <div className="mt-4">
              {loadError && (
                <ErrorState
                  title="Couldn't load your workspace"
                  message={loadError}
                  onRetry={() => token && load(token)}
                  retrying={loading}
                />
              )}
              {!loadError && projects === null && <LoadingState label="Loading projects…" />}
              {!loadError && projects !== null && projects.length === 0 && (
                <EmptyState
                  title="No projects yet"
                  message="Create your first project to generate requirements, design, code, test cases and NFR reports."
                  action={
                    !showCreateForm && (
                      <button type="button" className="btn-primary" onClick={() => setShowCreateForm(true)}>
                        Create a project
                      </button>
                    )
                  }
                />
              )}
              {!loadError && projects && projects.length > 0 && (
                <ul className="space-y-3">
                  {projects.map((p) => (
                    <li key={p.id} className="card card-interactive flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0">
                        <Link href={`/crown-ai/projects/${p.id}`} className="link text-base">
                          {p.name}
                        </Link>
                        <p className="mt-1 line-clamp-1 text-sm text-muted">{p.requirements}</p>
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                          <span className="badge">Stage: {STAGE_LABELS[p.status] ?? p.status}</span>
                          {formatDate(p.created_at) && <span>Created {formatDate(p.created_at)}</span>}
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <Link href={`/crown-ai/projects/${p.id}`} className="btn-secondary btn-sm">
                          Open
                        </Link>
                        <button
                          type="button"
                          onClick={() => onDownloadClick(p.id)}
                          disabled={downloadingId === p.id}
                          className="btn-ghost btn-sm"
                          aria-label={`Download ${p.name}`}
                        >
                          {downloadingId === p.id ? "Preparing…" : isPro ? "Download" : "🔒 Download"}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setActionError(null);
                            setConfirmDeleteId(p.id);
                          }}
                          className="btn-ghost btn-sm text-danger"
                          aria-label={`Delete ${p.name}`}
                        >
                          Delete
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

export default function CrownAiWorkspacePage() {
  return (
    <Suspense
      fallback={
        <div className="container-page section">
          <h1 className="page-title mb-6">
            <span className="text-gold-gradient">{PRODUCT_NAME}</span> Workspace
          </h1>
          <LoadingState label="Opening your workspace…" rows={2} />
        </div>
      }
    >
      <CrownAiWorkspaceInner />
    </Suspense>
  );
}

"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { api, ApiError, downloadFile, friendlyDetail, upgradeUrlFrom } from "@/lib/api";
import { getToken } from "@/lib/auth";
import WorkspaceBar from "@/components/WorkspaceBar";
import { Alert, EmptyState, ErrorState, LoadingState } from "@/components/States";

type Artifact = {
  id: string;
  stage: string;
  stage_label: string;
  content: string;
  created_at: string;
};

type ProjectDetail = {
  id: string;
  name: string;
  requirements: string;
  status: string;
  created_at: string;
  artifacts: Artifact[];
};

const STAGES: { key: string; label: string }[] = [
  { key: "requirements", label: "Requirements" },
  { key: "design", label: "Design" },
  { key: "code", label: "Code" },
  { key: "tests", label: "Test Cases" },
  { key: "nfr", label: "NFR Testing" },
];

// Generation can finish in a few milliseconds; keep each stage's progress
// state on screen at least this long so the visitor (and assistive tech)
// always sees that something is happening, well inside the 3s budget.
const MIN_STAGE_PROGRESS_MS = 600;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ProjectDetailInner() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const autogenerate = searchParams.get("autogenerate") === "1";
  const autoTriggered = useRef(false);

  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [activeStage, setActiveStage] = useState<string | null>(null);
  const [generating, setGenerating] = useState<string | null>(null);
  const [generatingAll, setGeneratingAll] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setToken(getToken());
  }, []);

  const refresh = useCallback(
    async (t: string): Promise<ProjectDetail | null> => {
      try {
        const data = await api.get<ProjectDetail>(`/projects/${id}`, t);
        setProject(data);
        setLoadError(null);
        if (data.artifacts.length > 0) setActiveStage(data.artifacts[data.artifacts.length - 1].stage);
        return data;
      } catch (err) {
        if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
          setNotFound(true);
        } else if (err instanceof ApiError && err.status === 401) {
          router.replace("/crown-ai");
        } else {
          setLoadError(
            err instanceof ApiError && err.status === 0 ? friendlyDetail(err.detail) : "Could not load this project."
          );
        }
        return null;
      }
    },
    [id, router]
  );

  useEffect(() => {
    if (token === undefined) return;
    if (token === null) {
      router.replace("/crown-ai");
      return;
    }
    refresh(token);
  }, [token, router, refresh]);

  const doneStages = new Set(project?.artifacts.map((a) => a.stage));
  const nextStageIndex = STAGES.findIndex((s) => !doneStages.has(s.key));
  const allDone = nextStageIndex === -1;

  // Latest loaded project, readable from stable callbacks without making
  // them (and the effects that use them) change on every refresh.
  const projectRef = useRef<ProjectDetail | null>(null);
  useEffect(() => {
    projectRef.current = project;
  }, [project]);

  const runGenerate = useCallback(
    async (stageKey: string, t: string): Promise<ProjectDetail | null> => {
      setGenerating(stageKey);
      setGenError(null);
      const started = Date.now();
      const holdProgress = async () => {
        const remaining = MIN_STAGE_PROGRESS_MS - (Date.now() - started);
        if (remaining > 0) await sleep(remaining);
      };
      try {
        await api.post(`/projects/${id}/generate/${stageKey}`, undefined, t);
        await holdProgress();
        const fresh = await refresh(t);
        setActiveStage(stageKey);
        return fresh;
      } catch (err) {
        await holdProgress();
        setGenError(err instanceof ApiError ? friendlyDetail(err.detail) : "Generation failed. Please try again.");
        return null;
      } finally {
        setGenerating(null);
      }
    },
    [id, refresh]
  );

  async function onGenerate(stageKey: string) {
    if (!token) return;
    await runGenerate(stageKey, token);
  }

  // Runs every remaining stage in order with one click, instead of requiring
  // a separate manual "Generate" click per stage. Starts from the latest
  // known artifacts, whether triggered automatically or via the button.
  const onGenerateAll = useCallback(
    async (t: string) => {
      setGeneratingAll(true);
      let current = projectRef.current;
      for (const stage of STAGES) {
        const done = new Set((current?.artifacts || []).map((a) => a.stage));
        if (done.has(stage.key)) continue;
        const fresh = await runGenerate(stage.key, t);
        if (!fresh) break;
        current = fresh;
      }
      setGeneratingAll(false);
    },
    [runGenerate]
  );

  useEffect(() => {
    if (!autogenerate || !token || !project) return;
    if (autoTriggered.current) return;
    if (project.artifacts.length > 0) return;
    autoTriggered.current = true;
    projectRef.current = project;
    onGenerateAll(token);
  }, [autogenerate, token, project, onGenerateAll]);

  async function onDownload() {
    if (!token) return;
    setDownloading(true);
    const result = await downloadFile(`/projects/${id}/download`, token);
    setDownloading(false);
    if ("error" in result) {
      if (result.error.status === 402) {
        router.push(upgradeUrlFrom(result.error.detail));
        return;
      }
      setGenError(friendlyDetail(result.error.detail));
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

  async function onCopy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  if (token === undefined || (token && !project && !notFound && !loadError)) {
    return (
      <div>
        <WorkspaceBar subtitle="Loading project…" />
        <div className="container-page section">
          <LoadingState label="Loading project…" />
        </div>
      </div>
    );
  }
  if (notFound) {
    return (
      <div>
        <WorkspaceBar />
        <div className="container-page section">
          <EmptyState
            icon="?"
            title="Project not found"
            message="This project doesn't exist, was deleted, or belongs to another account."
            action={
              <Link href="/crown-ai" className="btn-primary">
                Back to workspace
              </Link>
            }
          />
        </div>
      </div>
    );
  }
  if (loadError || !project) {
    return (
      <div>
        <WorkspaceBar />
        <div className="container-page section">
          <ErrorState
            title="Couldn't load this project"
            message={loadError || "Could not load this project."}
            retrying={reloading}
            onRetry={async () => {
              if (!token) return;
              setReloading(true);
              await refresh(token);
              setReloading(false);
            }}
          />
        </div>
      </div>
    );
  }

  const activeArtifact = project.artifacts.find((a) => a.stage === activeStage);
  const busy = generating !== null || generatingAll;
  const doneCount = STAGES.filter((s) => doneStages.has(s.key)).length;
  const generatingIndex = STAGES.findIndex((s) => s.key === generating);
  const progressText =
    generatingIndex !== -1
      ? `Generating ${STAGES[generatingIndex].label} (${generatingIndex + 1}/${STAGES.length})…`
      : busy
      ? `Preparing next stage (${doneCount}/${STAGES.length})…`
      : allDone
      ? `All stages complete (${STAGES.length}/${STAGES.length})`
      : doneCount > 0
      ? `✓ ${doneCount}/${STAGES.length} stages complete`
      : `No stages run yet (0/${STAGES.length})`;

  return (
    <div>
      <WorkspaceBar
        headingTitle={false}
        subtitle={project.name}
        actions={
          <Link href="/crown-ai" className="btn-secondary btn-sm">
            ← All projects
          </Link>
        }
      />

      <div className="container-page py-10">
        <h1 className="page-title break-words">{project.name}</h1>
        <p className="body-muted mt-2 max-w-3xl whitespace-pre-line">{project.requirements}</p>

        <section className="card mt-8" aria-labelledby="pipeline-heading">
          <h2 id="pipeline-heading" className="card-title">
            SDLC/STLC pipeline
          </h2>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {STAGES.map((s, i) => {
              const done = doneStages.has(s.key);
              const isNext = i === nextStageIndex;
              const locked = !done && !isNext;
              const active = activeStage === s.key;
              return (
                <button
                  key={s.key}
                  type="button"
                  disabled={locked || busy}
                  aria-pressed={done ? active : undefined}
                  onClick={() => (done ? setActiveStage(s.key) : onGenerate(s.key))}
                  className={`inline-flex min-h-[2.5rem] items-center gap-2 rounded-full border px-4 text-sm font-semibold transition ${
                    done
                      ? active
                        ? "border-transparent bg-gold-gradient text-on-gold shadow-gold"
                        : "border-line-strong bg-brand-soft text-brand-soft-ink hover:bg-subtle-2"
                      : isNext
                      ? "border-dashed border-gold-3 bg-surface text-brand hover:bg-brand-soft"
                      : "cursor-not-allowed border-line bg-subtle text-faint"
                  }`}
                >
                  {generating === s.key && <span className="spinner" aria-hidden="true" />}
                  {generating === s.key ? "Generating…" : done ? `✓ ${s.label}` : isNext ? `Generate ${s.label}` : s.label}
                </button>
              );
            })}

            {!allDone && (
              <button
                type="button"
                disabled={busy}
                onClick={() => token && onGenerateAll(token)}
                className="btn-primary sm:ml-2"
              >
                {generatingAll ? "Generating all artifacts…" : "Generate all artifacts"}
              </button>
            )}
          </div>

          <div
            role="status"
            aria-live="polite"
            data-testid="generation-progress"
            data-busy={busy ? "true" : "false"}
            className="mt-5 max-w-xl"
          >
            <div className="flex items-center gap-2 text-sm text-ink">
              {busy && <span className="spinner" aria-hidden="true" />}
              <span>{progressText}</span>
            </div>
            <div className="progress-track mt-2" aria-hidden="true">
              <div className="progress-bar" style={{ width: `${(doneCount / STAGES.length) * 100}%` }} />
            </div>
          </div>

          {genError && (
            <div className="mt-4">
              <Alert kind="warning" testId="generation-error">
                {genError}
                {/limit/i.test(genError) && (
                  <>
                    {" "}
                    <Link href="/pricing" className="font-bold underline">
                      View plans
                    </Link>
                  </>
                )}
              </Alert>
            </div>
          )}
        </section>

        <div className="mt-6 grid gap-6 lg:grid-cols-3">
          <section className="card min-w-0 lg:col-span-2" aria-labelledby="artifact-heading">
            {activeArtifact ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 id="artifact-heading" className="card-title">
                    {activeArtifact.stage_label}
                  </h2>
                  <button type="button" className="btn-ghost btn-sm" onClick={() => onCopy(activeArtifact.content)}>
                    {copied ? "Copied ✓" : "Copy"}
                  </button>
                </div>
                <pre className="code-block mt-3">{activeArtifact.content}</pre>
              </>
            ) : (
              <>
                <h2 id="artifact-heading" className="sr-only">
                  Generated artifact
                </h2>
                <EmptyState
                  title={busy ? "Generating your first artifact…" : "No artifacts generated yet"}
                  message={
                    busy
                      ? "Requirements come first; each stage appears here as soon as it's ready."
                      : 'Click "Generate Requirements" (or "Generate all artifacts") above to start.'
                  }
                />
              </>
            )}
          </section>

          <aside className="card h-fit">
            <h2 className="card-title">Download</h2>
            <p className="body-muted mt-2">Export all generated artifacts as a .zip file. Requires a paid plan.</p>
            <button
              type="button"
              className="btn-primary mt-4 w-full"
              onClick={onDownload}
              disabled={downloading || project.artifacts.length === 0}
            >
              {downloading && <span className="spinner" aria-hidden="true" />}
              {downloading ? "Preparing download…" : "Download artifacts"}
            </button>
            {project.artifacts.length === 0 && (
              <p className="field-hint">Generate at least one stage to enable downloads.</p>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

export default function ProjectDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="container-page section">
          <LoadingState label="Loading…" />
        </div>
      }
    >
      <ProjectDetailInner />
    </Suspense>
  );
}

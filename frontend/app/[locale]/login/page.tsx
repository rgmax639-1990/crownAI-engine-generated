"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useEffect, useState } from "react";
import {
  Alert, Button, Card, CardHeader, LoadingState, MockBadge, PageHeader, RadioGroup, Stepper, TextField, TextLink,
} from "@/components/ui";
import { api, ApiError, clearToken, getToken, setToken } from "@/lib/api";
import { useEnvironment } from "@/lib/env";
import { formatDateTime } from "@/lib/format";
import { isLocale, Locale, lp } from "@/lib/i18n";

type Factor = { id: string; factor_type: "passkey" | "totp" | "push" | "sms_otp"; label: string | null; is_primary: boolean };
type LoginRes = {
  access_token: string; scope: string; user_id: string; user_type: string; expires_at: string;
  available_factors: Factor[]; risk_level: string; risk_score: number; next_step: string; mock_otp_hint: string | null;
};
type Challenge = {
  challenge_id: string; factor_type: string; purpose: string; expires_at: string; mock: boolean;
  delivery?: string; mock_code?: string; mock_label?: string;
};
type VerifyRes = {
  access_token: string; scope: string; user_type: string; last_login_at: string | null; last_login_ip: string | null;
  other_sessions_terminated: number;
};

const T = {
  en: {
    title: "Login to internet banking",
    subtitle: "Sign in with your password, then confirm with a second factor.",
    steps: ["Password", "Second factor", "Signed in"],
    email: "Email or User ID",
    password: "Password",
    showPw: "Show",
    hidePw: "Hide",
    showPwLabel: "Show password",
    hidePwLabel: "Hide password",
    continue: "Continue",
    forgot: "Forgot password?",
    forgotTitle: "Resetting your password",
    forgotBody: "To set a new password, register for internet banking again with your account number and registered mobile number (verified by OTP), or visit any branch with a photo ID.",
    forgotLink: "Go to internet banking registration",
    newUser: "New to internet banking?",
    register: "Register now",
    noAccount: "Don't have an account yet?",
    openAccount: "Open one online in minutes",
    tipTitle: "Safe banking tip",
    tip: "We never call, SMS or email asking for your password, OTP, PIN or CVV. Check the website address before you sign in, and never sign in from a link in a message.",
    required: "This field is required",
    wrongCreds: "Incorrect user ID or password.",
    attempts: (n: number) => `${n} attempt${n === 1 ? "" : "s"} left before login is locked for 15 minutes.`,
    lockedTitle: "Login temporarily locked",
    locked: (t: string) => `Too many failed attempts. You can try again after ${t}, or reset your password.`,
    chooseFactor: "Choose how to verify it's you",
    factorLabel: { passkey: "Passkey (recommended)", totp: "Authenticator app code", push: "Push approval on your phone", sms_otp: "SMS OTP to registered mobile" } as Record<string, string>,
    sendCode: "Continue",
    code: "Verification code",
    codeHint: "Enter the 6-digit code",
    verify: "Verify and sign in",
    useAnother: "Use another method",
    mockCode: "Mock code:",
    passkeyMock: "The passkey prompt is simulated in this environment: enter the code shown.",
    startOver: "Sign in with a different user",
    welcome: "You're signed in",
    lastLogin: "Your previous login",
    fromIp: "from IP",
    firstLogin: "This is your first login. Welcome!",
    otherSessions: (n: number) => `${n} other session${n === 1 ? " was" : "s were"} signed out, because only one session can be active at a time. If that wasn't you, report fraud immediately.`,
    notYou: "Don't recognise this login?",
    reportFraud: "Report fraud",
    reasons: {
      expired: "You were signed out after 5 minutes of inactivity. Please sign in again.",
      superseded: "You were signed out because you signed in on another device or browser.",
      logged_out: "You have logged out securely. Close the browser if you are on a shared computer.",
    } as Record<string, string>,
    demo: "Demo accounts (synthetic data)",
    demoHint: (pw: string) => `All demo users use the password ${pw}. Available only in this demo environment.`,
    use: "Use",
    sessionGone: "Your sign-in has expired. Enter your password again.",
    risk: "We noticed an unusual sign-in. Please complete the extra check.",
  },
  hi: {
    title: "इंटरनेट बैंकिंग लॉगिन",
    subtitle: "अपने पासवर्ड से साइन इन करें, फिर दूसरे फ़ैक्टर से पुष्टि करें।",
    steps: ["पासवर्ड", "दूसरा फ़ैक्टर", "साइन इन"],
    email: "ईमेल या यूज़र आईडी",
    password: "पासवर्ड",
    showPw: "दिखाएँ",
    hidePw: "छिपाएँ",
    showPwLabel: "पासवर्ड दिखाएँ",
    hidePwLabel: "पासवर्ड छिपाएँ",
    continue: "आगे बढ़ें",
    forgot: "पासवर्ड भूल गए?",
    forgotTitle: "पासवर्ड रीसेट करना",
    forgotBody: "नया पासवर्ड सेट करने के लिए अपने खाता संख्या और पंजीकृत मोबाइल नंबर (OTP से सत्यापित) से इंटरनेट बैंकिंग के लिए फिर से पंजीकरण करें, या फ़ोटो आईडी के साथ किसी भी शाखा में जाएँ।",
    forgotLink: "इंटरनेट बैंकिंग पंजीकरण पर जाएँ",
    newUser: "इंटरनेट बैंकिंग में नए हैं?",
    register: "अभी पंजीकरण करें",
    noAccount: "अभी तक खाता नहीं है?",
    openAccount: "कुछ ही मिनटों में ऑनलाइन खाता खोलें",
    tipTitle: "सुरक्षित बैंकिंग सुझाव",
    tip: "हम कभी भी कॉल, SMS या ईमेल से आपका पासवर्ड, OTP, PIN या CVV नहीं माँगते। साइन इन से पहले वेबसाइट का पता जाँचें और किसी संदेश के लिंक से साइन इन न करें।",
    required: "यह फ़ील्ड आवश्यक है",
    wrongCreds: "यूज़र आईडी या पासवर्ड गलत है।",
    attempts: (n: number) => `लॉगिन 15 मिनट के लिए लॉक होने से पहले ${n} प्रयास शेष हैं।`,
    lockedTitle: "लॉगिन अस्थायी रूप से लॉक है",
    locked: (t: string) => `बहुत अधिक असफल प्रयास। आप ${t} के बाद फिर से कोशिश कर सकते हैं, या पासवर्ड रीसेट करें।`,
    chooseFactor: "पहचान की पुष्टि का तरीका चुनें",
    factorLabel: { passkey: "पासकी (अनुशंसित)", totp: "ऑथेंटिकेटर ऐप कोड", push: "फ़ोन पर पुश स्वीकृति", sms_otp: "पंजीकृत मोबाइल पर SMS OTP" } as Record<string, string>,
    sendCode: "आगे बढ़ें",
    code: "सत्यापन कोड",
    codeHint: "6 अंकों का कोड दर्ज करें",
    verify: "सत्यापित करें और साइन इन करें",
    useAnother: "दूसरा तरीका चुनें",
    mockCode: "मॉक कोड:",
    passkeyMock: "इस वातावरण में पासकी प्रॉम्प्ट सिम्युलेट किया गया है: दिखाया गया कोड दर्ज करें।",
    startOver: "किसी दूसरे यूज़र से साइन इन करें",
    welcome: "आप साइन इन हो गए हैं",
    lastLogin: "आपका पिछला लॉगिन",
    fromIp: "IP से",
    firstLogin: "यह आपका पहला लॉगिन है। स्वागत है!",
    otherSessions: (n: number) => `${n} अन्य सत्र साइन आउट कर दिए गए, क्योंकि एक समय में केवल एक सत्र सक्रिय रह सकता है। यदि यह आप नहीं थे, तो तुरंत धोखाधड़ी रिपोर्ट करें।`,
    notYou: "यह लॉगिन पहचान में नहीं आया?",
    reportFraud: "धोखाधड़ी रिपोर्ट करें",
    reasons: {
      expired: "5 मिनट तक कोई गतिविधि न होने के कारण आपको साइन आउट कर दिया गया। कृपया फिर से साइन इन करें।",
      superseded: "आपको साइन आउट कर दिया गया क्योंकि आपने किसी अन्य डिवाइस या ब्राउज़र पर साइन इन किया।",
      logged_out: "आप सुरक्षित रूप से लॉग आउट हो गए हैं। साझा कंप्यूटर पर ब्राउज़र बंद कर दें।",
    } as Record<string, string>,
    demo: "डेमो खाते (कृत्रिम डेटा)",
    demoHint: (pw: string) => `सभी डेमो यूज़र का पासवर्ड ${pw} है। केवल इस डेमो वातावरण में उपलब्ध।`,
    use: "उपयोग करें",
    sessionGone: "आपका साइन-इन समाप्त हो गया है। फिर से पासवर्ड दर्ज करें।",
    risk: "हमें एक असामान्य साइन-इन दिखा। कृपया अतिरिक्त जाँच पूरी करें।",
  },
};

/** Where to go after sign-in: a safe `next` inside the user's own area, else the area's home. */
function destination(userType: string, next: string | null): string {
  const home = userType === "staff" ? "/staff" : userType === "corporate" ? "/portal/corporate" : "/portal";
  if (next && next.startsWith("/") && !next.startsWith("//") && !next.includes("\\")) {
    if (userType === "staff" && (next === "/staff" || next.startsWith("/staff/"))) return next;
    if (userType === "retail" && (next === "/portal" || next.startsWith("/portal/")) && !next.startsWith("/portal/corporate")) return next;
    if (userType === "corporate" && ["/portal/corporate", "/portal/profile", "/portal/privacy"].some((p) => next === p || next.startsWith(p + "/"))) return next;
  }
  return home;
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoadingState rows={4} />}>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner() {
  const params = useParams<{ locale: string }>();
  const rawLocale = params?.locale;
  const locale: Locale = isLocale(rawLocale) ? rawLocale : "en";
  const tx = T[locale];
  const search = useSearchParams();
  const router = useRouter();
  const { env } = useEnvironment();
  const next = search.get("next");
  const reason = search.get("reason");

  const [step, setStep] = useState<0 | 1 | 2>(0);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [fieldErr, setFieldErr] = useState<{ email?: string; password?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [forgotOpen, setForgotOpen] = useState(false);

  const [factors, setFactors] = useState<Factor[] | null>(null);
  const [riskHigh, setRiskHigh] = useState(false);
  const [factor, setFactor] = useState("");
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [code, setCode] = useState("");
  const [codeErr, setCodeErr] = useState<string | null>(null);
  const [mfaError, setMfaError] = useState<string | null>(null);
  const [result, setResult] = useState<VerifyRes | null>(null);
  const [resuming, setResuming] = useState(false);

  // ?step=mfa with a password-level token: continue at the second factor.
  useEffect(() => {
    if (search.get("step") !== "mfa" || !getToken()) return;
    let alive = true;
    setResuming(true);
    api<{ items: Factor[] }>("/auth/mfa/factors")
      .then((r) => {
        if (!alive) return;
        setFactors(r.items);
        setFactor((r.items.find((f) => f.is_primary) ?? r.items[0])?.factor_type ?? "sms_otp");
        setStep(1);
      })
      .catch(() => {
        if (!alive) return;
        clearToken();
        setFormError(tx.sessionGone);
      })
      .finally(() => alive && setResuming(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitPassword = async (e: FormEvent) => {
    e.preventDefault();
    const errs: { email?: string; password?: string } = {};
    if (!email.trim()) errs.email = tx.required;
    if (!password) errs.password = tx.required;
    setFieldErr(errs);
    setFormError(null);
    setLockedUntil(null);
    if (errs.email || errs.password) {
      document.getElementById(errs.email ? "login-email" : "login-password")?.focus();
      return;
    }
    setBusy(true);
    try {
      const res = await api<LoginRes>("/auth/login", { method: "POST", auth: false, body: { email: email.trim(), password } });
      setToken(res.access_token); // password-level token; replaced by the full token after MFA
      setFactors(res.available_factors);
      setRiskHigh(res.risk_level === "high");
      setFactor((res.available_factors.find((f) => f.is_primary) ?? res.available_factors[0])?.factor_type ?? "sms_otp");
      setPassword("");
      setStep(1);
    } catch (err) {
      if (err instanceof ApiError) {
        const d = (err.details as { detail?: { attempts_remaining?: number | null; locked_until?: string } } | null)?.detail;
        if (err.status === 423) {
          setLockedUntil(d?.locked_until ?? null);
        } else if (err.status === 401) {
          const left = d?.attempts_remaining;
          setFormError(typeof left === "number" ? `${tx.wrongCreds} ${tx.attempts(left)}` : tx.wrongCreds);
        } else {
          setFormError(err.message);
        }
      } else {
        setFormError(String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const startChallenge = async () => {
    setBusy(true);
    setMfaError(null);
    try {
      const c = await api<Challenge>("/auth/mfa/challenge", { method: "POST", body: { factor_type: factor, purpose: "login" } });
      setChallenge(c);
      setCode("");
      setCodeErr(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        setStep(0);
        setFormError(tx.sessionGone);
      } else {
        setMfaError(err instanceof ApiError ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const verify = async (e: FormEvent) => {
    e.preventDefault();
    if (!challenge) return;
    if (!/^\d{6}$/.test(code.trim())) {
      setCodeErr(tx.codeHint);
      document.getElementById("login-code")?.focus();
      return;
    }
    setBusy(true);
    setCodeErr(null);
    setMfaError(null);
    try {
      const r = await api<VerifyRes>("/auth/mfa/verify", { method: "POST", body: { challenge_id: challenge.challenge_id, response: code.trim() } });
      setToken(r.access_token);
      setResult(r);
      setStep(2);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "mfa_expired" || err.status === 404) {
          setChallenge(null);
          setMfaError(err.message);
        } else if (err.code === "mfa_invalid") {
          setCodeErr(err.message);
        } else if (err.status === 401) {
          clearToken();
          setStep(0);
          setChallenge(null);
          setFormError(tx.sessionGone);
        } else {
          setMfaError(err.message);
        }
      } else {
        setMfaError(String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const startOver = () => {
    clearToken();
    setChallenge(null);
    setFactors(null);
    setStep(0);
  };

  const uniqueFactors = (factors ?? []).filter((f, i, arr) => arr.findIndex((x) => x.factor_type === f.factor_type) === i);
  const reasonText = reason ? tx.reasons[reason] : null;

  return (
    <div className="mx-auto max-w-xl">
      <PageHeader title={tx.title} description={tx.subtitle} />
      <Stepper steps={tx.steps} current={step} />

      {reasonText && step === 0 && (
        <Alert tone={reason === "logged_out" ? "success" : "warning"} className="mb-4">{reasonText}</Alert>
      )}

      {resuming && <LoadingState rows={2} />}

      {step === 0 && !resuming && (
        <Card>
          <form onSubmit={submitPassword} noValidate className="space-y-4">
            {lockedUntil !== null && (
              <Alert tone="danger" title={tx.lockedTitle}>{tx.locked(lockedUntil ? formatDateTime(lockedUntil) : "15 min")}</Alert>
            )}
            {formError && <Alert tone="danger">{formError}</Alert>}
            <TextField id="login-email" label={tx.email} value={email} onChange={(v) => { setEmail(v); setFieldErr((f) => ({ ...f, email: undefined })); }}
              autoComplete="username" inputMode="email" required error={fieldErr.email} />
            <div>
              <TextField id="login-password" label={tx.password} value={password}
                onChange={(v) => { setPassword(v); setFieldErr((f) => ({ ...f, password: undefined })); }}
                type={showPw ? "text" : "password"} autoComplete="current-password" required error={fieldErr.password}
                suffix={
                  <button type="button" className="min-h-tap font-semibold text-primary" onClick={() => setShowPw((s) => !s)}
                    aria-pressed={showPw} aria-label={showPw ? tx.hidePwLabel : tx.showPwLabel}>
                    {showPw ? tx.hidePw : tx.showPw}
                  </button>
                } />
              <button type="button" className="mt-2 min-h-tap text-sm font-medium text-primary underline underline-offset-2"
                aria-expanded={forgotOpen} aria-controls="forgot-help" onClick={() => setForgotOpen((o) => !o)}>
                {tx.forgot}
              </button>
              {forgotOpen && (
                <div id="forgot-help" className="mt-2">
                  <Alert tone="info" title={tx.forgotTitle}>
                    <p>{tx.forgotBody}</p>
                    <p className="mt-2"><TextLink href={lp(locale, "/register")}>{tx.forgotLink}</TextLink></p>
                  </Alert>
                </div>
              )}
            </div>
            <Button type="submit" fullWidth size="lg" loading={busy}>{tx.continue}</Button>
            <p className="text-sm text-fg-muted">
              {tx.newUser} <TextLink href={lp(locale, "/register")}>{tx.register}</TextLink>
            </p>
            <p className="text-sm text-fg-muted">
              {tx.noAccount} <TextLink href={lp(locale, "/open-account")}>{tx.openAccount}</TextLink>
            </p>
          </form>
        </Card>
      )}

      {step === 1 && (
        <Card>
          {riskHigh && <Alert tone="warning" className="mb-4">{tx.risk}</Alert>}
          {mfaError && <Alert tone="danger" className="mb-4">{mfaError}</Alert>}
          {!challenge ? (
            <div className="space-y-4">
              <RadioGroup name="login-factor" legend={tx.chooseFactor} layout="list" value={factor} onChange={setFactor}
                options={uniqueFactors.map((f) => ({ value: f.factor_type, label: tx.factorLabel[f.factor_type] ?? f.factor_type, description: f.label ?? undefined }))} />
              <div className="flex flex-wrap gap-2">
                <Button onClick={startChallenge} loading={busy} disabled={!factor}>{tx.sendCode}</Button>
                <Button variant="ghost" onClick={startOver}>{tx.startOver}</Button>
              </div>
            </div>
          ) : (
            <form onSubmit={verify} noValidate className="space-y-4">
              {challenge.delivery && <p className="text-fg">{challenge.delivery}</p>}
              {challenge.factor_type === "passkey" && challenge.mock && <p className="text-sm text-fg-muted">{tx.passkeyMock}</p>}
              {challenge.mock && challenge.mock_code && (
                <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted">
                  <MockBadge /> {tx.mockCode} <strong className="font-mono text-fg">{challenge.mock_code}</strong>
                </p>
              )}
              <TextField id="login-code" label={tx.code} value={code} onChange={(v) => { setCode(v.replace(/\D/g, "").slice(0, 6)); setCodeErr(null); }}
                inputMode="numeric" autoComplete="one-time-code" maxLength={6} required autoFocus error={codeErr ?? undefined} hint={tx.codeHint} />
              <div className="flex flex-wrap gap-2">
                <Button type="submit" loading={busy}>{tx.verify}</Button>
                <Button variant="ghost" onClick={() => { setChallenge(null); setMfaError(null); }} disabled={busy}>{tx.useAnother}</Button>
              </div>
            </form>
          )}
        </Card>
      )}

      {step === 2 && result && (
        <Card>
          <CardHeader title={tx.welcome} />
          <div className="space-y-3">
            {result.last_login_at ? (
              <p className="text-fg">
                {tx.lastLogin}: <strong>{formatDateTime(result.last_login_at)}</strong>
                {result.last_login_ip && <> {tx.fromIp} <span className="font-mono">{result.last_login_ip}</span></>}
              </p>
            ) : (
              <p className="text-fg">{tx.firstLogin}</p>
            )}
            {result.other_sessions_terminated > 0 && <Alert tone="warning">{tx.otherSessions(result.other_sessions_terminated)}</Alert>}
            <p className="text-sm text-fg-muted">
              {tx.notYou} <TextLink href={lp(locale, "/report-fraud")}>{tx.reportFraud}</TextLink>
            </p>
            <Button size="lg" fullWidth autoFocus onClick={() => router.replace(destination(result.user_type, next))}>{tx.continue}</Button>
          </div>
        </Card>
      )}

      <div className="mt-4">
        <Alert tone="info" title={tx.tipTitle}>{tx.tip}</Alert>
      </div>

      {step === 0 && env?.demo_accounts && env.demo_accounts.length > 0 && (
        <details className="mt-4 rounded-lg border border-border bg-surface">
          <summary className="flex min-h-tap cursor-pointer items-center gap-2 px-4 py-3 font-medium text-fg hover:bg-surface-2">
            <MockBadge label="DEMO" /> {tx.demo}
          </summary>
          <div className="px-4 pb-4">
            {env.demo_password && <p className="mb-3 text-sm text-fg-muted">{tx.demoHint(env.demo_password)}</p>}
            <ul className="divide-y divide-border">
              {env.demo_accounts.map((a) => (
                <li key={a.email} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span className="min-w-0">
                    <span className="block text-sm font-medium text-fg">{a.role}</span>
                    <span className="block break-all font-mono text-xs text-fg-muted">{a.email}</span>
                  </span>
                  <Button size="sm" variant="secondary" aria-label={`${tx.use} ${a.role}`}
                    onClick={() => {
                      setEmail(a.email);
                      setPassword(env.demo_password ?? "");
                      setFieldErr({});
                      document.getElementById("login-email")?.focus();
                    }}>
                    {tx.use}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        </details>
      )}
    </div>
  );
}

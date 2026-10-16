"use client";

import { useParams, useRouter } from "next/navigation";
import { FormEvent, useMemo, useState } from "react";
import { OtpRequest } from "@/components/OtpRequest";
import {
  Alert, Button, Card, CardHeader, CopyButton, cx, MockBadge, PageHeader, RadioGroup, Stepper, TextField, TextLink,
} from "@/components/ui";
import { api, ApiError, setToken } from "@/lib/api";
import { isEmail, isMobile, isOtp, useForm } from "@/lib/form";
import { isLocale, Locale, lp } from "@/lib/i18n";

type RegisterRes = { access_token: string; scope: string; user_id: string; next_step: string; mock: unknown };
type Challenge = { challenge_id: string; factor_type: string; delivery?: string; mock: boolean; mock_code?: string };
type FactorType = "passkey" | "totp" | "push";

const T = {
  en: {
    title: "Register for internet banking",
    subtitle: "Use your account number and the mobile number registered with the bank.",
    steps: ["Account details", "Security factor", "Verify & sign in"],
    accountNumber: "Account number",
    accountHint: "6 to 20 digits, as printed on your passbook or welcome letter",
    mobile: "Registered mobile number",
    otp: "OTP",
    email: "Email (this becomes your User ID)",
    password: "Create password",
    confirm: "Confirm password",
    show: "Show",
    hide: "Hide",
    strength: "Password strength",
    levels: ["Too weak", "Weak", "Fair", "Good", "Strong"],
    rules: { len: "At least 8 characters", mix: "Letters and at least one number", case: "Upper and lower case letters (recommended)", sym: "A symbol such as @ # $ (recommended)" },
    errAccount: "Enter your account number (6 to 20 digits)",
    errMobile: "Enter a valid mobile number",
    errOtp: "Enter the 6-digit OTP",
    errEmail: "Enter a valid email address",
    errPassword: "Use at least 8 characters with letters and a number",
    errConfirm: "Passwords do not match",
    submit: "Continue",
    already: "Internet banking is already registered for this email or account.",
    alreadyLink: "Sign in instead",
    factorTitle: "Add a second factor",
    factorDesc: "Every sign-in needs your password and a second factor. Passkeys are the safest choice: nothing to type and they cannot be phished.",
    factorLegend: "Choose your second factor",
    passkey: "Passkey (recommended)",
    passkeyDesc: "Use your device's fingerprint, face or screen lock.",
    totp: "Authenticator app",
    totpDesc: "Google Authenticator, Microsoft Authenticator or similar.",
    push: "Push approval",
    pushDesc: "Approve sign-ins on your registered phone.",
    createPasskey: "Create passkey",
    passkeySimulated: "Your browser could not create a passkey here, so a simulated passkey credential was generated for this demo environment.",
    passkeyCreated: "Passkey created on this device.",
    passkeyUnsupported: "This browser does not support passkeys. A simulated credential is used in this demo environment.",
    totpStep1: "Add this key to your authenticator app (or scan the QR code in the live app):",
    secret: "Setup key",
    otpauth: "Setup link",
    totpStep2: "Enter the 6-digit code your app shows",
    deviceLabel: "Device name",
    deviceHint: "For example: My Android phone",
    pushCode: "Approval code shown on your device",
    enrol: "Save and continue",
    skip: "Skip for now and use SMS OTP",
    mockHint: "Mock authenticator: use code",
    verifyTitle: "Verify your sign-in",
    verifyDesc: "Confirm with your second factor to finish and open internet banking.",
    sendCode: "Send verification",
    code: "Verification code",
    verify: "Verify and open internet banking",
    errCode: "Enter the 6-digit code",
    factorAdded: "Second factor added.",
    haveLogin: "Already registered?",
    login: "Login",
  },
  hi: {
    title: "इंटरनेट बैंकिंग के लिए पंजीकरण",
    subtitle: "अपना खाता संख्या और बैंक में पंजीकृत मोबाइल नंबर उपयोग करें।",
    steps: ["खाता विवरण", "सुरक्षा फ़ैक्टर", "सत्यापन और साइन इन"],
    accountNumber: "खाता संख्या",
    accountHint: "6 से 20 अंक, जैसा पासबुक या स्वागत पत्र में छपा है",
    mobile: "पंजीकृत मोबाइल नंबर",
    otp: "OTP",
    email: "ईमेल (यही आपकी यूज़र आईडी होगी)",
    password: "पासवर्ड बनाएँ",
    confirm: "पासवर्ड की पुष्टि करें",
    show: "दिखाएँ",
    hide: "छिपाएँ",
    strength: "पासवर्ड की मज़बूती",
    levels: ["बहुत कमज़ोर", "कमज़ोर", "ठीक", "अच्छा", "मज़बूत"],
    rules: { len: "कम से कम 8 अक्षर", mix: "अक्षर और कम से कम एक अंक", case: "बड़े और छोटे अक्षर (अनुशंसित)", sym: "@ # $ जैसा कोई चिह्न (अनुशंसित)" },
    errAccount: "अपना खाता संख्या दर्ज करें (6 से 20 अंक)",
    errMobile: "सही मोबाइल नंबर दर्ज करें",
    errOtp: "6 अंकों का OTP दर्ज करें",
    errEmail: "सही ईमेल पता दर्ज करें",
    errPassword: "कम से कम 8 अक्षर, जिनमें अक्षर और एक अंक हो",
    errConfirm: "पासवर्ड मेल नहीं खाते",
    submit: "आगे बढ़ें",
    already: "इस ईमेल या खाते के लिए इंटरनेट बैंकिंग पहले से पंजीकृत है।",
    alreadyLink: "इसके बजाय साइन इन करें",
    factorTitle: "दूसरा फ़ैक्टर जोड़ें",
    factorDesc: "हर साइन-इन के लिए पासवर्ड और दूसरा फ़ैक्टर ज़रूरी है। पासकी सबसे सुरक्षित है: कुछ टाइप नहीं करना होता और इसे फ़िश नहीं किया जा सकता।",
    factorLegend: "अपना दूसरा फ़ैक्टर चुनें",
    passkey: "पासकी (अनुशंसित)",
    passkeyDesc: "अपने डिवाइस के फ़िंगरप्रिंट, फ़ेस या स्क्रीन लॉक का उपयोग करें।",
    totp: "ऑथेंटिकेटर ऐप",
    totpDesc: "Google Authenticator, Microsoft Authenticator या इसी तरह का ऐप।",
    push: "पुश स्वीकृति",
    pushDesc: "अपने पंजीकृत फ़ोन पर साइन-इन स्वीकृत करें।",
    createPasskey: "पासकी बनाएँ",
    passkeySimulated: "आपका ब्राउज़र यहाँ पासकी नहीं बना सका, इसलिए इस डेमो वातावरण के लिए एक सिम्युलेटेड पासकी क्रेडेंशियल बनाया गया।",
    passkeyCreated: "इस डिवाइस पर पासकी बन गई।",
    passkeyUnsupported: "यह ब्राउज़र पासकी का समर्थन नहीं करता। इस डेमो वातावरण में सिम्युलेटेड क्रेडेंशियल उपयोग होगा।",
    totpStep1: "यह कुंजी अपने ऑथेंटिकेटर ऐप में जोड़ें (लाइव ऐप में QR कोड स्कैन करें):",
    secret: "सेटअप कुंजी",
    otpauth: "सेटअप लिंक",
    totpStep2: "आपका ऐप जो 6 अंकों का कोड दिखाए, वह दर्ज करें",
    deviceLabel: "डिवाइस का नाम",
    deviceHint: "उदाहरण: मेरा Android फ़ोन",
    pushCode: "आपके डिवाइस पर दिखाया गया स्वीकृति कोड",
    enrol: "सहेजें और आगे बढ़ें",
    skip: "अभी छोड़ें और SMS OTP उपयोग करें",
    mockHint: "मॉक ऑथेंटिकेटर: कोड",
    verifyTitle: "अपने साइन-इन की पुष्टि करें",
    verifyDesc: "पूरा करने और इंटरनेट बैंकिंग खोलने के लिए अपने दूसरे फ़ैक्टर से पुष्टि करें।",
    sendCode: "सत्यापन भेजें",
    code: "सत्यापन कोड",
    verify: "सत्यापित करें और इंटरनेट बैंकिंग खोलें",
    errCode: "6 अंकों का कोड दर्ज करें",
    factorAdded: "दूसरा फ़ैक्टर जुड़ गया।",
    haveLogin: "पहले से पंजीकृत हैं?",
    login: "लॉगिन",
  },
};

const MOCK_CODE = "123456";

function passwordScore(pw: string): number {
  if (!pw) return 0;
  let s = 0;
  if (pw.length >= 8) s++;
  if (/[A-Za-z]/.test(pw) && /\d/.test(pw)) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/[^A-Za-z0-9]/.test(pw) || pw.length >= 12) s++;
  return pw.length < 8 ? Math.min(s, 1) : s;
}

function b64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  bytes.forEach((b) => (s += String.fromCharCode(b)));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(len: number, alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"): string {
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

async function createPasskey(email: string): Promise<{ attestation: string; real: boolean; unsupported: boolean }> {
  const supported = typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials?.create;
  if (supported) {
    try {
      const cred = (await navigator.credentials.create({
        publicKey: {
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          rp: { name: "Example Bank" },
          user: { id: crypto.getRandomValues(new Uint8Array(16)), name: email, displayName: email },
          pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
          authenticatorSelection: { userVerification: "preferred", residentKey: "preferred" },
          attestation: "none",
          timeout: 60000,
        },
      })) as PublicKeyCredential | null;
      if (cred) return { attestation: "webauthn." + b64url(cred.rawId), real: true, unsupported: false };
    } catch {
      /* cancelled or not allowed here: fall back to a simulated credential below */
    }
  }
  return { attestation: "simulated-passkey." + randomString(24), real: false, unsupported: !supported };
}

type Values = { account_number: string; mobile: string; otp: string; email: string; password: string; confirm: string };

export default function RegisterPage() {
  const params = useParams<{ locale: string }>();
  const rawLocale = params?.locale;
  const locale: Locale = isLocale(rawLocale) ? rawLocale : "en";
  const tx = T[locale];
  const router = useRouter();

  const [step, setStep] = useState(0);
  const [showPw, setShowPw] = useState(false);
  const [already, setAlready] = useState(false);

  const form = useForm<Values>({
    initial: { account_number: "", mobile: "", otp: "", email: "", password: "", confirm: "" },
    validate: (v) => {
      const e: Partial<Record<keyof Values, string>> = {};
      if (!/^\d{6,20}$/.test(v.account_number.replace(/\s/g, ""))) e.account_number = tx.errAccount;
      if (!isMobile(v.mobile)) e.mobile = tx.errMobile;
      if (!isOtp(v.otp)) e.otp = tx.errOtp;
      if (!isEmail(v.email)) e.email = tx.errEmail;
      if (v.password.length < 8 || v.password.length > 128 || !/[A-Za-z]/.test(v.password) || !/\d/.test(v.password)) e.password = tx.errPassword;
      if (!v.confirm || v.confirm !== v.password) e.confirm = tx.errConfirm;
      return e;
    },
  });
  const v = form.values;
  const score = passwordScore(v.password);

  const submitDetails = form.handleSubmit(async (vals) => {
    setAlready(false);
    try {
      const res = await api<RegisterRes>("/auth/register", {
        method: "POST",
        auth: false,
        body: {
          account_number: vals.account_number.replace(/\s/g, ""),
          mobile: vals.mobile,
          otp: vals.otp,
          email: vals.email.trim(),
          password: vals.password,
          preferred_language: locale,
        },
      });
      setToken(res.access_token);
      setStep(1);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setAlready(true);
        return;
      }
      throw err;
    }
  });

  // ---------------------------------------------------------------- step 2: factor enrolment
  const [factorType, setFactorType] = useState<FactorType>("passkey");
  const [passkeyAttestation, setPasskeyAttestation] = useState<{ attestation: string; real: boolean; unsupported: boolean } | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [pushLabel, setPushLabel] = useState("");
  const [pushCode, setPushCode] = useState("");
  const [enrolErr, setEnrolErr] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [enrolled, setEnrolled] = useState<"passkey" | "totp" | "push" | "sms_otp" | null>(null);
  const secret = useMemo(() => (typeof window === "undefined" ? "" : randomString(16)), []);
  const otpauth = `otpauth://totp/ExampleBank:${encodeURIComponent(v.email.trim())}?secret=${secret}&issuer=ExampleBank&digits=6&period=30`;

  const makePasskey = async () => {
    setBusy(true);
    setEnrolErr(null);
    try {
      setPasskeyAttestation(await createPasskey(v.email.trim()));
    } finally {
      setBusy(false);
    }
  };

  const enrol = async (e: FormEvent) => {
    e.preventDefault();
    setEnrolErr(null);
    setFieldErr(null);
    let attestation = "";
    let label: string | undefined;
    if (factorType === "passkey") {
      if (!passkeyAttestation) {
        setFieldErr(tx.createPasskey);
        return;
      }
      attestation = passkeyAttestation.attestation;
      label = passkeyAttestation.real ? "Passkey on this device" : "Passkey (simulated)";
    } else if (factorType === "totp") {
      if (!/^\d{6}$/.test(totpCode)) {
        setFieldErr(tx.errCode);
        document.getElementById("reg-totp")?.focus();
        return;
      }
      attestation = totpCode;
    } else {
      if (!/^\d{6}$/.test(pushCode)) {
        setFieldErr(tx.errCode);
        document.getElementById("reg-push-code")?.focus();
        return;
      }
      attestation = pushCode;
      label = pushLabel.trim() || undefined;
    }
    setBusy(true);
    try {
      await api("/auth/mfa/factors", { method: "POST", body: { factor_type: factorType, attestation, label } });
      setEnrolled(factorType);
      setStep(2);
    } catch (err) {
      if (err instanceof ApiError && err.fieldErrors.attestation) setFieldErr(err.fieldErrors.attestation);
      else setEnrolErr(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // ---------------------------------------------------------------- step 3: verify login
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [code, setCode] = useState("");
  const [verifyErr, setVerifyErr] = useState<string | null>(null);

  const sendChallenge = async () => {
    setBusy(true);
    setVerifyErr(null);
    try {
      const c = await api<Challenge>("/auth/mfa/challenge", { method: "POST", body: { factor_type: enrolled ?? "sms_otp", purpose: "login" } });
      setChallenge(c);
      setCode("");
    } catch (err) {
      setVerifyErr(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const verifyLogin = async (e: FormEvent) => {
    e.preventDefault();
    if (!challenge) return;
    if (!/^\d{6}$/.test(code)) {
      setVerifyErr(tx.errCode);
      document.getElementById("reg-code")?.focus();
      return;
    }
    setBusy(true);
    setVerifyErr(null);
    try {
      const r = await api<{ access_token: string }>("/auth/mfa/verify", { method: "POST", body: { challenge_id: challenge.challenge_id, response: code } });
      setToken(r.access_token);
      router.replace("/portal");
    } catch (err) {
      if (err instanceof ApiError && err.code === "mfa_expired") setChallenge(null);
      setVerifyErr(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const ruleItems = [
    { ok: v.password.length >= 8, label: tx.rules.len },
    { ok: /[A-Za-z]/.test(v.password) && /\d/.test(v.password), label: tx.rules.mix },
    { ok: /[a-z]/.test(v.password) && /[A-Z]/.test(v.password), label: tx.rules.case },
    { ok: /[^A-Za-z0-9]/.test(v.password), label: tx.rules.sym },
  ];

  return (
    <div className="mx-auto max-w-2xl">
      <PageHeader title={tx.title} description={tx.subtitle} />
      <Stepper steps={tx.steps} current={step} />

      {step === 0 && (
        <Card>
          <form onSubmit={submitDetails} noValidate className="space-y-4">
            {already && (
              <Alert tone="warning" title={tx.already}>
                <TextLink href={lp(locale, "/login")}>{tx.alreadyLink}</TextLink>
              </Alert>
            )}
            {form.formError && <Alert tone="danger">{form.formError}</Alert>}
            <TextField id="account_number" label={tx.accountNumber} value={v.account_number} hint={tx.accountHint}
              onChange={(x) => form.set("account_number", x.replace(/[^\d\s]/g, "").slice(0, 24))} onBlur={() => form.blur("account_number")}
              inputMode="numeric" autoComplete="off" required error={form.errors.account_number} />
            <TextField id="mobile" label={tx.mobile} value={v.mobile} onChange={(x) => form.set("mobile", x)} onBlur={() => form.blur("mobile")}
              type="tel" inputMode="tel" autoComplete="tel" required error={form.errors.mobile} />
            <OtpRequest mobile={v.mobile} purpose="registration" locale={locale} />
            <TextField id="otp" label={tx.otp} value={v.otp} onChange={(x) => form.set("otp", x.replace(/\D/g, "").slice(0, 6))}
              onBlur={() => form.blur("otp")} inputMode="numeric" autoComplete="one-time-code" maxLength={6} required error={form.errors.otp} />
            <TextField id="email" label={tx.email} value={v.email} onChange={(x) => form.set("email", x)} onBlur={() => form.blur("email")}
              type="email" autoComplete="email" required error={form.errors.email} />
            <div>
              <TextField id="password" label={tx.password} value={v.password} onChange={(x) => form.set("password", x)}
                onBlur={() => form.blur("password")} type={showPw ? "text" : "password"} autoComplete="new-password" required
                error={form.errors.password}
                suffix={
                  <button type="button" className="min-h-tap font-semibold text-primary" aria-pressed={showPw} onClick={() => setShowPw((s) => !s)}>
                    {showPw ? tx.hide : tx.show}
                  </button>
                } />
              <div className="mt-2" aria-live="polite">
                <div className="flex items-center justify-between text-xs text-fg-muted">
                  <span>{tx.strength}</span>
                  <span className="font-semibold text-fg">{tx.levels[score]}</span>
                </div>
                <div className="mt-1 grid grid-cols-4 gap-1" aria-hidden="true">
                  {[1, 2, 3, 4].map((i) => (
                    <span key={i} className={cx("h-1.5 rounded-pill", i <= score ? (score <= 1 ? "bg-danger" : score === 2 ? "bg-warning" : "bg-success") : "bg-surface-3")} />
                  ))}
                </div>
                <ul className="mt-2 space-y-0.5 text-xs">
                  {ruleItems.map((r) => (
                    <li key={r.label} className={r.ok ? "text-success" : "text-fg-muted"}>
                      <span aria-hidden="true">{r.ok ? "✓" : "○"}</span> {r.label}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            <TextField id="confirm" label={tx.confirm} value={v.confirm} onChange={(x) => form.set("confirm", x)} onBlur={() => form.blur("confirm")}
              type={showPw ? "text" : "password"} autoComplete="new-password" required error={form.errors.confirm} />
            <Button type="submit" size="lg" fullWidth loading={form.submitting}>{tx.submit}</Button>
            <p className="text-sm text-fg-muted">
              {tx.haveLogin} <TextLink href={lp(locale, "/login")}>{tx.login}</TextLink>
            </p>
          </form>
        </Card>
      )}

      {step === 1 && (
        <Card>
          <CardHeader title={tx.factorTitle} description={tx.factorDesc} />
          <form onSubmit={enrol} noValidate className="space-y-4">
            {enrolErr && <Alert tone="danger">{enrolErr}</Alert>}
            <RadioGroup name="factor" legend={tx.factorLegend} value={factorType} layout="list"
              onChange={(x) => { setFactorType(x as FactorType); setFieldErr(null); }}
              options={[
                { value: "passkey", label: tx.passkey, description: tx.passkeyDesc },
                { value: "totp", label: tx.totp, description: tx.totpDesc },
                { value: "push", label: tx.push, description: tx.pushDesc },
              ]} />

            {factorType === "passkey" && (
              <div className="space-y-3 rounded-md bg-surface-2 p-4">
                <Button variant="secondary" onClick={makePasskey} loading={busy}>{tx.createPasskey}</Button>
                {passkeyAttestation && (
                  <Alert tone={passkeyAttestation.real ? "success" : "warning"}>
                    {passkeyAttestation.real ? tx.passkeyCreated : passkeyAttestation.unsupported ? tx.passkeyUnsupported : tx.passkeySimulated}
                    {!passkeyAttestation.real && <span className="ml-2"><MockBadge label="SIMULATED" /></span>}
                  </Alert>
                )}
                {fieldErr && <p className="text-sm font-medium text-danger">{fieldErr}</p>}
              </div>
            )}

            {factorType === "totp" && (
              <div className="space-y-3 rounded-md bg-surface-2 p-4">
                <p className="text-sm text-fg">{tx.totpStep1}</p>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium uppercase text-fg-muted">{tx.secret}</span>
                  <code className="break-all rounded bg-surface px-2 py-1 font-mono text-sm">{secret.replace(/(.{4})/g, "$1 ").trim()}</code>
                  <CopyButton value={secret} />
                </div>
                <div className="min-w-0">
                  <span className="text-xs font-medium uppercase text-fg-muted">{tx.otpauth}</span>
                  <code className="mt-1 block break-all rounded bg-surface px-2 py-1 font-mono text-xs">{otpauth}</code>
                </div>
                <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted"><MockBadge /> {tx.mockHint} <strong className="font-mono text-fg">{MOCK_CODE}</strong></p>
                <TextField id="reg-totp" label={tx.totpStep2} value={totpCode} onChange={(x) => { setTotpCode(x.replace(/\D/g, "").slice(0, 6)); setFieldErr(null); }}
                  inputMode="numeric" autoComplete="one-time-code" maxLength={6} required error={fieldErr ?? undefined} />
              </div>
            )}

            {factorType === "push" && (
              <div className="space-y-3 rounded-md bg-surface-2 p-4">
                <TextField id="reg-push-label" label={tx.deviceLabel} hint={tx.deviceHint} value={pushLabel} onChange={setPushLabel} maxLength={60} />
                <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted"><MockBadge /> {tx.mockHint} <strong className="font-mono text-fg">{MOCK_CODE}</strong></p>
                <TextField id="reg-push-code" label={tx.pushCode} value={pushCode} onChange={(x) => { setPushCode(x.replace(/\D/g, "").slice(0, 6)); setFieldErr(null); }}
                  inputMode="numeric" maxLength={6} required error={fieldErr ?? undefined} />
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <Button type="submit" loading={busy}>{tx.enrol}</Button>
              <Button variant="ghost" onClick={() => { setEnrolled("sms_otp"); setStep(2); }} disabled={busy}>{tx.skip}</Button>
            </div>
          </form>
        </Card>
      )}

      {step === 2 && (
        <Card>
          <CardHeader title={tx.verifyTitle} description={tx.verifyDesc} />
          {enrolled && enrolled !== "sms_otp" && <Alert tone="success" className="mb-4">{tx.factorAdded}</Alert>}
          {verifyErr && <Alert tone="danger" className="mb-4">{verifyErr}</Alert>}
          {!challenge ? (
            <Button onClick={sendChallenge} loading={busy}>{tx.sendCode}</Button>
          ) : (
            <form onSubmit={verifyLogin} noValidate className="space-y-4">
              {challenge.delivery && <p className="text-fg">{challenge.delivery}</p>}
              {challenge.mock && challenge.mock_code && (
                <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted"><MockBadge /> {tx.mockHint} <strong className="font-mono text-fg">{challenge.mock_code}</strong></p>
              )}
              <TextField id="reg-code" label={tx.code} value={code} onChange={(x) => setCode(x.replace(/\D/g, "").slice(0, 6))}
                inputMode="numeric" autoComplete="one-time-code" maxLength={6} required autoFocus />
              <Button type="submit" loading={busy}>{tx.verify}</Button>
            </form>
          )}
        </Card>
      )}
    </div>
  );
}

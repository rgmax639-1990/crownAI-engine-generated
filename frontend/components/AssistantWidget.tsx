"use client";

/**
 * Floating assistant (bottom-right). Answers only from the bank's published content and cites
 * the source pages; refuses anything involving OTP / PIN / CVV / password; offers a human
 * hand-off with a queue position. Messages are stored PII-redacted by the backend.
 */
import Link from "next/link";
import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Locale } from "@/lib/i18n";
import { Button, Spinner, cx } from "./ui";

type ChatLocale = "en" | "hi" | "hinglish";
type Citation = { title: string; href: string; version_id: string; content_type?: string };
type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  at?: string;
  citations?: Citation[];
  handoff_suggested?: boolean;
  safety?: boolean;
  system?: boolean;
  failed?: boolean;
};
type SessionOut = { session_id: string; locale: ChatLocale; disclaimer: string; quick_replies: string[]; messages: ChatMessage[] };
type MessageOut = {
  user_message: ChatMessage;
  message: ChatMessage;
  quick_replies: string[];
  handoff_suggested: boolean;
  safety: boolean;
  redacted: boolean;
};
type HandoffOut = { handoff_status: string; queue_position: number; estimated_wait_minutes: number; message: ChatMessage };

const TX = {
  en: {
    open: "Ask the assistant",
    title: "Banking assistant",
    close: "Close assistant",
    language: "Assistant language",
    disclaimer: "The bank never asks for your OTP, PIN, CVV or password. Never share them with anyone, including here.",
    placeholder: "Ask about rates, accounts, cards…",
    send: "Send",
    human: "Talk to a human",
    queued: "In queue",
    starting: "Starting a secure chat…",
    startError: "The assistant is unavailable right now.",
    retry: "Try again",
    sendError: "Your message was not sent.",
    sources: "Sources",
    redacted: "We removed personal details from your message for your safety.",
    tooLong: "Keep your question within 500 characters.",
    typing: "Assistant is typing…",
    you: "You",
    bot: "Assistant",
  },
  hi: {
    open: "सहायक से पूछें",
    title: "बैंकिंग सहायक",
    close: "सहायक बंद करें",
    language: "सहायक की भाषा",
    disclaimer: "बैंक कभी भी आपका OTP, PIN, CVV या पासवर्ड नहीं माँगता। इन्हें किसी से भी साझा न करें, यहाँ भी नहीं।",
    placeholder: "दरें, खाते, कार्ड के बारे में पूछें…",
    send: "भेजें",
    human: "अधिकारी से बात करें",
    queued: "कतार में",
    starting: "सुरक्षित चैट शुरू हो रही है…",
    startError: "सहायक अभी उपलब्ध नहीं है।",
    retry: "फिर से कोशिश करें",
    sendError: "आपका संदेश नहीं भेजा जा सका।",
    sources: "स्रोत",
    redacted: "आपकी सुरक्षा के लिए हमने आपके संदेश से व्यक्तिगत जानकारी हटा दी है।",
    tooLong: "अपना प्रश्न 500 अक्षरों के भीतर रखें।",
    typing: "सहायक लिख रहा है…",
    you: "आप",
    bot: "सहायक",
  },
} as const;

const LANG_OPTIONS: Array<{ value: ChatLocale; label: string; aria: string }> = [
  { value: "en", label: "EN", aria: "English" },
  { value: "hi", label: "हिं", aria: "हिन्दी" },
  { value: "hinglish", label: "Hinglish", aria: "Hinglish" },
];

export function AssistantWidget({ locale, authenticated }: { locale: Locale; authenticated?: boolean }) {
  const tx = TX[locale] ?? TX.en;
  const auth = authenticated ? ("user" as const) : (false as const);
  const [open, setOpen] = useState(false);
  const [chatLocale, setChatLocale] = useState<ChatLocale>(locale);
  const [session, setSession] = useState<SessionOut | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [quick, setQuick] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<{ text: string; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<HandoffOut | null>(null);
  const [handingOff, setHandingOff] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => setChatLocale(locale), [locale]);

  const start = useCallback(
    async (lang: ChatLocale) => {
      setStarting(true);
      setStartError(null);
      setHandoff(null);
      try {
        const s = await api<SessionOut>("/assistant/sessions", { method: "POST", auth, body: { locale: lang } });
        setSession(s);
        setMessages(s.messages ?? []);
        setQuick(s.quick_replies ?? []);
      } catch (e) {
        setStartError(e instanceof ApiError ? e.message : tx.startError);
      } finally {
        setStarting(false);
      }
    },
    [auth, tx.startError],
  );

  useEffect(() => {
    if (open && !session && !starting && !startError) void start(chatLocale);
  }, [open, session, starting, startError, start, chatLocale]);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 0);
  }, [open, session]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  const close = () => {
    setOpen(false);
    setTimeout(() => openerRef.current?.focus(), 0);
  };

  const changeLanguage = (lang: ChatLocale) => {
    if (lang === chatLocale) return;
    setChatLocale(lang);
    // A new session greets in the new language; the old one stays on the server (redacted).
    setSession(null);
    setMessages([]);
    setStartError(null);
    setHandoff(null);
    // The effect above starts the new session.
  };

  const send = async (raw: string) => {
    const value = raw.trim();
    if (!value || !session || sending) return;
    if (value.length > 500) {
      setSendError({ text: value, message: tx.tooLong });
      return;
    }
    setSending(true);
    setSendError(null);
    setNotice(null);
    setText("");
    const tempId = `pending-${Date.now()}`;
    setMessages((m) => [...m, { id: tempId, role: "user", text: value }]);
    try {
      const r = await api<MessageOut>(`/assistant/sessions/${session.session_id}/messages`, {
        method: "POST",
        auth,
        body: { text: value, locale: chatLocale },
      });
      setMessages((m) => [...m.filter((x) => x.id !== tempId), r.user_message, r.message]);
      setQuick(r.quick_replies ?? []);
      if (r.redacted) setNotice(tx.redacted);
    } catch (e) {
      setMessages((m) => m.map((x) => (x.id === tempId ? { ...x, failed: true } : x)));
      setSendError({ text: value, message: e instanceof ApiError ? e.message : tx.sendError });
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) setSession(null);
    } finally {
      setSending(false);
    }
  };

  const retrySend = () => {
    if (!sendError) return;
    const value = sendError.text;
    setMessages((m) => m.filter((x) => !(x.failed && x.text === value)));
    setSendError(null);
    void send(value);
  };

  const requestHuman = async () => {
    if (!session || handingOff) return;
    setHandingOff(true);
    setSendError(null);
    try {
      const r = await api<HandoffOut>(`/assistant/sessions/${session.session_id}/handoff`, { method: "POST", auth });
      setHandoff(r);
      setMessages((m) => [...m, r.message]);
    } catch (e) {
      setSendError({ text: "", message: e instanceof ApiError ? e.message : tx.sendError });
    } finally {
      setHandingOff(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void send(text);
  };

  const onInputKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(text);
    }
  };

  const onPanelKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") close();
  };

  const isHumanReply = (q: string) => /human|insaan|अधिकारी|इंसान/i.test(q);
  const lastBot = [...messages].reverse().find((m) => m.role === "assistant");

  return (
    <>
      {!open && (
        <button
          ref={openerRef}
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          className="fixed bottom-4 right-4 z-40 inline-flex min-h-tap items-center gap-2 rounded-pill bg-primary px-4 text-sm font-semibold text-primary-fg shadow-lg hover:bg-primary-hover"
        >
          <span aria-hidden="true">💬</span>
          <span>{tx.open}</span>
        </button>
      )}

      {open && (
        <div
          role="dialog"
          aria-modal="false"
          aria-labelledby="assistant-title"
          lang={chatLocale === "hi" ? "hi" : "en"}
          onKeyDown={onPanelKey}
          className="fixed inset-x-0 bottom-0 z-50 flex h-[85vh] flex-col rounded-t-xl border border-border bg-surface text-fg shadow-lg sm:inset-x-auto sm:bottom-4 sm:right-4 sm:h-[36rem] sm:max-h-[calc(100vh-2rem)] sm:w-[24rem] sm:rounded-xl"
        >
          <div className="flex items-center gap-2 border-b border-border p-3">
            <h2 id="assistant-title" className="min-w-0 flex-1 truncate font-semibold">
              {tx.title}
            </h2>
            <div role="group" aria-label={tx.language} className="flex rounded-md border border-border">
              {LANG_OPTIONS.map((o) => (
                <button key={o.value} type="button" aria-pressed={chatLocale === o.value} aria-label={o.aria}
                  onClick={() => changeLanguage(o.value)}
                  className={cx("min-h-9 px-2 text-xs font-semibold first:rounded-l-md last:rounded-r-md",
                    chatLocale === o.value ? "bg-primary text-primary-fg" : "text-fg-muted hover:bg-surface-2")}>
                  {o.label}
                </button>
              ))}
            </div>
            <button type="button" onClick={close} aria-label={tx.close}
              className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-xl text-fg-muted hover:bg-surface-2 hover:text-fg">
              ×
            </button>
          </div>

          <p className="border-b border-border bg-warning-soft px-3 py-2 text-xs font-medium text-fg">
            <span aria-hidden="true">⚠ </span>
            {tx.disclaimer}
          </p>

          <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto p-3" aria-live="polite" aria-relevant="additions" aria-busy={sending}>
            {starting && <Spinner label={tx.starting} />}
            {startError && (
              <div role="alert" className="space-y-2 rounded-lg border border-danger bg-danger-soft p-3 text-sm">
                <p>{startError}</p>
                <Button size="sm" variant="secondary" onClick={() => { setStartError(null); void start(chatLocale); }}>
                  {tx.retry}
                </Button>
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id} className={cx("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div className={cx("max-w-[85%] rounded-lg px-3 py-2 text-sm",
                  m.role === "user" ? "bg-primary text-primary-fg" : m.safety ? "border border-danger bg-danger-soft text-fg" : "bg-surface-2 text-fg",
                  m.failed && "opacity-60")}>
                  <span className="sr-only">{m.role === "user" ? tx.you : tx.bot}: </span>
                  <p className="whitespace-pre-line break-words">{m.text}</p>
                  {m.citations && m.citations.length > 0 && (
                    <div className="mt-2 border-t border-border pt-2">
                      <p className="text-xs font-semibold text-fg-muted">{tx.sources}</p>
                      <ul className="mt-1 space-y-1">
                        {m.citations.map((c) => (
                          <li key={c.version_id + c.href}>
                            <Link href={c.href} className="text-xs font-medium text-primary underline underline-offset-2 hover:no-underline">
                              {c.title}
                            </Link>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {sending && <Spinner size="sm" label={tx.typing} />}
            {handoff && (
              <p className="rounded-md bg-info-soft px-3 py-2 text-sm">
                {tx.queued}: <strong>#{handoff.queue_position}</strong> · ~{handoff.estimated_wait_minutes} min
              </p>
            )}
          </div>

          {(notice || sendError) && (
            <div className="px-3 pb-2 text-xs" aria-live="assertive">
              {notice && <p className="text-fg-muted">{notice}</p>}
              {sendError && (
                <p className="flex flex-wrap items-center gap-2 font-medium text-danger">
                  {sendError.message}
                  {sendError.text && (
                    <button type="button" onClick={retrySend} className="min-h-9 underline">
                      {tx.retry}
                    </button>
                  )}
                  {!session && !starting && (
                    <button type="button" onClick={() => void start(chatLocale)} className="min-h-9 underline">
                      {tx.retry}
                    </button>
                  )}
                </p>
              )}
            </div>
          )}

          {session && quick.length > 0 && (
            <div className="flex gap-2 overflow-x-auto px-3 pb-2" aria-label="Suggestions">
              {quick.map((q) => (
                <button key={q} type="button" disabled={sending}
                  onClick={() => (isHumanReply(q) ? void requestHuman() : void send(q))}
                  className="min-h-9 shrink-0 rounded-pill border border-primary px-3 text-xs font-semibold text-primary hover:bg-primary-soft disabled:opacity-60">
                  {q}
                </button>
              ))}
            </div>
          )}

          <form onSubmit={onSubmit} className="flex items-end gap-2 border-t border-border p-3">
            <label htmlFor="assistant-input" className="sr-only">{tx.placeholder}</label>
            <textarea id="assistant-input" ref={inputRef} rows={1} maxLength={500} value={text} disabled={!session}
              onChange={(e) => setText(e.target.value)} onKeyDown={onInputKey} placeholder={tx.placeholder}
              className="min-h-tap flex-1 resize-none rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-subtle" />
            <Button type="submit" size="sm" loading={sending} disabled={!session || !text.trim()}>
              {tx.send}
            </Button>
          </form>
          <div className="flex justify-between gap-2 px-3 pb-3">
            <Button variant={lastBot?.handoff_suggested ? "primary" : "ghost"} size="sm" onClick={() => void requestHuman()}
              loading={handingOff} disabled={!session || !!handoff}>
              {handoff ? `${tx.queued} #${handoff.queue_position}` : tx.human}
            </Button>
          </div>
        </div>
      )}
    </>
  );
}

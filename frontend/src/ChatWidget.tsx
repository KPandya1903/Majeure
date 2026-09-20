import { useEffect, useRef, useState } from "react";
import { api } from "./api";

const EXAMPLES = [
  "Which shipments go through Hormuz?",
  "What's our total open exposure?",
  "Which decisions need approval?",
];

/** Ask PortGuard as a persistent launcher rather than a tab, so a question can be asked from
 * whatever screen prompted it without losing that screen. Answers come from the same grounded
 * /api/ask endpoint (shipments/decisions/events/traffic as JSON context, cited). */
export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [chat, setChat] = useState<{ role: "user" | "assistant"; text: string }[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [chat, busy]);

  async function send(text?: string) {
    const question = (text ?? q).trim();
    if (!question || busy) return;
    setChat((c) => [...c, { role: "user", text: question }]);
    setQ("");
    setBusy(true);
    try {
      const { answer } = await api.ask(question);
      setChat((c) => [...c, { role: "assistant", text: answer }]);
    } catch {
      setChat((c) => [...c, { role: "assistant", text: "Couldn't reach PortGuard's API. Try again in a moment." }]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="pg-chat-launcher" onClick={() => setOpen(true)} aria-label="Ask PortGuard">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.9-.9L3 21l1.9-5A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z" />
        </svg>
        <span>Ask PortGuard</span>
      </button>
    );
  }

  return (
    <div className="pg-chat-panel" role="dialog" aria-label="Ask PortGuard">
      <div className="pg-chat-head">
        <div className="pg-chat-avatar">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 2 4 5.2v5.9c0 4.6 3.2 8.4 8 9.4 4.8-1 8-4.8 8-9.4V5.2L12 2z" />
          </svg>
        </div>
        <div style={{ flexGrow: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>Ask PortGuard</div>
          <div style={{ fontSize: 11.5, opacity: 0.8 }}>Answers from your data, with sources</div>
        </div>
        <button className="pg-chat-close" onClick={() => setOpen(false)} aria-label="Close">×</button>
      </div>

      <div className="pg-chat-body">
        {chat.length === 0 && (
          <div>
            <div className="pg-sub" style={{ marginBottom: 10 }}>Try one of these:</div>
            {EXAMPLES.map((e) => (
              <button key={e} className="pg-chat-suggest" onClick={() => send(e)}>{e}</button>
            ))}
          </div>
        )}
        {chat.map((m, i) => (
          <div key={i} className={`pg-chat-row ${m.role}`}>
            <div className="pg-chat-bubble">{m.text}</div>
          </div>
        ))}
        {busy && (
          <div className="pg-chat-row assistant">
            <div className="pg-chat-bubble">
              <span className="pg-chat-dots"><span /><span /><span /></span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="pg-chat-input">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()}
               placeholder="Ask about shipments, events, decisions…" disabled={busy} aria-label="Your question" />
        <button className="pg-chat-send" onClick={() => send()} disabled={busy || !q.trim()} aria-label="Send">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
        </button>
      </div>
    </div>
  );
}

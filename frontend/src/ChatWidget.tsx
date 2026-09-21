import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";

const EXAMPLES = [
  "Which shipments go through Hormuz?",
  "What's our total open exposure?",
  "Which decisions need approval?",
];

// Answers arrive as markdown — **bold** figures, [1](url) citations, and "- " bullets. Rendering
// them as plain text meant every citation the model produced showed up as literal "[1](https://…)"
// in the bubble and nothing was clickable, which quietly defeated the point of citing at all.
// Three constructs is the whole grammar the answers use, so this stays inline rather than pulling
// in a markdown library for 2 KB of output.
// **bold** | *italic* | [1](url). The italic arm is not optional decoration: answers write event
// names as *RED SEA TENSIONS*, and without it the asterisks render literally, which reads as a
// broken chatbot even though the answer underneath is correct. The bold arm must come first so
// `**x**` is never matched as two italics.
const INLINE_RE =
  /\*\*(.+?)\*\*|\*(?!\s)([^*\n]+?)(?<!\s)\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;

function renderInline(text: string, keyPrefix: string) {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push(<strong key={`${keyPrefix}-b${m.index}`}>{m[1]}</strong>);
    } else if (m[2] !== undefined) {
      out.push(<em key={`${keyPrefix}-i${m.index}`}>{m[2]}</em>);
    } else {
      out.push(
        <a key={`${keyPrefix}-a${m.index}`} href={m[4]} target="_blank" rel="noopener noreferrer">
          {m[3]}
        </a>,
      );
    }
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderMarkdown(text: string) {
  return text.split("\n").map((line, i) => {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    return bullet ? (
      <div key={i} style={{ display: "flex", gap: 7, marginTop: 3 }}>
        <span aria-hidden="true" style={{ opacity: 0.55 }}>•</span>
        <span>{renderInline(bullet[1], String(i))}</span>
      </div>
    ) : (
      <div key={i} style={{ marginTop: i ? 5 : 0 }}>{renderInline(line, String(i))}</div>
    );
  });
}

/** Ask Majeure as a persistent launcher rather than a tab, so a question can be asked from
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
      setChat((c) => [...c, { role: "assistant", text: "Couldn't reach Majeure's API. Try again in a moment." }]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="pg-chat-launcher" onClick={() => setOpen(true)} aria-label="Ask Majeure">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.9-.9L3 21l1.9-5A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z" />
        </svg>
        <span>Ask Majeure</span>
      </button>
    );
  }

  return (
    <div className="pg-chat-panel" role="dialog" aria-label="Ask Majeure">
      <div className="pg-chat-head">
        <div className="pg-chat-avatar">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 2 4 5.2v5.9c0 4.6 3.2 8.4 8 9.4 4.8-1 8-4.8 8-9.4V5.2L12 2z" />
          </svg>
        </div>
        <div style={{ flexGrow: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>Ask Majeure</div>
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
            <div className="pg-chat-bubble">
              {m.role === "assistant" ? renderMarkdown(m.text) : m.text}
            </div>
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

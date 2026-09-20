import { Fragment, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Decision, Draft, Shipment } from "./api";
import { ACTIONS, RISK_TONE, api, fmtCountdown, fmtDate, fmtMoney, hoursUntil } from "./api";
import { Chip } from "./components";

const RISK_ORDER: Record<string, number> = { low: 0, medium: 1, high: 2, critical: 3 };

function topDecision(s: Shipment): Decision | undefined {
  return [...s.decisions].sort((a, b) => RISK_ORDER[b.risk_level] - RISK_ORDER[a.risk_level])[0];
}

function FleetRow({ s, selected, onSelect, rowRef }:
  { s: Shipment; selected: boolean; onSelect: () => void; rowRef?: React.Ref<HTMLTableRowElement> }) {
  const top = topDecision(s);
  const contractEta = new Date(s.eta);
  let pgEta = contractEta;
  let delayDays = 0;
  if (top) {
    const opt = top.exposure?.options.find((o) => o.action === top.recommended_action);
    delayDays = opt ? Math.round(opt.expected_delay_days) : 0;
    pgEta = new Date(contractEta.getTime() + delayDays * 86_400_000);
  }
  const dueHours = top?.exposure?.force_majeure_notice_due ? hoursUntil(top.exposure.force_majeure_notice_due) : null;
  return (
    <tr ref={rowRef} className={selected ? "selected" : ""} onClick={onSelect}
        aria-expanded={selected}>
      <td>
        <div style={{ fontWeight: 600 }}>{s.vessel_name ?? s.shipment_id}</div>
        <div className="pg-mono">{s.bl_number}</div>
        {/* IMO is the vessel's permanent identifier: showing it is what makes the claim "this is a
            real ship" checkable by anyone, in Equasis or GISIS, in about ten seconds. */}
        {s.imo && s.vessel_source === "public_registry" ? (
          <div style={{ fontSize: 11, color: "var(--pg-faint)", marginTop: 2 }}
               title={`${s.carrier ?? "Carrier"} — real vessel, IMO ${s.imo}. Carrier attributed from the vessel name.`}>
            IMO {s.imo} · {s.carrier}
          </div>
        ) : null}
      </td>
      <td>
        <div>{s.pol_locode} → {s.pod_locode}</div>
        <div style={{ fontSize: 11.5, color: "var(--pg-faint)" }}>
          {s.transshipment.length ? `${s.transshipment.length} TSP · ${s.transshipment.join(", ")}` : "Direct"}
        </div>
      </td>
      <td className="pg-num">{fmtDate(contractEta.toISOString())}</td>
      <td className="pg-num">
        {fmtDate(pgEta.toISOString(), false)}{" "}
        {delayDays > 0 ? <Chip text={`+${delayDays}d`} tone="warn" /> : <Chip text="On time" tone="ok" />}
      </td>
      <td>
        {top ? (
          <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Chip text={top.risk_level.toUpperCase()} tone={RISK_TONE[top.risk_level]} />
            <span>{top.recommended_action_label} — ${fmtMoney(top.estimated_cost_usd ?? 0)}</span>
            {/* Review state belongs on the row, not only inside the panel. Without it the table
                looks identical before and after a decision is actioned, so an operator scanning
                thirty-three shipments cannot tell which ones still need them. A reviewed row also
                stops showing a notice countdown: the deadline is no longer the thing to act on. */}
            {top.human_decision === "approved" ? (
              <Chip text="APPROVED" tone="ok" />
            ) : top.human_decision === "overridden" ? (
              <Chip text="OVERRIDDEN" tone="mute" />
            ) : top.recommended_action === "invoke_force_majeure" && dueHours !== null ? (
              <Chip text={`Notice ${fmtCountdown(dueHours)}`} tone={dueHours > 0 ? "warn" : "risk"} />
            ) : top.estimated_cost_usd ? (
              <Chip text={`$${fmtMoney(top.estimated_cost_usd)}`} tone={top.risk_level === "critical" ? "risk" : "warn"} />
            ) : null}
          </span>
        ) : (
          <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Chip text="CLEAR" tone="ok" />
            <span style={{ color: "var(--pg-muted)" }}>No event touches this route</span>
          </span>
        )}
      </td>
    </tr>
  );
}

function DecisionPanel({ shipment, decision, onReviewed }: { shipment: Shipment; decision: Decision; onReviewed: () => void }) {
  const [note, setNote] = useState(decision.human_notes ?? "");
  const [busy, setBusy] = useState(false);
  // The drafted notice is the most concrete thing this product makes, and it was invisible: the
  // endpoint existed and nothing ever called it, so "Approve & send notice" referred to a document
  // the operator could not read.
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [openDraft, setOpenDraft] = useState<number | null>(null);
  const [copied, setCopied] = useState<number | null>(null);
  const [to, setTo] = useState("");
  const [sending, setSending] = useState<number | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [mail, setMail] = useState<{ configured: boolean; default_recipient: string | null } | null>(null);

  // Ask up front whether this deployment can send at all. Offering a Send button that cannot work
  // is worse than offering none: the operator finds out only after believing a notice went out.
  useEffect(() => {
    api.mailStatus()
      .then((m) => { setMail(m); if (m.default_recipient) setTo(m.default_recipient); })
      .catch(() => setMail({ configured: false, default_recipient: null }));
  }, []);

  useEffect(() => {
    let live = true;
    api.drafts(decision.decision_id)
      .then((d) => { if (live) setDrafts(d); })
      .catch(() => { if (live) setDrafts([]); });
    return () => { live = false; };
  }, [decision.decision_id, decision.human_decision]);
  const dueHours = decision.exposure?.force_majeure_notice_due ? hoursUntil(decision.exposure.force_majeure_notice_due) : null;
  const options = (decision.exposure?.options ?? []).filter((o) => o.available);
  const maxCost = Math.max(0, ...options.map((o) => o.cost_usd));

  async function review(human_decision: "approved" | "overridden") {
    setBusy(true);
    try {
      await api.reviewDecision(decision.decision_id, human_decision, note);
      const fresh = await api.drafts(decision.decision_id).catch(() => []);
      setDrafts(fresh);
      onReviewed();
    } finally {
      setBusy(false);
    }
  }

  async function sendDraft(d: Draft) {
    setSending(d.draft_id);
    setSendError(null);
    try {
      await api.sendDraft(d.draft_id, to.trim());
      setDrafts(await api.drafts(decision.decision_id).catch(() => drafts ?? []));
    } catch (e) {
      setSendError(e instanceof Error ? e.message : "Could not send.");
    } finally {
      setSending(null);
    }
  }

  async function copyDraft(d: Draft) {
    try {
      await navigator.clipboard.writeText(`${d.subject}\n\n${d.body}`);
      setCopied(d.draft_id);
      setTimeout(() => setCopied(null), 1800);
    } catch {
      setCopied(null);  // clipboard blocked (insecure context); the text is on screen regardless
    }
  }

  return (
    <div className="pg-card" style={{ marginTop: 14 }}>
      <div className="pg-eyebrow">{shipment.pol} → {shipment.pod} · {shipment.bl_number}</div>
      <div className="pg-h">{decision.recommended_action_label}</div>
      {/* The costs below are priced from contract terms that are generated, on a vessel that is
          real. Saying which is which here — next to the money, not buried in a footer — is the
          difference between a demo someone can trust and one they have to take on faith. */}
      {shipment.vessel_source === "public_registry" && shipment.terms_source === "synthetic" ? (
        <div style={{ fontSize: 11.5, color: "var(--pg-faint)", marginTop: 6, lineHeight: 1.45 }}>
          {shipment.vessel_name} (IMO {shipment.imo}) and {shipment.carrier} are real and verifiable;
          the carrier is attributed from the vessel name. Contract terms and cargo values below are
          synthetic — real Bills of Lading are commercially confidential.
        </div>
      ) : null}

      {decision.exposure?.force_majeure_covered === false ? (
        <div className="pg-card" style={{ marginTop: 10, color: "var(--pg-muted)", fontSize: "0.87rem" }}>
          Force majeure clause does not cover this event.
        </div>
      ) : dueHours !== null ? (
        <div className={`pg-banner${dueHours <= 0 ? " late" : ""}`} style={{ marginTop: 10 }}>
          {"⏱"} Written force majeure notice {dueHours > 0 ? "due in" : "was due"}{" "}
          <strong>{fmtCountdown(dueHours)}</strong>{dueHours <= 0 ? " ago" : ""} — by{" "}
          {decision.exposure!.force_majeure_notice_due!.slice(0, 16).replace("T", " ")} UTC
        </div>
      ) : null}

      <div className="pg-panel-grid" style={{ marginTop: 14 }}>
        <div className="pg-card">
          <div className="pg-eyebrow">What each option costs</div>
          {options.map((o) => {
            const isBest = o.action === decision.recommended_action;
            const isWorst = !isBest && maxCost > 0 && o.cost_usd === maxCost && o.cost_usd > 0;
            const width = maxCost ? Math.max(3, Math.round((100 * o.cost_usd) / maxCost)) : 3;
            const wrapClass = isBest ? "pg-best" : "pg-option";
            return (
              <div key={o.action} className={wrapClass}>
                <div className="pg-option-head">
                  <span style={{ fontWeight: isBest ? 600 : 400, color: isBest ? "var(--pg-blue)" : "var(--pg-text)" }}>
                    {ACTIONS[o.action] ?? o.action}{isBest ? " · recommended" : ""}
                  </span>
                  <span className="pg-num" style={{ fontWeight: 600, color: isBest ? "var(--pg-blue)" : isWorst ? "var(--pg-red)" : "var(--pg-text)" }}>
                    ${fmtMoney(o.cost_usd)}
                  </span>
                </div>
                <div className={`pg-bar${isBest ? " is-best" : isWorst ? " is-risk" : ""}`}>
                  <span style={{ width: `${width}%` }} />
                </div>
                <div className="pg-option-note">{o.note}</div>
              </div>
            );
          })}
          {decision.exposure?.assumptions?.length ? (
            <div style={{ fontSize: 11.5, color: "var(--pg-faint)", marginTop: 12 }}>
              {decision.exposure.assumptions[decision.exposure.assumptions.length - 1]}
            </div>
          ) : null}
        </div>

        <div>
          <div className="pg-card">
            <div className="pg-eyebrow">Why this fired</div>
            <div style={{ fontSize: 13.5, lineHeight: 1.55, marginTop: 6 }}>{decision.rationale}</div>
            <div className="pg-facts">
              <div>
                <div className="pg-fact-label">Matched on</div>
                <div className="pg-fact-value">{decision.match_context?.match_reason?.replace(/_/g, " ") ?? "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>{decision.match_context?.matched_port_id}</div>
              </div>
              <div>
                <div className="pg-fact-label">Corroboration</div>
                <div className="pg-fact-value">{decision.corroboration ? `${decision.corroboration.n_reports} reports` : "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>
                  {decision.corroboration ? `${decision.corroboration.n_publishers} publishers · ${Math.round(decision.corroboration.confidence * 100)}%` : ""}
                </div>
              </div>
              <div>
                <div className="pg-fact-label">Days to ETA</div>
                <div className="pg-fact-value">{decision.match_context?.days_until_eta ?? "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>ETA {shipment.eta.slice(0, 10)}</div>
              </div>
            </div>
          </div>

          {decision.traffic?.pct_change != null && (
            <div className="pg-card" style={{ marginTop: 14 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div className="pg-eyebrow">Measured traffic</div>
                  <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 2 }}>{decision.traffic.name} · IMF PortWatch</div>
                </div>
                <Chip text={`${decision.traffic.pct_change < 0 ? "↓" : "↑"} ${Math.abs(decision.traffic.pct_change).toFixed(1)}%`} tone="warn" />
              </div>
            </div>
          )}

          <div className="pg-card" style={{ marginTop: 14 }}>
            <div className="pg-eyebrow">Evidence</div>
            <div style={{ marginTop: 8, fontSize: 13.5, lineHeight: 1.8 }}>
              {(decision.corroboration?.headlines ?? []).slice(0, 4).map((h, i) => (
                <div key={i}><a href={h.url} target="_blank" rel="noreferrer">{h.source} — {h.title.slice(0, 70)}</a></div>
              ))}
              {!decision.corroboration?.headlines?.length &&
                decision.citations.slice(0, 4).map((u, i) => (
                  <div key={i}><a href={u} target="_blank" rel="noreferrer">[{i + 1}] {u.slice(0, 70)}</a></div>
                ))}
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 14 }}>
        <label htmlFor="note" style={{ fontSize: 13, color: "var(--pg-muted)" }}>Reviewer note</label>
        <input id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional — visible in the audit log"
          style={{ flexGrow: 1, border: "1px solid var(--pg-line)", borderRadius: 6, padding: "10px 12px", fontSize: 13.5 }} />
        <button className="pg-btn" disabled={busy} onClick={() => review("overridden")}>Override</button>
        {/* The old label was "Approve & send notice". Nothing is transmitted anywhere — there is no
            mail or EDI integration — so the button now names what it actually does.
            Once approved this becomes a static confirmation rather than a disabled button: a
            greyed-out control that still looks pressable is indistinguishable from a broken one,
            and that is exactly how it was read. Override remains live, and reverts it. */}
        {decision.human_decision === "approved" ? (
          <span className="pg-approved-mark">
            Approved
          </span>
        ) : (
          <button className="pg-btn primary" disabled={busy} onClick={() => review("approved")}>
            {busy ? "Saving…" : "Approve"}
          </button>
        )}
      </div>

      {/* Approval is otherwise invisible: one word in grey at the bottom of a long panel. */}
      <div style={{ marginTop: 10, fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
        {decision.human_decision === "approved" ? (
          <Chip text="APPROVED" tone="ok" />
        ) : decision.human_decision === "overridden" ? (
          <Chip text="OVERRIDDEN" tone="mute" />
        ) : (
          <Chip text="AWAITING REVIEW" tone="warn" />
        )}
        <span style={{ color: "var(--pg-muted)" }}>
          {decision.human_decision === "approved"
            ? "Notice signed off and ready to send. PortGuard does not transmit it."
            : decision.human_decision === "overridden"
            ? "Recommendation rejected; the drafted notice was discarded."
            : "No document is sent until a human approves it."}
        </span>
      </div>

      {drafts && drafts.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="pg-eyebrow">Drafted documents</div>
          {drafts.map((d) => (
            <div key={d.draft_id} className="pg-draft">
              <button className="pg-draft-head"
                      onClick={() => setOpenDraft(openDraft === d.draft_id ? null : d.draft_id)}>
                <span style={{ flexGrow: 1, textAlign: "left" }}>
                  <strong>{d.kind.replace(/_/g, " ")}</strong>
                  <span style={{ color: "var(--pg-muted)" }}> — {d.subject}</span>
                </span>
                <Chip text={d.status.toUpperCase()}
                      tone={d.status === "approved" ? "ok" : d.status === "discarded" ? "mute" : "warn"} />
                <span style={{ color: "var(--pg-faint)", marginLeft: 8 }}>
                  {openDraft === d.draft_id ? "Hide" : "Read"}
                </span>
              </button>
              {openDraft === d.draft_id && (
                <div>
                  <pre className="pg-draft-body">{d.body}</pre>
                  <div className="pg-draft-actions">
                    <button className="pg-btn" style={{ minHeight: 34, fontSize: 12.5 }}
                            onClick={() => copyDraft(d)}>
                      {copied === d.draft_id ? "Copied" : "Copy text"}
                    </button>
                    {d.status === "sent" ? (
                      <span style={{ fontSize: 12.5, color: "var(--pg-green)" }}>
                        Sent to <strong>{d.sent_to}</strong>
                        {d.sent_at ? ` on ${d.sent_at.slice(0, 16).replace("T", " ")} UTC` : ""}
                      </span>
                    ) : d.status === "approved" ? (
                      <>
                        <input value={to} onChange={(e) => setTo(e.target.value)}
                               placeholder="recipient@company.com" aria-label="Send to"
                               style={{ flexGrow: 1, minWidth: 180, border: "1px solid var(--pg-line)",
                                        borderRadius: 6, padding: "7px 10px", fontSize: 12.5 }} />
                        <button className="pg-btn primary" style={{ minHeight: 34, fontSize: 12.5 }}
                                disabled={sending === d.draft_id || !to.trim() || !mail?.configured}
                                title={mail?.configured ? "" : "Email is not configured on this deployment"}
                                onClick={() => sendDraft(d)}>
                          {sending === d.draft_id ? "Sending…" : "Send with signed copy"}
                        </button>
                      </>
                    ) : (
                      <span style={{ fontSize: 12.5, color: "var(--pg-muted)" }}>
                        Approve this decision to enable sending.
                      </span>
                    )}
                  </div>
                  {!mail?.configured && d.status === "approved" && (
                    <div className="pg-draft-hint">
                      Email is not configured here, so nothing can be transmitted. Copy the text, or
                      set RESEND_API_KEY to enable sending.
                    </div>
                  )}
                  {sendError && <div className="pg-draft-hint" style={{ color: "var(--pg-red)" }}>{sendError}</div>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Shipments() {
  const [ships, setShips] = useState<Shipment[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const selectedRow = useRef<HTMLTableRowElement>(null);
  // Only the FIRST load picks a row. Without this, collapsing the open row and letting the 60s
  // refresh land would silently re-open the top shipment.
  const autoPicked = useRef(false);

  function reload() {
    api.shipments().then((data) => {
      setShips(data);
      if (!autoPicked.current && data.length) {
        autoPicked.current = true;
        setSelected(data[0].shipment_id);
      }
    });
  }
  useEffect(reload, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Bring the opened row and its details into view together. Runs in a layout effect after the
  // expanded row exists, so the measurement is of the final height rather than the collapsed one.
  useLayoutEffect(() => {
    const row = selectedRow.current;
    if (!row || !selected) return;
    const panel = row.closest(".pg-tabpanel") as HTMLElement | null;
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!panel) {
      row.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "center" });
      return;
    }
    // rAF so the scroll is queued after this frame's layout, otherwise Safari animates towards a
    // position measured before the expanded row was painted and lands short.
    const id = requestAnimationFrame(() => {
      const rowTop = row.getBoundingClientRect().top;
      const panelTop = panel.getBoundingClientRect().top;
      // Sit the row a fifth of the way down rather than flush at the top: that clears the sticky
      // table header and leaves the detail block filling the space below it, which is what
      // "centred" means once the row itself is the thing you are reading from.
      const offset = panel.clientHeight * 0.2;
      panel.scrollTo({ top: panel.scrollTop + (rowTop - panelTop) - offset,
                       behavior: smooth ? "smooth" : "auto" });
    });
    return () => cancelAnimationFrame(id);
  }, [selected]);

  if (!ships) return <div className="pg-card pg-empty">Loading shipments…</div>;
  if (!ships.length) return <div className="pg-card pg-empty">No shipments yet — run <code>python src/load_shipments.py</code>.</div>;

  const sorted = [...ships].sort((a, b) => RISK_ORDER[topDecision(b)?.risk_level ?? "low"] - RISK_ORDER[topDecision(a)?.risk_level ?? "low"]);
  const activeShip = ships.find((s) => s.shipment_id === selected);
  const activeDecisions = [...(activeShip?.decisions ?? [])].sort((a, b) => RISK_ORDER[b.risk_level] - RISK_ORDER[a.risk_level]);

  return (
    <div>
      <table className="pg-table">
        <thead>
          <tr>
            <th>Shipment · B/L</th><th>Path</th><th>Contract ETA</th><th>PortGuard ETA</th><th>Status &amp; recommendation</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => {
            const isOpen = s.shipment_id === selected;
            return (
              <Fragment key={s.shipment_id}>
                <FleetRow s={s} selected={isOpen} rowRef={isOpen ? selectedRow : undefined}
                  onSelect={() => setSelected(isOpen ? null : s.shipment_id)} />
                {/* Details belong under the row they describe. Rendered after the whole table they
                    sat below thirty-two other shipments, so on a long fleet you clicked a row and
                    the thing you asked for was off-screen. */}
                {isOpen && (
                  <tr className="pg-expand">
                    <td colSpan={5}>
                      {activeDecisions.length === 0 ? (
                        <div className="pg-card pg-empty">
                          No matching events for {s.shipment_id} right now.
                        </div>
                      ) : activeDecisions.map((d) => (
                        <DecisionPanel key={d.decision_id} shipment={s} decision={d} onReviewed={reload} />
                      ))}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

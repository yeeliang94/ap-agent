import { Fragment, useEffect, useMemo, useState, type FocusEvent } from "react";
import {
  ArtifactRole,
  ClaimArtifact,
  ClaimCase,
  ClaimsRunDetail,
  Disposition,
  claimsFileUrl,
  confirmClaimant,
  confirmGrouping,
  createCase,
  mergeCase,
  moveArtifact,
  setArtifactDisposition,
  setArtifactRole,
  setClaimant,
  splitCase,
  updateCase,
} from "../../api";
import { InlineConfirm, InlineReason } from "../../components/Inline";
import { Reload, useAction } from "../../hooks/useAction";

// Map & Group (hardening H6): the ONE map screen for every input shape.
// The investigation proposed Claim Cases (a structured folder arrives with
// its grouping pre-proposed on a folder basis); the reviewer confirms,
// merges, splits, moves files, sets or confirms claimants and settles the
// files nobody placed. Every action is applied on the server at once
// (audited, revision-checked); the screen reloads after each one. The
// view is keyed by the run's STATUS only (not its revision): a reload
// updates it in place, so what the reviewer is typing, which rows are
// open and a stale-run message all survive the reload.
// The delivered MapView is the fallback while CLAIMS_CASE_MODEL is off.

const ROLE_LABEL: Record<ArtifactRole, string> = {
  report: "Claim summary (report)",
  receipts: "Receipts / evidence",
  approval: "Approval e-mail",
  report_copy: "Print of the report",
  listing: "Listing",
  roster: "Roster",
  policy: "Policy",
  other: "Other (not evidence)",
  unknown: "Unknown — please decide",
};

const DISPOSITION_LABEL: Record<Disposition, string> = {
  used: "used",
  duplicate: "duplicate",
  irrelevant: "irrelevant",
  unreadable: "unreadable",
  unresolved: "not placed yet",
};

function fileName(path: string): string {
  return path.split("/").pop() ?? path;
}

/** The reviewer's unsaved text for a case, with the server values it was
 *  typed over: when the server's values move on (our save landed, or
 *  another screen changed them) the draft is stale and the server wins. */
interface Draft {
  name: string;
  identifier: string;
  label: string;
  base: { name: string; identifier: string; label: string };
}

function serverValues(c: ClaimCase): Draft["base"] {
  return { name: c.claimant.name, identifier: c.claimant.identifier, label: c.label };
}

function sameValues(a: Draft["base"], b: Draft["base"]): boolean {
  return a.name === b.name && a.identifier === b.identifier && a.label === b.label;
}

/** The one thing the screen is asking the reviewer right now: a reason
 *  for settling a file, a name for a new / split case, or a yes to a
 *  merge — asked inline, never through window.prompt/confirm. */
type Pending =
  | { kind: "settle"; artifactId: string; disposition: Disposition }
  | { kind: "split"; caseId: string }
  | { kind: "create" }
  | { kind: "merge"; caseId: string; into: string };

export default function GroupView({
  run,
  onChanged,
  onConfirmed,
}: {
  run: ClaimsRunDetail;
  onChanged: Reload;
  onConfirmed: () => void;
}) {
  const cases = run.cases ?? [];
  const artifacts = run.artifacts ?? [];
  const grouping = run.grouping;
  const action = useAction(onChanged);
  const busy = !!action.busy;
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [pending, setPending] = useState<Pending | null>(null);
  // Drafts are reconciled against the server on every reload: one typed
  // over values the server has since changed is dropped (the server wins),
  // as is one for a case that no longer exists.
  useEffect(() => {
    setDrafts((ds) => {
      const byId = new Map(cases.map((c) => [c.id, c]));
      const kept = Object.entries(ds).filter(([id, d]) => {
        const c = byId.get(id);
        return c && sameValues(d.base, serverValues(c));
      });
      return kept.length === Object.keys(ds).length ? ds : Object.fromEntries(kept);
    });
  }, [cases]);
  const survey = "files" in run.survey ? run.survey : null;
  const tabsOf = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const f of survey?.files ?? []) m.set(f.path, Object.keys(f.peek?.tabs ?? {}));
    for (const a of artifacts) if (!m.has(a.path) && a.sheets?.length) m.set(a.path, a.sheets);
    return m;
  }, [survey, artifacts]);
  const readOnly = run.status !== "map_ready";
  // Regrouping (create/merge/split/move/role) is a server switch; the gate,
  // claimant, report sheet and dispositions are always available.
  const canRegroup = !readOnly && grouping?.actions_enabled !== false;
  const byCase = useMemo(() => {
    const m = new Map<string, ClaimArtifact[]>();
    for (const a of artifacts) m.set(a.case_id, [...(m.get(a.case_id) ?? []), a]);
    return m;
  }, [artifacts]);
  const pool = byCase.get("") ?? [];
  const openConflicts = run.flags.filter((f) => f.code === "OWNERSHIP_CONFLICT" && f.status === "open");
  const inv = run.investigation;

  /** Run one server action: awaited, then the run is reloaded (awaited too)
   *  before the buttons are released; a stale run reloads, then says so. */
  const act = (fn: () => Promise<unknown>) => action.run(fn);

  function draftOf(c: ClaimCase): Draft["base"] {
    const d = drafts[c.id];
    const server = serverValues(c);
    return d && sameValues(d.base, server) ? d : server;
  }

  function setDraft(c: ClaimCase, patch: Partial<Draft["base"]>) {
    setDrafts({ ...drafts, [c.id]: { ...draftOf(c), ...patch, base: serverValues(c) } });
  }

  function claimantDirty(c: ClaimCase) {
    const d = draftOf(c);
    return d.name !== c.claimant.name || d.identifier !== c.claimant.identifier;
  }

  /** Name and identifier are saved TOGETHER, once, when focus leaves the
   *  claimant cell — not on each input's blur: one audited change per
   *  claimant, and the inputs are disabled while it is in flight, so
   *  saving the first field never interrupts typing the second. */
  function leaveClaimant(c: ClaimCase, ev: FocusEvent<HTMLElement>) {
    const next = ev.relatedTarget as HTMLElement | null;
    if (next?.dataset?.claimantOf === c.id) return; // moving between the two inputs
    if (!claimantDirty(c)) return;
    const d = draftOf(c);
    act(() => setClaimant(run.id, run.revision, c.id, d.name, d.identifier));
  }

  function saveLabel(c: ClaimCase) {
    const d = draftOf(c);
    if (d.label.trim() && d.label !== c.label) act(() => updateCase(run.id, run.revision, c.id, { label: d.label }));
  }

  /** Settle a file: asks for the reason inline (in the file's row); the
   *  round-trip is made only once a non-empty reason is typed. */
  function settle(a: ClaimArtifact, disposition: Disposition) {
    setPending({ kind: "settle", artifactId: a.id, disposition });
  }

  async function settleWith(a: ClaimArtifact, disposition: Disposition, reason: string) {
    if (await act(() => setArtifactDisposition(run.id, run.revision, a.id, disposition, reason))) setPending(null);
  }

  async function splitSelected(c: ClaimCase, label: string) {
    const ids = (byCase.get(c.id) ?? []).filter((a) => selected[a.id]).map((a) => a.id);
    if (!ids.length) return;
    if (await act(() => splitCase(run.id, run.revision, c.id, ids, label))) {
      setSelected({});
      setPending(null);
    }
  }

  async function createFromSelected(label: string) {
    const ids = pool.filter((a) => selected[a.id]).map((a) => a.id);
    if (!ids.length) return;
    if (await act(() => createCase(run.id, run.revision, label, ids))) {
      setSelected({});
      setPending(null);
    }
  }

  async function mergeInto(c: ClaimCase, into: string) {
    if (await act(() => mergeCase(run.id, run.revision, c.id, into))) setPending(null);
  }

  return (
    <div>
      {inv?.plan && (
        <p className="sub">
          Investigation: {inv.adapter === "investigator" ? "tool-using agent" : "structured-folder mapper"}
          {inv.plan.strategy ? ` · ${inv.plan.strategy.replace("_", " ")}` : ""}
          {inv.plan.rounds ? ` · settled on round ${inv.plan.rounds}` : ""}
          {inv.plan.steps?.length ? ` · steps: ${inv.plan.steps.slice(0, 6).join(" → ")}` : ""}
          {inv.plan.questions?.length ? ` · open questions: ${inv.plan.questions.join("; ")}` : ""}
          {run.tool_summary && Object.keys(run.tool_summary).length > 0
            ? ` · tools: ${Object.values(run.tool_summary).reduce((n, t) => n + t.calls, 0)} call(s)` +
              (Object.values(run.tool_summary).some((t) => t.failed)
                ? `, ${Object.values(run.tool_summary).reduce((n, t) => n + t.failed, 0)} failed — see the TOOL_FAILED flags`
                : "")
            : ""}
          {!canRegroup && !readOnly ? " · regrouping actions are switched off on this server (files can still be settled, claimants set, the sheet chosen)" : ""}
        </p>
      )}
      {run.map_warnings.length > 0 && (
        <div className="card banner bad">
          <b>The audit could not settle {run.map_warnings.length} thing(s)</b>
          <ul className="muted">
            {run.map_warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <span className="sub">Fix them below (move, split, set the claimant, settle the file), then confirm.</span>
        </div>
      )}
      {grouping && (
        <p className="summary-line">
          <b>
            {grouping.counts.dispositioned}/{grouping.counts.artifacts} files placed
            {grouping.counts.unresolved ? ` · ${grouping.counts.unresolved} need review` : ""}
            {" · "}
            {grouping.counts.to_verify} case{grouping.counts.to_verify === 1 ? "" : "s"} to verify
            {openConflicts.length ? ` · ${openConflicts.length} ownership conflict${openConflicts.length === 1 ? "" : "s"}` : ""}
          </b>{" "}
          <span className="sub">
            Cases are proposals; a claimant is confirmed by you, never by the AI. Confirming the grouping
            confirms the names shown as <i>proposed</i>; a case with no name stays unknown and cannot be paid.
          </span>
        </p>
      )}
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table className="table maptable">
          <thead>
            <tr>
              <th></th>
              <th>Case</th>
              <th>Claimant</th>
              <th>Claim summary + sheet</th>
              <th>Mileage sheet</th>
              <th>Receipts</th>
              <th>Other files</th>
              <th>Not placed</th>
              <th>Exclude</th>
              <th>Merge into</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => {
              const files = byCase.get(c.id) ?? [];
              const workbooks = files.filter((a) => a.media_type === "workbook");
              const tabs = c.roles.report_file ? tabsOf.get(c.roles.report_file) ?? [] : [];
              const receipts = files.filter((a) => c.roles.receipt_files.includes(a.path));
              const others = files.filter((a) => !c.roles.receipt_files.includes(a.path) && a.path !== c.roles.report_file && a.disposition !== "unresolved");
              const unresolved = files.filter((a) => a.disposition === "unresolved");
              const conflict = openConflicts.find((f) => f.case_id === c.id);
              const d = draftOf(c);
              const open = !!expanded[c.id];
              const problems = grouping?.by_case?.[c.id] ?? [];
              return (
                <Fragment key={c.id}>
                  <tr className={conflict || unresolved.length || problems.length ? "attention" : c.state === "excluded" ? "detail" : ""}>
                    <td>
                      <button className="btn" aria-label={open ? "Hide files" : "Show files"} title={c.reason}
                        onClick={() => setExpanded({ ...expanded, [c.id]: !open })}>
                        {open ? "▾" : "▸"}
                      </button>
                    </td>
                    <td>
                      <input aria-label={`case ${c.label} name`} value={d.label} disabled={readOnly} style={{ width: 130 }}
                        onChange={(ev) => setDraft(c, { label: ev.target.value })} onBlur={() => saveLabel(c)} />
                      <span className="sub" title={c.grouping_basis}>{c.grouping_basis.split(":")[0].replace("_", " ")}
                        {c.confidence ? ` · ${Math.round(c.confidence * 100)}%` : ""}</span>
                      {conflict && <span className="pill warn" title={conflict.reason}>ownership conflict</span>}
                    </td>
                    <td>
                      <span onBlur={(ev) => leaveClaimant(c, ev)}>
                        <input aria-label={`${c.label} claimant name`} value={d.name} disabled={readOnly || busy} style={{ width: 130 }}
                          data-claimant-of={c.id} placeholder="name" onChange={(ev) => setDraft(c, { name: ev.target.value })} />
                        <input aria-label={`${c.label} identifier`} value={d.identifier} disabled={readOnly || busy} style={{ width: 140 }}
                          data-claimant-of={c.id} placeholder="ER code / id" onChange={(ev) => setDraft(c, { identifier: ev.target.value })} />
                      </span>
                      <span className={`chip ${c.claimant.state === "confirmed" ? "ok" : c.claimant.state === "proposed" ? "review" : "flag"}`}
                        title={c.claimant.basis}>
                        {c.claimant.state}
                      </span>
                      {c.claimant.state === "proposed" && !readOnly && (
                        <button className="btn" disabled={busy || claimantDirty(c)}
                          title={claimantDirty(c) ? "The edited name is saved when you leave the field; confirm after that." : c.claimant.basis}
                          onClick={() => act(() => confirmClaimant(run.id, run.revision, c.id))}>
                          Confirm name
                        </button>
                      )}
                    </td>
                    <td>
                      <label className="sub">
                        <input type="checkbox" aria-label={`${c.label} has no summary`} checked={!!c.roles.no_report} disabled={readOnly}
                          onChange={(ev) => act(() => updateCase(run.id, run.revision, c.id, {
                            roles: ev.target.checked ? { no_report: true } : { no_report: false, report_file: workbooks[0]?.path ?? null },
                          }))} />{" "}
                        no summary — lines from evidence
                      </label>
                      {!c.roles.no_report && (
                        <>
                          <select aria-label={`${c.label} report file`} value={c.roles.report_file ?? ""} disabled={readOnly}
                            onChange={(ev) => ev.target.value && act(() => updateCase(run.id, run.revision, c.id, { roles: { report_file: ev.target.value, report_tab: null } }))}>
                            <option value="">— choose —</option>
                            {workbooks.map((a) => (
                              <option key={a.id} value={a.path}>{fileName(a.path)}</option>
                            ))}
                          </select>
                          <select aria-label={`${c.label} report tab`} value={c.roles.report_tab ?? ""} disabled={readOnly || !c.roles.report_file}
                            onChange={(ev) => act(() => updateCase(run.id, run.revision, c.id, { roles: { report_tab: ev.target.value || null } }))}>
                            <option value="">— sheet —</option>
                            {tabs.map((t) => (
                              <option key={t} value={t}>{t}</option>
                            ))}
                          </select>
                        </>
                      )}
                    </td>
                    <td>
                      {!c.roles.no_report && (
                        <select aria-label={`${c.label} mileage tab`} value={c.roles.mileage_tab ?? ""} disabled={readOnly || !c.roles.report_file}
                          onChange={(ev) => act(() => updateCase(run.id, run.revision, c.id, { roles: { mileage_tab: ev.target.value || null } }))}>
                          <option value="">— none —</option>
                          {tabs.map((t) => (
                            <option key={t} value={t}>{t}</option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td>{receipts.length ? receipts.map((a) => <span key={a.id} className="pill" title={a.role_reason}>{fileName(a.path)}</span>) : <span className="sub">none</span>}</td>
                    <td>{others.length ? others.map((a) => <span key={a.id} className="pill muted" title={`${ROLE_LABEL[a.proposed_role]} — ${a.role_reason}`}>{fileName(a.path)}</span>) : <span className="sub">—</span>}</td>
                    <td>{unresolved.length ? unresolved.map((a) => <span key={a.id} className="pill warn" title={a.role_reason}>{fileName(a.path)}</span>) : <span className="sub">—</span>}</td>
                    <td>
                      <input type="checkbox" aria-label={`exclude ${c.label}`} checked={c.state === "excluded"} disabled={readOnly}
                        onChange={(ev) => act(() => updateCase(run.id, run.revision, c.id, { state: ev.target.checked ? "excluded" : "proposed" }))} />
                    </td>
                    <td>
                      {canRegroup && cases.length > 1 && (pending?.kind === "merge" && pending.caseId === c.id ? (
                        <InlineConfirm
                          question={`Merge ${c.label} into ${cases.find((x) => x.id === pending.into)?.label ?? "that case"}? Its files move over; this case is removed.`}
                          confirmLabel="Merge" busy={busy}
                          onConfirm={() => mergeInto(c, pending.into)} onCancel={() => setPending(null)} />
                      ) : (
                        <select aria-label={`merge ${c.label} into`} value="" disabled={busy}
                          onChange={(ev) => ev.target.value && setPending({ kind: "merge", caseId: c.id, into: ev.target.value })}>
                          <option value="">— merge —</option>
                          {cases.filter((x) => x.id !== c.id).map((x) => (
                            <option key={x.id} value={x.id}>{x.label}</option>
                          ))}
                        </select>
                      ))}
                    </td>
                  </tr>
                  {open && (
                    <tr className="detail">
                      <td></td>
                      <td colSpan={9}>
                        <p className="basis">Why this case: {c.reason || c.grouping_basis}
                          {c.claimant.basis ? ` · claimant: ${c.claimant.basis}` : ""}</p>
                        <FileTable files={files} run={run} cases={cases} readOnly={readOnly} canRegroup={canRegroup} busy={busy}
                          selected={selected} setSelected={setSelected} act={act} settle={settle} tabsOf={tabsOf}
                          pending={pending} settleWith={settleWith} cancelPending={() => setPending(null)} />
                        {canRegroup && files.length > 1 && (pending?.kind === "split" && pending.caseId === c.id ? (
                          <InlineReason prompt="Name for the new case" placeholder="e.g. Alicia — second claim" initial={`${c.label} (split)`}
                            confirmLabel="Split into a new case" busy={busy}
                            onConfirm={(label) => splitSelected(c, label)} onCancel={() => setPending(null)} />
                        ) : (
                          <div className="actions">
                            <button className="btn" disabled={busy || !files.some((a) => selected[a.id])}
                              onClick={() => setPending({ kind: "split", caseId: c.id })}>
                              Split selected files into a new case
                            </button>
                          </div>
                        ))}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {cases.length === 0 && (
              <tr>
                <td colSpan={10} className="sub">No cases yet — create one from the files below.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h3 style={{ marginTop: 18 }}>
        Files in no case {pool.length ? `(${pool.length})` : ""}
        <span className="sub">
          Every file must end up used inside a case, or be settled as irrelevant, a duplicate or unreadable.
          A file left here that could carry a claim (a workbook, PDF or image) keeps the gate shut.
        </span>
      </h3>
      {pool.length === 0 ? (
        <p className="sub">Every file sits inside a case.</p>
      ) : (
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <FileTable files={pool} run={run} cases={cases} readOnly={readOnly} canRegroup={canRegroup} busy={busy}
            selected={selected} setSelected={setSelected} act={act} settle={settle} tabsOf={tabsOf} pool
            pending={pending} settleWith={settleWith} cancelPending={() => setPending(null)} />
          {canRegroup && (pending?.kind === "create" ? (
            <div style={{ padding: 10 }}>
              <InlineReason prompt="Name for the new case" placeholder="e.g. the claimant's name" initial=""
                confirmLabel="Create the case" busy={busy}
                onConfirm={createFromSelected} onCancel={() => setPending(null)} />
            </div>
          ) : (
            <div className="actions" style={{ padding: 10 }}>
              <button className="btn" disabled={busy || !pool.some((a) => selected[a.id])}
                onClick={() => setPending({ kind: "create" })}>
                New case from selected files
              </button>
            </div>
          ))}
        </div>
      )}

      {!readOnly && (
        <div className="actions">
          <button className="btn primary" disabled={busy || !grouping?.ok}
            title={grouping?.problems.length ? grouping.problems.join("\n") : "Confirm the grouping and start verifying every case"}
            onClick={() => act(async () => { await confirmGrouping(run.id, run.revision); onConfirmed(); })}>
            {busy ? "Working…" : "Confirm grouping & verify"}
          </button>
          {grouping && grouping.problems.length > 0 && (
            <span className="sub">
              Not ready: {grouping.problems[0]}
              {grouping.problems.length > 1 ? ` (+${grouping.problems.length - 1} more)` : ""}
            </span>
          )}
        </div>
      )}
      {readOnly && <p className="sub">This grouping was confirmed; verification has started.</p>}
      {action.error && <p className="error">{action.error}</p>}
    </div>
  );
}

function FileTable({
  files, run, cases, readOnly, canRegroup, busy, selected, setSelected, act, settle, tabsOf, pool,
  pending, settleWith, cancelPending,
}: {
  files: ClaimArtifact[];
  run: ClaimsRunDetail;
  cases: ClaimCase[];
  readOnly: boolean;
  canRegroup: boolean;
  busy: boolean;
  selected: Record<string, boolean>;
  setSelected: (s: Record<string, boolean>) => void;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  settle: (a: ClaimArtifact, d: Disposition) => void;
  tabsOf: Map<string, string[]>;
  pool?: boolean;
  pending: Pending | null;
  settleWith: (a: ClaimArtifact, d: Disposition, reason: string) => Promise<void>;
  cancelPending: () => void;
}) {
  return (
    <table className="table inner">
      <tbody>
        {files.map((a) => (
          <tr key={a.id} className={a.disposition === "unresolved" ? "attention" : ""}>
            <td>
              {canRegroup && (
                <input type="checkbox" aria-label={`select ${a.path}`} checked={!!selected[a.id]}
                  onChange={(ev) => setSelected({ ...selected, [a.id]: ev.target.checked })} />
              )}
            </td>
            <td className="mono" title={a.path}>{fileName(a.path)}</td>
            <td className="sub">
              {a.media_type}
              {a.pages ? `, ${a.pages} page(s)` : ""}
              {a.sheets?.length ? `, sheets: ${a.sheets.join(", ")}` : tabsOf.get(a.path)?.length ? `, sheets: ${tabsOf.get(a.path)!.join(", ")}` : ""}
              {a.signals && a.signals.filter((s) => s.strength === "strong").length > 0 && (
                <span className="sub" title="identity signals found in this file, each with where it was seen">
                  {a.signals.filter((s) => s.strength === "strong").map((s) => `${s.kind.replace("_", " ")}: ${s.value}`).join(" · ")}
                </span>
              )}
            </td>
            <td>
              <select aria-label={`role of ${a.path}`} value={a.proposed_role} disabled={readOnly || !canRegroup || busy}
                onChange={(ev) => act(() => setArtifactRole(run.id, run.revision, a.id, ev.target.value as ArtifactRole, false))}>
                {(Object.keys(ROLE_LABEL) as ArtifactRole[]).map((r) => (
                  <option key={r} value={r}>{ROLE_LABEL[r]}</option>
                ))}
              </select>
            </td>
            <td>
              <span className={`chip ${a.disposition === "unresolved" ? "flag" : a.disposition === "used" ? "ok" : "wait"}`}
                title={a.disposition_reason || a.role_reason}>
                {DISPOSITION_LABEL[a.disposition]}{a.disposition_by === "reviewer" ? " (you)" : ""}
              </span>
            </td>
            <td className="reason-cell">{a.role_reason || a.disposition_reason}</td>
            <td>
              {!readOnly && (
                <>
                  {canRegroup && (
                  <select aria-label={`move ${a.path}`} value="" disabled={busy}
                    onChange={(ev) => ev.target.value !== "" && act(() => moveArtifact(run.id, run.revision, a.id, ev.target.value === "__out" ? "" : ev.target.value))}>
                    <option value="">— move to —</option>
                    {cases.filter((c) => c.id !== a.case_id).map((c) => (
                      <option key={c.id} value={c.id}>{c.label}</option>
                    ))}
                    {!pool && <option value="__out">out of every case</option>}
                  </select>
                  )}
                  {a.disposition !== "used" && (pending?.kind === "settle" && pending.artifactId === a.id ? (
                    <InlineReason
                      prompt={`Why is ${fileName(a.path)} ${DISPOSITION_LABEL[pending.disposition]}? (goes in the audit trail)`}
                      placeholder="reason — required" initial="" confirmLabel={`Mark ${pending.disposition}`} busy={busy}
                      onConfirm={(reason) => settleWith(a, pending.disposition, reason)} onCancel={cancelPending} />
                  ) : (
                    <>
                      <button className="btn" disabled={busy} onClick={() => settle(a, "irrelevant")}>irrelevant</button>
                      <button className="btn" disabled={busy} onClick={() => settle(a, "duplicate")}>duplicate</button>
                      <button className="btn" disabled={busy} onClick={() => settle(a, "unreadable")}>unreadable</button>
                    </>
                  ))}
                </>
              )}
              {(a.media_type === "pdf" || a.media_type === "image") && (
                <a className="sub" href={claimsFileUrl(run.id, a.path, 1)} target="_blank" rel="noreferrer"> view page 1</a>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

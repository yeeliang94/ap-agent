import { useEffect, useState } from "react";
import {
  ArtifactRole, ClaimArtifact, ClaimCase, ClaimsRunDetail, confirmGrouping, createCase, mergeCase,
  moveArtifact, setArtifactDisposition, setArtifactRole, setClaimant, splitCase,
} from "../api";
import { Action, useAction } from "../hooks/useAction";
import { setQuery, useRouter } from "../router";

// The organization workbench of a claims run at map_ready: one selected
// Claim (identity, summary, assigned evidence), the submitted-files drawer,
// and — only when the server says regrouping is switched on — the merge /
// split / create / move / role actions.

export default function ClaimsOrganize({ run, reload }: { run: ClaimsRunDetail; reload: () => Promise<void> }) {
  const cases = run.cases || [];
  const artifacts = run.artifacts || [];
  const { location } = useRouter();
  const initial = new URLSearchParams(location.search).get("claim") || cases[0]?.id || "";
  const [selectedId, setSelectedId] = useState(initial);
  const selected = cases.find((c) => c.id === selectedId) || cases[0];
  const [drawer, setDrawer] = useState(false);
  const action = useAction(reload, "Could not update the organization");
  const problems = run.grouping?.by_case?.[selected?.id || ""] || [];
  const assigned = artifacts.filter((a) => a.case_id === selected?.id);
  const unassigned = artifacts.filter((a) => !a.case_id);
  const choose = (id: string) => { setSelectedId(id); window.history.replaceState({}, "", setQuery({ claim: id })); };
  const settle = (artifact: ClaimArtifact, disposition: "irrelevant" | "duplicate" | "unreadable") => action.run(
    () => setArtifactDisposition(run.id, run.revision, artifact.id, disposition, `${disposition} during organization`),
    { key: `settle:${artifact.id}`, fallback: "Could not update the file" },
  );
  const assign = (artifact: ClaimArtifact) => action.run(
    () => moveArtifact(run.id, run.revision, artifact.id, selected.id),
    { key: `assign:${artifact.id}`, fallback: "Could not assign the file" },
  );
  const setRole = (artifact: ClaimArtifact, role: ArtifactRole, remember: boolean) => action.run(
    () => setArtifactRole(run.id, run.revision, artifact.id, role, remember),
    { key: `role:${artifact.id}`, fallback: "Could not update the file role" },
  );
  const confirm = () => action.run(
    () => confirmGrouping(run.id, run.revision),
    { key: "confirm", fallback: "Could not confirm the claims" },
  );
  if (!selected) return <div className="empty-state"><h2>No proposed claims</h2><p>Open Activity to inspect why no claims were found.</p></div>;
  return <div className="organize-workbench">
    <aside className="claim-index" aria-label="Claims"><label className="mobile-claim-select">Claim<select value={selected.id} onChange={(e) => choose(e.target.value)}>{cases.map((c) => <option value={c.id} key={c.id}>{c.label}</option>)}</select></label><div className="desktop-claim-index"><h2>Claims</h2>{cases.map((c) => { const p = run.grouping?.by_case?.[c.id]?.length || 0; return <button key={c.id} onClick={() => choose(c.id)} className={c.id === selected.id ? "selected" : ""} aria-current={c.id === selected.id ? "true" : undefined}><span>{c.label}</span><small>{p ? `${p} blocking` : c.claimant.state === "confirmed" ? "Ready" : "Needs confirmation"}</small></button>; })}</div></aside>
    <main className="claim-editor"><header><div><span className="eyebrow">Selected claim</span><h2>{selected.label}</h2></div><button className="btn files-button" onClick={() => setDrawer(true)}>Submitted files ({artifacts.length})</button></header>{problems.length ? <div className="alert danger"><div><strong>{problems.length} blocking problem{problems.length === 1 ? "" : "s"}</strong>{problems.map((p) => <p key={p}>{p}</p>)}</div></div> : null}<ClaimIdentity run={run} claim={selected} reload={reload} /><section className="editor-section"><h3>Claim summary</h3><dl><div><dt>Reported total</dt><dd>{selected.reported_total || "Not stated"}</dd></div><div><dt>Lines total</dt><dd>{selected.lines_total || "Not available"}</dd></div><div><dt>Grouping basis</dt><dd>{selected.grouping_basis || selected.reason || "Proposed from submitted files"}</dd></div></dl></section><section className="editor-section"><h3>Mileage source</h3><p>{selected.roles?.mileage_tab || "No separate mileage source assigned"}</p></section><section className="editor-section"><h3>Assigned evidence</h3><div className="compact-files">{assigned.map((a) => <div key={a.id}><span>{a.path}</span><small>{a.proposed_role.replaceAll("_", " ")} · {a.disposition}</small></div>)}</div></section>{run.grouping?.actions_enabled ? <ClaimAdvancedActions key={selected.id} run={run} selected={selected} cases={cases} assigned={assigned} unassigned={unassigned} action={action} /> : null}</main>
    <SubmittedFiles open={drawer} onClose={() => setDrawer(false)} artifacts={artifacts} unassigned={unassigned} busy={Boolean(action.busy)} actionsEnabled={Boolean(run.grouping?.actions_enabled)} selected={selected} settle={settle} assign={assign} setRole={setRole} />
    <div className="decision-bar organize-decision"><span>{run.grouping?.ok ? `${cases.length} claims ready to check` : `${run.grouping?.problems.length || 0} blocking problems must be resolved`}</span><button className="btn primary" disabled={!run.grouping?.ok || Boolean(action.busy) || run.status !== "map_ready"} onClick={confirm}>{action.busy === "confirm" ? "Starting…" : "Confirm claims and start checks"}</button>{action.error ? <span className="error">{action.error}</span> : null}</div>
  </div>;
}

// Keyed by the selected Claim (see above): the ticked files and typed names
// belong to one Claim and start empty when another is selected.
function ClaimAdvancedActions({ run, selected, cases, assigned, unassigned, action }: {
  run: ClaimsRunDetail; selected: ClaimCase; cases: ClaimCase[]; assigned: ClaimArtifact[];
  unassigned: ClaimArtifact[]; action: Action;
}) {
  const mergeTargets = cases.filter((claim) => claim.id !== selected.id);
  const [mergeInto, setMergeInto] = useState(mergeTargets[0]?.id || "");
  const [splitLabel, setSplitLabel] = useState("");
  const [splitIds, setSplitIds] = useState<Set<string>>(new Set());
  const [createLabel, setCreateLabel] = useState("");
  const [createIds, setCreateIds] = useState<Set<string>>(new Set());
  const effectiveMergeInto = mergeTargets.some((claim) => claim.id === mergeInto) ? mergeInto : mergeTargets[0]?.id || "";
  const toggle = (current: Set<string>, id: string, update: (next: Set<string>) => void) => {
    const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); update(next);
  };
  const merge = () => {
    const target = cases.find((claim) => claim.id === effectiveMergeInto);
    if (!target || !window.confirm(`Merge all files and findings from ${selected.label} into ${target.label}? This changes the whole Claim.`)) return;
    void action.run(() => mergeCase(run.id, run.revision, selected.id, target.id), { key: "merge", fallback: "Could not merge the claims" });
  };
  const split = async () => {
    const ok = await action.run(() => splitCase(run.id, run.revision, selected.id, [...splitIds], splitLabel.trim()), { key: "split", fallback: "Could not split the claim" });
    if (ok) { setSplitIds(new Set()); setSplitLabel(""); }
  };
  const create = async () => {
    const ok = await action.run(() => createCase(run.id, run.revision, createLabel.trim(), [...createIds]), { key: "create", fallback: "Could not create the claim" });
    if (ok) { setCreateIds(new Set()); setCreateLabel(""); }
  };
  return <details className="advanced-actions"><summary>Advanced actions</summary><div className="advanced-action-grid">
    <section><h4>Merge Claim</h4><p>Move this entire Claim into another.</p><select aria-label="Merge into Claim" value={effectiveMergeInto} onChange={(event) => setMergeInto(event.target.value)}>{mergeTargets.map((claim) => <option key={claim.id} value={claim.id}>{claim.label}</option>)}</select><button className="btn danger" disabled={!effectiveMergeInto || Boolean(action.busy)} onClick={merge}>Merge Claim</button></section>
    <section><h4>Split selected files</h4><p>Create a new Claim from some files in this Claim.</p><input aria-label="New split Claim name" placeholder="New Claim name" value={splitLabel} onChange={(event) => setSplitLabel(event.target.value)} />{assigned.map((artifact) => <label className="check-row" key={artifact.id}><input type="checkbox" checked={splitIds.has(artifact.id)} onChange={() => toggle(splitIds, artifact.id, setSplitIds)} /><span>{artifact.path.split("/").pop()}</span></label>)}<button className="btn" disabled={!splitLabel.trim() || !splitIds.size || splitIds.size >= assigned.length || Boolean(action.busy)} onClick={split}>Split into new Claim</button></section>
    {unassigned.length ? <section><h4>Create Claim</h4><p>Start a new Claim from unassigned files.</p><input aria-label="New Claim name" placeholder="New Claim name" value={createLabel} onChange={(event) => setCreateLabel(event.target.value)} />{unassigned.map((artifact) => <label className="check-row" key={artifact.id}><input type="checkbox" checked={createIds.has(artifact.id)} onChange={() => toggle(createIds, artifact.id, setCreateIds)} /><span>{artifact.path.split("/").pop()}</span></label>)}<button className="btn" disabled={!createLabel.trim() || !createIds.size || Boolean(action.busy)} onClick={create}>Create Claim</button></section> : null}
  </div></details>;
}

function ClaimIdentity({ run, claim, reload }: { run: ClaimsRunDetail; claim: ClaimCase; reload: () => Promise<void> }) {
  const [name, setName] = useState(claim.claimant.name || ""), [identifier, setIdentifier] = useState(claim.claimant.identifier || "");
  const action = useAction(reload, "Could not save the claimant");
  useEffect(() => { setName(claim.claimant.name || ""); setIdentifier(claim.claimant.identifier || ""); }, [claim.id, claim.claimant.name, claim.claimant.identifier]);
  const save = () => action.run(() => setClaimant(run.id, run.revision, claim.id, name, identifier), { key: "claimant" });
  return <section className="editor-section"><h3>Claimant</h3><div className="field-grid"><label>Name<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>Identifier<input value={identifier} onChange={(e) => setIdentifier(e.target.value)} /></label></div><div className="section-actions"><span className={`status ${claim.claimant.state === "confirmed" ? "ready" : "working"}`}>{claim.claimant.state}</span><button className="btn" disabled={Boolean(action.busy) || !name.trim()} onClick={save}>{action.busy ? "Saving…" : "Save claimant"}</button></div>{action.error ? <p className="error">{action.error}</p> : null}</section>;
}

function SubmittedFiles({ open, onClose, artifacts, unassigned, busy, actionsEnabled, selected, settle, assign, setRole }: { open: boolean; onClose: () => void; artifacts: ClaimArtifact[]; unassigned: ClaimArtifact[]; busy: boolean; actionsEnabled: boolean; selected: ClaimCase; settle: (a: ClaimArtifact, d: "irrelevant" | "duplicate" | "unreadable") => void; assign: (a: ClaimArtifact) => void; setRole: (a: ClaimArtifact, role: ArtifactRole, remember: boolean) => void }) {
  return <aside className={`submitted-files ${open ? "open" : ""}`} aria-label="Submitted files"><header><div><h2>Submitted files</h2><p>{unassigned.length} unassigned</p></div><button className="icon-button files-button" onClick={onClose} aria-label="Close submitted files">×</button></header>{artifacts.map((artifact) => <SubmittedFile key={artifact.id} artifact={artifact} busy={busy} actionsEnabled={actionsEnabled} selected={selected} settle={settle} assign={assign} setRole={setRole} />)}</aside>;
}

const ARTIFACT_ROLES: ArtifactRole[] = ["report", "receipts", "approval", "report_copy", "listing", "roster", "policy", "other", "unknown"];

function SubmittedFile({ artifact, busy, actionsEnabled, selected, settle, assign, setRole }: { artifact: ClaimArtifact; busy: boolean; actionsEnabled: boolean; selected: ClaimCase; settle: (a: ClaimArtifact, d: "irrelevant" | "duplicate" | "unreadable") => void; assign: (a: ClaimArtifact) => void; setRole: (a: ClaimArtifact, role: ArtifactRole, remember: boolean) => void }) {
  const [role, setRoleValue] = useState<ArtifactRole>(artifact.proposed_role);
  const [remember, setRemember] = useState(false);
  useEffect(() => setRoleValue(artifact.proposed_role), [artifact.proposed_role]);
  const canAssign = actionsEnabled && artifact.case_id !== selected.id;
  const showActions = actionsEnabled || artifact.disposition === "unresolved";
  return <div className="submitted-file"><strong>{artifact.path.split("/").pop()}</strong><span>{artifact.case_id ? "Assigned to Claim" : "Unassigned file"} · {artifact.disposition}</span>{showActions ? <details><summary>File actions</summary>{canAssign ? <button disabled={busy} onClick={() => assign(artifact)}>Assign to {selected.label}</button> : null}{actionsEnabled ? <div className="file-role-action"><label>File role<select value={role} onChange={(event) => setRoleValue(event.target.value as ArtifactRole)}>{ARTIFACT_ROLES.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label><label className="check-row"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />Remember for this workspace</label><button disabled={busy || role === artifact.proposed_role} onClick={() => setRole(artifact, role, remember)}>Update file role</button></div> : null}{artifact.disposition === "unresolved" ? <><button disabled={busy} onClick={() => settle(artifact, "irrelevant")}>Mark irrelevant</button><button disabled={busy} onClick={() => settle(artifact, "duplicate")}>Mark duplicate</button><button disabled={busy} onClick={() => settle(artifact, "unreadable")}>Mark unreadable</button></> : null}</details> : null}</div>;
}

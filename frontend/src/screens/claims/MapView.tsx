import { Fragment, useMemo, useState } from "react";
import {
  ClaimMap,
  ClaimsRunDetail,
  MapEmployee,
  MapFile,
  claimsFileUrl,
  confirmClaimMap,
} from "../../api";
import { Reload, useAction } from "../../hooks/useAction";

// Map & Rules: confirm the agent's map with one click; correct it if
// needed. One row per subfolder; every role is a dropdown; the agent's
// reason sits beside every judgement (hover, or expand the row).

const ROLE_LABEL: Record<MapFile["role"], string> = {
  report: "Expense report",
  receipts: "Receipts",
  ignore: "Ignore",
  unplaced: "Unplaced — please decide",
};

/** '*_Approval.pdf' from 'Aegene Ong_Approval.pdf': the employee-specific
 *  prefix becomes a wildcard so the rule carries to every employee. */
export function patternFor(path: string): string {
  const name = path.split("/").pop() ?? path;
  const i = name.lastIndexOf("_");
  return i > 0 ? "*" + name.slice(i) : name;
}

/** One thing that stops the map being confirmed, carrying the FOLDER it
 *  belongs to. The folder is the key: a row shows its own problems by
 *  identity, never by matching the start of the message text (a name that
 *  is the start of another—"Ali" of "Alicia"—would borrow it). */
export interface MapProblem {
  /** The employee folder, "" for a problem that belongs to no row. */
  folder: string;
  message: string;
}

/** Client-side mirror of the server's validation, so the Confirm button
 *  can say why it is disabled. The server checks again. */
export function mapProblems(map: ClaimMap, run: ClaimsRunDetail): MapProblem[] {
  const survey = "files" in run.survey ? run.survey : null;
  const byPath = new Map((survey?.files ?? []).map((f) => [f.path, f]));
  const problems: MapProblem[] = [];
  const codes = new Map<string, string>();
  for (const e of map.employees) {
    if (!e.is_employee || e.skip) continue;
    const label = e.name || e.folder;
    const say = (message: string) => problems.push({ folder: e.folder, message });
    const receipts = e.files.filter((f) => f.role === "receipts");
    if (e.no_report) {
      if (receipts.length === 0) say(`${label}: no report and no receipt files — add one or skip`);
    } else if (!e.report_file) {
      say(`${label}: choose the report file, or mark “no report”`);
    } else {
      const f = byPath.get(e.report_file);
      if (!f || f.type !== "workbook") say(`${label}: the report must be a workbook`);
      else if (!e.report_tab) say(`${label}: choose the report tab`);
    }
    if (!e.name.trim()) say(`${e.folder}: the employee needs a name`);
    const code = e.er_code.trim();
    if (code) {
      if (codes.has(code)) say(`${label}: ER code ${code} is also used by ${codes.get(code)}`);
      codes.set(code, label);
    }
  }
  return problems;
}

export default function MapView({
  run,
  onChanged,
  onConfirmed,
}: {
  run: ClaimsRunDetail;
  /** Re-read the run (a stale-run 409 reloads through this). */
  onChanged: Reload;
  onConfirmed: () => void;
}) {
  const initial = ("employees" in run.map ? run.map : { employees: [], root_files: [], notes: [] }) as ClaimMap;
  const [map, setMap] = useState<ClaimMap>(() => JSON.parse(JSON.stringify(initial)));
  // path -> pattern the reviewer ticked "remember" on
  const [remember, setRemember] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // Confirming goes through the shared action hook, like every other
  // mutation: the reload is awaited before the button is released and a
  // stale run (409) reloads the screen before it says so.
  const action = useAction(onChanged, "Could not confirm the map");
  const busy = !!action.busy;
  const survey = "files" in run.survey ? run.survey : null;
  const byPath = useMemo(() => new Map((survey?.files ?? []).map((f) => [f.path, f])), [survey]);
  const original = useMemo(() => {
    const m = new Map<string, MapFile["role"]>();
    for (const e of initial.employees) for (const f of e.files) m.set(f.path, f.role);
    return m;
  }, [initial]);
  const originalReason = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of initial.employees) for (const f of e.files) m.set(f.path, f.reason);
    return m;
  }, [initial]);
  const problems = mapProblems(map, run);
  const confirmed = !!initial.confirmed || run.status !== "map_ready";
  const changedFiles = map.employees.flatMap((e) => e.files.filter((f) => original.get(f.path) !== f.role));

  function update(i: number, patch: Partial<MapEmployee>) {
    setMap((m) => {
      const employees = m.employees.map((e, j) => (j === i ? { ...e, ...patch } : e));
      return { ...m, employees };
    });
  }

  function setRole(i: number, path: string, role: MapFile["role"]) {
    setMap((m) => {
      const e = m.employees[i];
      // Changing a role keeps the agent's original reason visible; changing
      // it back restores the original wording.
      const files = e.files.map((f) =>
        f.path !== path
          ? f
          : role === original.get(path)
            ? { ...f, role, reason: originalReason.get(path) ?? f.reason }
            : { ...f, role, reason: `set by the reviewer (the agent said: ${originalReason.get(path) ?? f.reason})` }
      );
      const patch: Partial<MapEmployee> = { files };
      // Exactly one report per employee: choosing a new one unchooses the old.
      if (role === "report") {
        patch.report_file = path;
        patch.no_report = false;
        const tabs = Object.keys(byPath.get(path)?.peek?.tabs ?? {});
        patch.report_tab = e.report_tab && tabs.includes(e.report_tab) ? e.report_tab : tabs[0] ?? null;
        patch.files = files.map((f) => (f.path !== path && f.role === "report" ? { ...f, role: "ignore" as const } : f));
      } else if (e.report_file === path) {
        patch.report_file = null;
        patch.report_tab = null;
      }
      const employees = m.employees.map((x, j) => (j === i ? { ...x, ...patch } : x));
      return { ...m, employees };
    });
  }

  function confirm() {
    const rules = changedFiles
      .filter((f) => remember[f.path])
      .map((f) => ({ pattern: patternFor(f.path), role: f.role }));
    // The revision the screen loaded travels with the map: the server
    // answers 409 if the run moved on, and the reviewer is not silently
    // confirming a map built from stale data.
    return action.run(async () => {
      await confirmClaimMap(run.id, map, rules, run.revision);
      onConfirmed();
    });
  }

  return (
    <div>
      {run.map_warnings.length > 0 && (
        <div className="card banner bad">
          <b>The map audit could not settle {run.map_warnings.length} thing(s)</b>
          <ul className="muted">
            {run.map_warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <span className="sub">Fix them in the table below, then confirm.</span>
        </div>
      )}
      <p className="summary-line">
        <b>
          {map.employees.filter((e) => e.is_employee && !e.skip).length} employees to verify
        </b>{" "}
        <span className="sub">
          The agent looked inside every file and proposed this map
          {initial.rounds ? ` (settled on round ${initial.rounds})` : ""}. Hover a role for its
          reason, or expand a row. Change anything that is wrong; tick “remember” to keep a
          correction for {run.client}.
        </span>
      </p>
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table className="table maptable">
          <thead>
            <tr>
              <th></th>
              <th>Folder</th>
              <th>Employee?</th>
              <th>Name</th>
              <th>ER code</th>
              <th>Report file + tab</th>
              <th>Mileage tab</th>
              <th>Receipts</th>
              <th>Ignored</th>
              <th>Unplaced</th>
              <th>Skip</th>
            </tr>
          </thead>
          <tbody>
            {map.employees.map((e, i) => {
              const workbooks = e.files.filter((f) => byPath.get(f.path)?.type === "workbook");
              const tabs = e.report_file ? Object.keys(byPath.get(e.report_file)?.peek?.tabs ?? {}) : [];
              const receipts = e.files.filter((f) => f.role === "receipts");
              const ignored = e.files.filter((f) => f.role === "ignore");
              const unplaced = e.files.filter((f) => f.role === "unplaced");
              const open = !!expanded[e.folder];
              const rowProblems = problems.filter((p) => p.folder === e.folder);
              return (
                <Fragment key={e.folder}>
                  <tr className={unplaced.length || rowProblems.length ? "attention" : ""}>
                    <td>
                      <button
                        className="btn"
                        aria-label={open ? "Hide reasons" : "Show reasons"}
                        title={e.reason}
                        onClick={() => setExpanded({ ...expanded, [e.folder]: !open })}
                      >
                        {open ? "▾" : "▸"}
                      </button>
                    </td>
                    <td className="mono" title={e.reason}>{e.folder}</td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`${e.folder} is an employee`}
                        checked={e.is_employee}
                        disabled={confirmed}
                        onChange={(ev) => update(i, { is_employee: ev.target.checked })}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`${e.folder} employee name`}
                        value={e.name}
                        disabled={confirmed || !e.is_employee}
                        onChange={(ev) => update(i, { name: ev.target.value })}
                        style={{ width: 120 }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`${e.folder} ER code`}
                        value={e.er_code}
                        disabled={confirmed || !e.is_employee}
                        onChange={(ev) => update(i, { er_code: ev.target.value })}
                        style={{ width: 150 }}
                      />
                    </td>
                    <td>
                      {e.is_employee && (
                        <>
                          <label className="sub" title={e.files.find((f) => f.role === "report")?.reason ?? ""}>
                            <input
                              type="checkbox"
                              aria-label={`${e.folder} has no report`}
                              checked={e.no_report}
                              disabled={confirmed}
                              onChange={(ev) =>
                                update(i, {
                                  no_report: ev.target.checked,
                                  ...(ev.target.checked
                                    ? { report_file: null, report_tab: null, files: e.files.map((f) => (f.role === "report" ? { ...f, role: "ignore" as const } : f)) }
                                    : {}),
                                })
                              }
                            />{" "}
                            no report — build rows from receipts
                          </label>
                          {!e.no_report && (
                            <>
                              <select
                                aria-label={`${e.folder} report file`}
                                value={e.report_file ?? ""}
                                disabled={confirmed}
                                title={e.files.find((f) => f.path === e.report_file)?.reason ?? ""}
                                onChange={(ev) => ev.target.value && setRole(i, ev.target.value, "report")}
                              >
                                <option value="">— choose —</option>
                                {workbooks.map((f) => (
                                  <option key={f.path} value={f.path}>{f.path.split("/").pop()}</option>
                                ))}
                              </select>
                              <select
                                aria-label={`${e.folder} report tab`}
                                value={e.report_tab ?? ""}
                                disabled={confirmed || !e.report_file}
                                onChange={(ev) => update(i, { report_tab: ev.target.value || null })}
                              >
                                <option value="">— tab —</option>
                                {tabs.map((t) => (
                                  <option key={t} value={t}>{t}</option>
                                ))}
                              </select>
                            </>
                          )}
                        </>
                      )}
                    </td>
                    <td>
                      {e.is_employee && !e.no_report && (
                        <select
                          aria-label={`${e.folder} mileage tab`}
                          value={e.mileage_tab ?? ""}
                          disabled={confirmed || !e.report_file}
                          onChange={(ev) => update(i, { mileage_tab: ev.target.value || null })}
                        >
                          <option value="">— none —</option>
                          {tabs.map((t) => (
                            <option key={t} value={t}>{t}</option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td>{receipts.length ? receipts.map((f) => <span key={f.path} className="pill" title={f.reason}>{f.path.split("/").pop()}</span>) : <span className="sub">none</span>}</td>
                    <td>{ignored.length ? ignored.map((f) => <span key={f.path} className="pill muted" title={f.reason}>{f.path.split("/").pop()}</span>) : <span className="sub">—</span>}</td>
                    <td>
                      {unplaced.length ? (
                        unplaced.map((f) => <span key={f.path} className="pill warn" title={f.reason}>{f.path.split("/").pop()}</span>)
                      ) : (
                        <span className="sub">—</span>
                      )}
                    </td>
                    <td>
                      {e.is_employee && (
                        <input
                          type="checkbox"
                          aria-label={`skip ${e.folder}`}
                          checked={!!e.skip}
                          disabled={confirmed}
                          onChange={(ev) => update(i, { skip: ev.target.checked })}
                        />
                      )}
                    </td>
                  </tr>
                  {open && (
                    <tr className="detail">
                      <td></td>
                      <td colSpan={10}>
                        <p className="basis">Why this folder: {e.reason}</p>
                        <table className="table inner">
                          <tbody>
                            {e.files.map((f) => {
                              const changed = original.get(f.path) !== f.role;
                              const info = byPath.get(f.path);
                              return (
                                <tr key={f.path}>
                                  <td className="mono">{f.path.split("/").pop()}</td>
                                  <td className="sub">
                                    {info?.type}
                                    {info?.pages ? `, ${info.pages} page(s)` : ""}
                                    {info?.er_code ? `, ${info.er_code}` : ""}
                                  </td>
                                  <td>
                                    <select
                                      aria-label={`role of ${f.path}`}
                                      value={f.role}
                                      disabled={confirmed}
                                      onChange={(ev) => setRole(i, f.path, ev.target.value as MapFile["role"])}
                                    >
                                      {(Object.keys(ROLE_LABEL) as MapFile["role"][]).map((r) => (
                                        <option key={r} value={r}>{ROLE_LABEL[r]}</option>
                                      ))}
                                    </select>
                                  </td>
                                  <td className="reason-cell">{f.reason}</td>
                                  <td>
                                    {changed && !confirmed && (
                                      <label className="sub">
                                        <input
                                          type="checkbox"
                                          checked={!!remember[f.path]}
                                          onChange={(ev) => setRemember({ ...remember, [f.path]: ev.target.checked })}
                                        />{" "}
                                        remember “{patternFor(f.path)} → {f.role}” for {run.client}
                                      </label>
                                    )}
                                    {(info?.type === "pdf" || info?.type === "image") && (
                                      <a className="sub" href={claimsFileUrl(run.id, f.path, 1)} target="_blank" rel="noreferrer">
                                        {" "}
                                        view page 1
                                      </a>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {map.root_files.length > 0 && (
        <p className="sub">
          Files directly in the batch folder (not inside any employee folder):{" "}
          {map.root_files.map((f) => `${f.path} (${ROLE_LABEL[f.role]} — ${f.reason})`).join("; ")}
        </p>
      )}
      {!confirmed && (
        <div className="actions">
          <button
            className="btn primary"
            disabled={busy || problems.length > 0}
            title={problems.length ? problems.map((p) => p.message).join("\n") : "Save this map and start verifying every employee"}
            onClick={confirm}
          >
            {busy ? "Starting verification…" : "Confirm & verify"}
          </button>
          {problems.length > 0 && (
            <span className="sub">
              Not ready: {problems[0].message}
              {problems.length > 1 ? ` (+${problems.length - 1} more)` : ""}
            </span>
          )}
        </div>
      )}
      {confirmed && <p className="sub">This map was confirmed; verification has started.</p>}
      {action.error && <p className="error">{action.error}</p>}
    </div>
  );
}

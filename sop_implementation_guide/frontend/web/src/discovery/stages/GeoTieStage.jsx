import { useEffect, useRef, useState } from "react";

const GEO_OPTIONS = ["federal", "state:MD", "county:24033", "tract:NMTC", "unknown"];
const CUSTOM = "__custom__";

export default function GeoTieStage({ payload, onValueChange }) {
  const programs = payload.programs || [];
  const initial = useRef(Object.fromEntries(programs.map((pr) => [pr.program_name, pr.recommended_geo_scope || "unknown"])));
  const [scopes, setScopes] = useState(() => ({ ...initial.current }));
  const [customOpen, setCustomOpen] = useState({});

  useEffect(() => {
    const overrides = {};
    for (const [name, scope] of Object.entries(scopes)) {
      if (scope !== initial.current[name]) overrides[name] = scope;
    }
    onValueChange(Object.keys(overrides).length ? { geo: overrides } : "approved");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopes]);

  return (
    <>
      <div className="banner ok">Last step. On submit, everything you have reviewed is written to <code>funding_programs</code>.</div>
      <div className="table-wrap">
        <table>
          <thead><tr><th style={{ width: "60%" }}>Program</th><th>Geographic reach</th></tr></thead>
          <tbody>
            {programs.length === 0 ? (
              <tr><td colSpan={2} className="assess">No programs to tie.</td></tr>
            ) : (
              programs.map((pr) => {
                const rec = pr.recommended_geo_scope || "unknown";
                const current = scopes[pr.program_name] ?? rec;
                const isCustom = customOpen[pr.program_name] || !GEO_OPTIONS.includes(current);
                return (
                  <tr key={pr.program_name}>
                    <td>
                      <div><b>{pr.program_name}</b></div>
                      {pr.reason && <div className="assess">{pr.reason}</div>}
                    </td>
                    <td>
                      <select
                        value={isCustom ? CUSTOM : current}
                        onChange={(e) => {
                          const v = e.target.value;
                          setCustomOpen((prev) => ({ ...prev, [pr.program_name]: v === CUSTOM }));
                          if (v !== CUSTOM) setScopes((prev) => ({ ...prev, [pr.program_name]: v }));
                        }}
                      >
                        {GEO_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
                        <option value={CUSTOM}>(custom…)</option>
                      </select>
                      {isCustom && (
                        <input
                          style={{ display: "block", marginTop: ".3rem" }}
                          placeholder="e.g. tract:LIC"
                          defaultValue={GEO_OPTIONS.includes(current) ? "" : current}
                          onChange={(e) => setScopes((prev) => ({ ...prev, [pr.program_name]: e.target.value.trim() || "unknown" }))}
                        />
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <div className="muted-note">Submitting sends only the programs whose scope you changed. Send <code>approved</code> to accept every recommendation.</div>
    </>
  );
}

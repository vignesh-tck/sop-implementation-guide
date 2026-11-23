// Who does a plan step, as one of four kinds, each with its own icon and colour
// (the colours are the .actor-* classes in index.css).

export const ACTORS = {
  agent: { label: "Agent", desc: "Done by this system: the zoning lookup or the funding agent" },
  city: { label: "City", desc: "City of Bowie staff, council or contractors it hires" },
  builder: { label: "Builder", desc: "The property owner, developer or their contractor" },
  other: { label: "Other", desc: "Anyone else: the state (MD SHA), the county, M-NCPPC, boards" },
};

export const ACTOR_KEYS = Object.keys(ACTORS);

const PATHS = {
  // A robot head.
  agent: (
    <>
      <rect x="5" y="8" width="14" height="11" rx="3" />
      <path d="M12 8V5" />
      <circle cx="12" cy="4" r="1" />
      <circle cx="9.5" cy="13" r=".9" fill="currentColor" />
      <circle cx="14.5" cy="13" r=".9" fill="currentColor" />
      <path d="M9.5 16.3h5" />
    </>
  ),
  // A city hall: pediment and columns.
  city: (
    <>
      <path d="M12 3 3.5 7.5h17L12 3z" />
      <path d="M5 10v7M9.7 10v7M14.3 10v7M19 10v7" />
      <path d="M3 20.5h18M4 17.5h16" />
    </>
  ),
  // A hard hat.
  builder: (
    <>
      <path d="M4.5 16a7.5 7.5 0 0 1 15 0" />
      <path d="M2.5 16h19v2.5h-19z" />
      <path d="M10 9.3V6.2a2 2 0 0 1 4 0v3.1" />
    </>
  ),
  // Two people.
  other: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 20a5.5 5.5 0 0 1 11 0" />
      <circle cx="17" cy="9" r="2.3" />
      <path d="M15.6 14.3A4.6 4.6 0 0 1 21 18.8" />
    </>
  ),
};

export function ActorIcon({ kind, size = 16 }) {
  return (
    <svg className="actor-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {PATHS[kind] || PATHS.other}
    </svg>
  );
}

export function ClockIcon({ size = 13 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

// An actor as a small chip: icon and name.
export function ActorChip({ kind, owner }) {
  const a = ACTORS[kind] || ACTORS.other;
  return (
    <span className={"actor-chip actor-" + (ACTORS[kind] ? kind : "other")} title={a.desc}>
      <ActorIcon kind={kind} size={14} />
      {a.label}{owner ? <span className="actor-owner"> · {owner}</span> : null}
    </span>
  );
}

export const actorOf = (s) => (ACTORS[s.actor] ? s.actor : "other");

// Who does how many steps, for the top of a plan. With onPick, each chip is a
// filter: clicking one shows only that actor's steps, clicking it again shows all.
export function ActorLegend({ steps, active = null, onPick }) {
  const count = Object.fromEntries(ACTOR_KEYS.map((k) => [k, 0]));
  for (const s of steps) count[actorOf(s)]++;
  return (
    <span className="actor-legend">
      {ACTOR_KEYS.filter((k) => count[k]).map((k) => {
        const body = <><ActorIcon kind={k} size={14} />{ACTORS[k].label} <b>{count[k]}</b></>;
        return onPick ? (
          <button key={k} type="button" className={"actor-chip actor-" + k + (active === k ? " on" : "")}
                  aria-pressed={active === k} title={active === k ? "Show all steps" : `Show only ${ACTORS[k].label} steps`}
                  onClick={() => onPick(active === k ? null : k)}>
            {body}
          </button>
        ) : (
          <span key={k} className={"actor-chip actor-" + k} title={ACTORS[k].desc}>{body}</span>
        );
      })}
    </span>
  );
}

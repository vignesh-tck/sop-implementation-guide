import { num } from "../api.js";

export default function ProfileCard({ profile, title }) {
  const actions = profile.top_actions || [];
  return (
    <div className="card">
      <h4>{title}</h4>
      <div className="scores">
        <div>
          <div className="big">{num(profile.feasibility_score)}</div>
          <div className="kv">feasibility / 100</div>
        </div>
        <div className="kv">
          funding <b>{num(profile.funding_score)}</b>
        </div>
        <div className="kv">
          zoning <b>{num(profile.zoning_score)}</b>{" "}
          <span title="ZoningAgent is a scaffold — placeholder value">*</span>
        </div>
        <div className="kv">
          policy <b>{num(profile.policy_score)}</b>{" "}
          <span title="PolicyAgent is a scaffold — placeholder value">*</span>
        </div>
      </div>
      <div className="kv" style={{ marginTop: ".5rem" }}>
        * zoning and policy are placeholder 50s until those agents are implemented.
      </div>
      {actions.length > 0 && (
        <ol className="actions">
          {actions.map((a, i) => {
            const m = String(a).match(/^(.*?)(https?:\/\/\S+)$/);
            return (
              <li key={i}>
                {m ? (
                  <>
                    {m[1]}
                    <a href={m[2]} target="_blank" rel="noopener noreferrer">
                      {m[2]}
                    </a>
                  </>
                ) : (
                  a
                )}
              </li>
            );
          })}
        </ol>
      )}
      {profile.narrative && <div className="narrative">{profile.narrative}</div>}
    </div>
  );
}

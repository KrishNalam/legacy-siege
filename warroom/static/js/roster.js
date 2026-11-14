// Comms live roster card (SWARM_COMMS=1): one line per agent from swarm.roster — state dot, id, what it is
// doing and where, its parent Scout and any takeover. Self-contained: adopt by calling installRoster(cardEl)
// (or reuse rosterRows() in another layout). Data: snapshot.roster, change stream coll "roster", GET /api/roster.
import { state, on } from "./store.js";

const ORDER = { working: 0, dead: 1, idle: 2, standby: 3, retired: 4 };

export function rosterRows() {
  return [...state.roster.values()].sort((a, b) =>
    (ORDER[a.state] ?? 5) - (ORDER[b.state] ?? 5) || String(a._id).localeCompare(String(b._id)));
}

export function installRoster(card) {
  if (!card) return;
  const list = card.querySelector("ol"), count = card.querySelector("#roster-count");
  let t = null;
  const render = () => {
    const rows = rosterRows();
    card.hidden = rows.length === 0;
    if (!rows.length) return;
    const n = (s) => rows.filter((r) => r.state === s).length;
    count.textContent = `${n("working")} busy · ${n("dead")} dead · ${rows.length} agents`;
    list.replaceChildren(...rows.map((r) => {
      const li = document.createElement("li");
      li.className = `${r.state || ""}${r.took_over ? " took" : ""}`;
      const i = document.createElement("i"), b = document.createElement("b"), span = document.createElement("span");
      b.textContent = r._id;
      span.textContent = r.doing || r.state || "";
      if (r.parent) { const sm = document.createElement("small"); sm.textContent = ` ← ${r.parent}`; span.append(sm); }
      li.title = [r.doing, r.parent && `parent ${r.parent}`, r.spawned_by && `spawned by ${r.spawned_by}`]
        .filter(Boolean).join(" · ");
      li.append(i, b, span);
      return li;
    }));
  };
  const soon = () => { clearTimeout(t); t = setTimeout(render, 150); };
  on("snapshot", render);
  on("change:roster", soon);
}

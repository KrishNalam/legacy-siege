// Legacy Siege war room — entry point. Wires the change-stream store to the stage and panels.
import { state, on, connect, prettyTime } from "./js/store.js";
import { Stage } from "./js/stage.js";
import { Panels } from "./js/panels.js";
import { installBeats } from "./js/beats.js";

const panels = new Panels({ onInspect: (h) => inspect(h) });
const stage = new Stage(document.getElementById("stage"), { onInspect: (h) => inspect(h) });

function inspect(h) {
  if (h.kind === "atlas") return panels.openAtlas(h.coll);
  if (h.db && h.coll && h.id != null) return panels.inspect(h);
}

// cinematic captions (DOM, crisp at any scale); suppressed briefly after a snapshot so replayed
// buffered events don't re-announce old news
let quietUntil = 0;
const capEl = document.getElementById("captions");
function caption(text, small = "", color = "#4cc9f0", ms = 4200) {
  if (performance.now() < quietUntil) return;
  const el = document.createElement("div");
  el.className = "caption"; el.style.setProperty("--c", color);
  el.innerHTML = `<span></span>${small ? "<small></small>" : ""}`;
  el.firstChild.textContent = prettyTime(text); if (small) el.lastChild.textContent = prettyTime(small);
  capEl.prepend(el);
  while (capEl.children.length > 3) capEl.lastChild.remove();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 500); }, ms);
}
window.addEventListener("caption", (e) => caption(e.detail.text, e.detail.small, e.detail.color));

installBeats(stage, panels, caption);

on("snapshot", () => {
  quietUntil = performance.now() + 1200;
  panels.renderTopbar(); panels.renderIncident(); panels.renderActivity(); panels.renderApprovals();
  panels.renderGarden(); panels.refreshScorecard();
});
// bulk deletes (a reset) → re-render every panel from the store once things settle
let rerenderT = null;
on("change", (m) => {
  if (m.op !== "delete") return;
  clearTimeout(rerenderT);
  rerenderT = setTimeout(() => {
    panels.renderTopbar(); panels.renderIncident(); panels.renderActivity(); panels.renderApprovals();
    panels.renderGarden(); panels.refreshScorecard();
  }, 400);
});
on("status", (s) => panels.setConn(s));
on("tick", () => { panels.renderConn(); panels.renderTopbar(); });

connect();
window.__warroom = { state, stage, panels, caption }; // handy in devtools

"""Pure beat-log helpers for scripts.record_demo (no database, no browser), so they can be unit-tested anywhere."""
from __future__ import annotations


def script_beats(events: list[dict], t) -> list[dict]:
    """Beats only the 10-beat script uses (docs/video-script.md); each carries the facts its caption needs.
    `t(event, d)` = event time in video seconds (derive_beats' helper)."""
    def after(name, t0, **match):
        return next((e for e in events if e["name"] == name and e["t"] >= t0
                     and all(e.get(k) == v for k, v in match.items())), None)

    out = []

    def add(name, a, b, **info):
        if a is not None and b is not None and b > a:
            out.append({"name": name, "t_start": a, "t_end": b, **{k: v for k, v in info.items() if v is not None}})

    # Claude, checked: the AI card from "thinking" to its verdict; prefer an incident whose plan ran
    decided = [e for e in events if e["name"] == "ai_decided"]
    pick = next((e for e in decided if e.get("decision") == "ai_plan"), decided[0] if decided else None)
    if pick:
        think = next((e for e in events if e["name"] == "ai_thinking" and e.get("incident") == pick["incident"]), pick)
        add("ai_checked", t(think, -1) if pick["t"] - think["t"] < 8 else t(pick, -6), t(pick, 3.5),
            decision=pick.get("decision"), incident=pick.get("incident"))
    ov = after("ai_overruled", 0)
    if ov:
        add("ai_overruled", t(ov, -1.5), t(ov, 4), op=ov.get("op"), rule=ov.get("rule"), incident=ov.get("incident"))
    # schema drift: monitor notices → mapping learned (Verifier-verified); the repeat → mapping reused
    learned = after("mapping_learned", 0)
    if learned:
        seen = next((e for e in reversed(events) if e["name"] == "drift_noticed" and e["t"] <= learned["t"]), None)
        a = t(seen, -1) if seen and learned["t"] - seen["t"] < 25 else t(learned, -6)
        add("schema_drift", a, t(learned, 3), **{"from": learned.get("frm"), "to": learned.get("to"),
                                                 "version": learned.get("version")})
    reused = after("mapping_reused", 0)
    if reused:
        add("drift_reused", t(reused, -2), t(reused, 3), version=reused.get("version"))
    # Red fleet: a striker FREEZEs a bee (SIGSTOP), its lease expires, a Recovery takes over
    frz = next((e for e in events if e["name"] == "red_freeze" and str(e.get("by") or "").startswith("red-")),
               after("red_freeze", 0))
    if frz:
        took = after("takeover", frz["t"])
        thaw = after("red_thaw", frz["t"], id=frz.get("id"))
        end = t(took, 3) if took and took["t"] - frz["t"] < 60 else t(frz, 14)
        add("red_freeze", t(frz, -3), end, by=frz.get("by"), agent=(thaw or {}).get("agent"),
            takeover_s=round(took["t"] - frz["t"], 1) if took and took["t"] - frz["t"] < 60 else None)
    return out

"""Drive the real Legacy Siege demo through the war room UI and screenshot key moments.

usage: shoot.py OUTDIR [--quick]   (--quick: stop after night 1 resolved)
"""
import json, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8700"
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
QUICK = "--quick" in sys.argv
logs = []
T0 = time.monotonic()


def log(m):
    print(f"[{time.monotonic() - T0:6.1f}s] {m}", flush=True)


def post(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


SUMMARY = """() => {
  const w = window.__warroom; if (!w) return null; const s = w.state;
  const incs = [...s.incidents.values()].sort((a,b)=>Date.parse(a.opened_at)-Date.parse(b.opened_at));
  const inc = incs.filter(i=>i.active).pop() || incs.pop() || null;
  const sys = Object.fromEntries([...s.systems.values()].map(d=>[d._id,{st:d.status,bl:d.blames,v:d.version}]));
  const ag = [...s.agents.values()];
  return {
    agents: ag.length, dead: ag.filter(a=>a.status==='dead').map(a=>a._id),
    sys, modal: !document.getElementById('modal').hidden,
    inc: inc && {id: inc._id, st: inc.status, path: inc.diagnosis_path||[], recall: !!inc.recall, used: inc.recall?.used,
                 handoffs: (inc.handoffs||[]).length, steps: (inc.steps||[]).map(x=>[x.op,x.status,(x.attempts||[]).length]), active: inc.active},
    rejects: s.thoughts.filter(t=>t.kind==='reject').length,
    upg: [...s.upgrades.values()].map(u=>u.status).pop() || null,
    chaos: [...s.chaos.values()].map(c=>c.fault),
    killhit: [...s.chaos.values()].filter(c=>c.fault==='agent_kill' && c.result && c.result.agent).length,
    lies: !!document.querySelector('.stamp'),
    rejected: window.__rejN || 0,
  };
}"""


def main():
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                               args=["--disable-gpu-sandbox", "--force-color-profile=srgb"])
        page = br.new_page(viewport={"width": 1920, "height": 1080})
        page.on("console", lambda m: logs.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
        page.goto(BASE + "/")

        def S():
            return page.evaluate(SUMMARY)

        def shot(name, delay=0.0):
            if delay: page.wait_for_timeout(int(delay * 1000))
            page.screenshot(path=str(OUT / f"{name}.png"))
            log(f"shot {name}")

        def wait(desc, fn, timeout=90, poll=0.15):
            t = time.monotonic()
            while time.monotonic() - t < timeout:
                s = S()
                if s and fn(s): return s
                page.wait_for_timeout(int(poll * 1000))
            log(f"TIMEOUT waiting for {desc}: {S()}")
            return None

        def click_approve():
            page.click("#apr-yes")
            log("clicked Approve")

        # ---------------- reset + idle
        post("/api/reset", {"mode": "full"})
        log("full reset")
        page.wait_for_timeout(1500)
        page.reload()
        wait("ready", lambda s: s["agents"] >= 12 and all(v["st"] == "up" for v in s["sys"].values()) and not (s["inc"] and s["inc"]["active"]), 90)
        page.evaluate("""() => { const st = window.__warroom.stage, o = st.stamp.bind(st); window.__rejN = 0;
          st.stamp = (sys, text, ...r) => { if (text === 'RECORD REJECTED') window.__rejN++; return o(sys, text, ...r); }; }""")
        page.wait_for_timeout(3000)
        fps = page.evaluate("() => new Promise(r => { let n = 0; const t0 = performance.now(); const f = () => { n++; performance.now() - t0 < 2000 ? requestAnimationFrame(f) : r(n / 2); }; requestAnimationFrame(f); })")
        log(f"idle fps (headless, software): {fps}")
        shot("01-idle")

        # ---------------- night 1
        page.click("button[data-scn='bank-night-1']")
        log("clicked Night 1")
        wait("strike", lambda s: "batch_abort" in s["chaos"], 30, 0.05)
        shot("02-chaos-strike", 0.25)
        wait("cascade", lambda s: s["sys"].get("portal", {}).get("bl") == "unix" and s["sys"].get("unix", {}).get("bl") == "mainframe", 40)
        shot("03-cascade", 1.0)
        fps = page.evaluate("() => new Promise(r => { let n = 0; const t0 = performance.now(); const f = () => { n++; performance.now() - t0 < 2000 ? requestAnimationFrame(f) : r(n / 2); }; requestAnimationFrame(f); })")
        log(f"busy fps (headless, software): {fps}")
        wait("diag hop 2", lambda s: s["inc"] and len(s["inc"]["path"]) >= 2, 40, 0.1)
        shot("04-diagnosis-walk", 0.5)
        page.set_viewport_size({"width": 1366, "height": 768})
        shot("16-laptop-1366x768", 0.6)
        page.set_viewport_size({"width": 1920, "height": 1080})
        wait("lie", lambda s: s["rejects"] >= 1, 60, 0.1)
        shot("05-ack-not-applied", 0.6)
        killed = False
        n_apr = 0
        while True:
            s = wait("approval or resolved", lambda s: s["modal"] or (s["inc"] and s["inc"]["st"] == "resolved"), 120)
            if not s or not s["modal"]: break
            n_apr += 1
            shot("06-approval-modal", 0.8) if n_apr == 1 else None
            click_approve()
            if not killed:
                page.wait_for_timeout(200)
                page.click("#btn-kill"); killed = True
                log("clicked Kill a worker")
                wait("sigkill", lambda s: s["killhit"] >= 1, 20, 0.05)
                shot("07-agent-kill", 1.3)
                wait("handoff", lambda s: s["inc"] and s["inc"]["handoffs"] >= 1, 40, 0.1)
                shot("08-nurse-takeover", 1.1)
            page.wait_for_timeout(500)
        wait("resolved", lambda s: s["inc"] and s["inc"]["st"] == "resolved", 90)
        shot("09-resolved", 1.6)
        if QUICK:
            finish(page, br); return

        # ---------------- night 2
        post("/api/reset", {"mode": "next_round"})
        log("next_round reset")
        wait("green", lambda s: all(v["st"] == "up" for v in s["sys"].values()) and not (s["inc"] and s["inc"]["active"]), 40)
        page.wait_for_timeout(2500)
        page.click("button[data-scn='bank-night-2']")
        log("clicked Night 2")
        wait("recall", lambda s: s["inc"] and s["inc"]["active"] and s["inc"]["recall"], 60, 0.1)
        shot("10-recall-golden-path", 1.2)
        while True:
            s = wait("approval or resolved", lambda s: s["modal"] or (s["inc"] and s["inc"]["st"] == "resolved"), 120)
            if not s or not s["modal"]: break
            page.wait_for_timeout(700); click_approve(); page.wait_for_timeout(500)
        wait("resolved 2", lambda s: s["inc"] and s["inc"]["st"] == "resolved", 90)
        shot("11-scorecard", 2.5)

        # ---------------- gardener
        wait("upgrade", lambda s: s["upg"], 40)
        shot_g = False
        while True:
            s = wait("upg approval/proven", lambda s: s["modal"] or s["upg"] in ("proven", "rejected"), 150)
            if not s or not s["modal"]: break
            if not shot_g: shot("12-gardener-approval", 0.8); shot_g = True
            click_approve(); page.wait_for_timeout(500)
            break
        # deploy moment (version bump)
        s = wait("deployed", lambda s: s["sys"].get("mainframe", {}).get("v", 1) >= 2, 60, 0.1)
        if s: shot("13-gardener-deploy", 0.5)
        wait("proven", lambda s: s["upg"] in ("proven", "rejected"), 90)
        shot("14-upgrade-proven", 1.2)
        page.wait_for_timeout(5500)  # let the NEVER AGAIN banner clear first
        rej0 = S()["rejected"]
        page.click("button[data-scn='bank-night-3']")
        log("clicked Night 3")
        s = wait("reject log", lambda s: s["rejected"] > rej0, 40, 0.1)
        shot("15-night3-shrugged-off", 1.0)
        finish(page, br)


def finish(page, br):
    (OUT / "console.txt").write_text("\n".join(logs) or "(no console errors / page errors)")
    log(f"console/page errors: {len(logs)}")
    for l in logs[:30]: print("  ", l)
    br.close()


main()

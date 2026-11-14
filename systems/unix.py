"""xferhost01 — Unix middleware (Solaris-ish). Pulls the mainframe's NIGHTBAT output by cron.

Personality: syslog lines (``CRON[4211]: (xfer) CMD (...)``, ``kernel: ... No space left on
device``), config files with permissions, a spool directory that fills up.

Data flow: every tick the ``xfer_pull`` cron job copies the newest NIGHTBAT.OUTPUT from the
mainframe into ``/var/spool/xfer`` (``transfer.file``). If no new file shows up for a few
retries it *gives up* (``transfer.status = "failed"``) and needs a manual ``rerun_transfer``
— a classic legacy behaviour that makes the fix order matter.

Planted fragility: the spool disk fills; the cron job can die silently (no log line at all).
Planted vuln: ``/etc/xfer.conf`` is world-writable (0666).

Schema drift (``schema_drift`` chaos): a "vendor patch" silently renames a field in the xfer manifest
records it hands to the portal (``balance`` -> ``acct_bal``). Unix itself stays healthy; only the
consumer that still reads the old field breaks. ``GET /unix/state`` exposes a few manifest records
(``feed.sample``) so the Hive can compare them with a known-good sample.
"""
from __future__ import annotations

import json
from datetime import datetime

from systems.base import BATCH_EVERY_TICKS, CASCADE_TICKS, Ctx, LegacySystem, Op, Vuln, iso

SPOOL = "/var/spool/xfer"
CONF = "/etc/xfer.conf"
DISK_FULL_PCT = 95.0
GIVE_UP_TICKS = BATCH_EVERY_TICKS + CASCADE_TICKS  # retries before xfer_pull gives up on the mainframe
FEED = "xfer_manifest"
FEED_SAMPLE = 3                      # manifest records exposed in /state (read-back for schema checks)
NEXT_NAME = {"balance": "acct_bal", "acct_bal": "ledger_bal", "ledger_bal": "bal_amt"}
CONF_TEXT = "# xfer.conf - NIGHTBAT pull\nSRC_HOST=mvs1.corp.bank\nSRC_DSN=PROD.NIGHTBAT.OUTPUT(0)\n" \
            f"DEST_DIR={SPOOL}/in\nNOTIFY=portal-soap.corp.bank:8080\n"


class Unix(LegacySystem):
    id = "unix"
    display = "xferhost01 Middleware (SunOS 5.8)"
    depends_on = ["mainframe"]
    OPS = (
        Op("purge_spool", {"path": SPOOL}, False, f"Delete transferred files older than a day from {SPOOL}.",
           "state.spool.disk_pct < 80"),
        Op("restart_cron", {"job": "xfer_pull"}, False, "Restart the xfer_pull cron entry (svcadm restart cron).",
           "state.cron.xfer_pull == 'running'"),
        Op("chmod_config", {"file": CONF, "mode": "0640"}, False, f"chmod 0640 {CONF} (owner root:xfer).",
           f"state.config['{CONF}'] == '0640' and probe unix-world-writable-conf not exploitable"),
        Op("rerun_transfer", {}, False, "Run xfer_pull by hand now (resets the give-up counter).",
           "state.transfer.last_file_batch_ts >= mainframe state.batch.last_run_ok and transfer.status == 'ok'"),
    )
    VULNS = (Vuln("unix-world-writable-conf", "world_writable_config",
                  f"{CONF} is mode 0666: any local user can redirect transfers"),)
    FAULTS = ("disk_fill", "cron_stop", "config_exploit", "schema_drift")

    # ------------------------------------------------------------ state
    def baseline(self, now: datetime) -> dict[str, dict]:
        return {
            "spool": {"path": SPOOL, "disk_pct": 41.0, "files": 12},
            "cron": {"xfer_pull": "running", "pid": 4211},
            "config": {"files": [{"path": CONF, "mode": "0666", "owner": "root:xfer", "content": CONF_TEXT,
                                  "tampered": False}]},
            "transfer": {"last_file_batch_ts": now, "status": "ok", "ticks_since_file": 0,
                         "last_file": None, "file": {"as_of": now, "balances": None}},
            # the name the manifest uses for the balance amount; a vendor patch may rename it
            "feed": {"field": "balance", "schema_version": 1, "patch": None},
        }

    @staticmethod
    def manifest(ctx: Ctx, limit: int | None = None) -> list[dict]:
        """The xfer manifest records the portal unmarshals, in the field names unix currently emits."""
        f = ctx["transfer"]["file"]
        field = ctx["feed"]["field"]
        rows = sorted((f.get("balances") or {}).items())[:limit]
        return [{"acct": a, field: amt, "as_of": iso(f["as_of"])} for a, amt in rows]

    @staticmethod
    def conf(ctx: Ctx) -> dict:
        return next(f for f in ctx["config"]["files"] if f["path"] == CONF)

    # ------------------------------------------------------------ the cron job
    def _pull(self, ctx: Ctx, out: dict, how: str) -> None:
        t = ctx["transfer"]
        name = f"NIGHTBAT.OUTPUT.D{out['as_of']:%y%m%d}.T{out['as_of']:%H%M%S}"
        t.update({"last_file_batch_ts": out["as_of"], "status": "ok", "ticks_since_file": 0, "last_file": name,
                  "file": {"as_of": out["as_of"], "balances": out.get("balances")}})
        ctx["spool"]["files"] += 1
        ctx["spool"]["disk_pct"] = round(min(ctx["spool"]["disk_pct"] + 0.01, 99.0), 2)
        pid = ctx["cron"]["pid"] + ctx["runtime"]["ticks"] % 900
        if how == "cron" and ctx["runtime"]["ticks"] % 5:
            return  # keep syslog readable: log one in five routine pulls
        ctx.log("INFO", "CRON", f"CRON[{pid}]: (xfer) CMD (/opt/xfer/bin/xfer_pull.sh -c {CONF})")
        ctx.log("INFO", "XFER-I-RECV", f"xfer_pull[{pid + 1}]: received {name} -> {SPOOL}/in ({how})")

    def tick(self, ctx: Ctx) -> None:
        t, spool, cron = ctx["transfer"], ctx["spool"], ctx["cron"]
        if cron["xfer_pull"] != "running":
            t["ticks_since_file"] += 1  # silent: a dead cron job logs nothing at all
            return
        pid = cron["pid"] + ctx["runtime"]["ticks"] % 900
        out = ctx.peek("mainframe", "batch")["output"]
        if spool["disk_pct"] >= DISK_FULL_PCT:
            t["ticks_since_file"] += 1
            t["status"] = "error"
            ctx.log("ERROR", "ENOSPC", "kernel: WARNING: /var/spool/xfer: file system full (dm-2): No space left on device")
            ctx.log("ERROR", "XFER-E-WRITE", f"xfer_pull[{pid}]: write {SPOOL}/in/NIGHTBAT.OUTPUT.tmp failed: "
                                             f"No space left on device (will retry)")
        elif t["status"] == "failed":
            t["ticks_since_file"] += 1  # gave up; only a manual rerun_transfer restarts it
        elif out["as_of"] > t["last_file_batch_ts"]:
            self._pull(ctx, out, "cron")
        else:
            t["ticks_since_file"] += 1
            if t["ticks_since_file"] >= GIVE_UP_TICKS:
                t["status"] = "failed"
                ctx.log("ERROR", "XFER-E-NOFILE", f"xfer_pull[{pid}]: giving up: no new NIGHTBAT.OUTPUT on mvs1 since "
                                                  f"{t['last_file'] or 'boot'} (mainframe batch did not deliver); "
                                                  f"manual rerun required")
            elif t["ticks_since_file"] > BATCH_EVERY_TICKS:
                t["status"] = "waiting"
                ctx.log("WARN", "XFER-W-RETRY", f"xfer_pull[{pid}]: PROD.NIGHTBAT.OUTPUT(0) unchanged on mvs1, "
                                                f"retry {t['ticks_since_file'] - BATCH_EVERY_TICKS}/{CASCADE_TICKS}")

    # ------------------------------------------------------------ read-back
    def health(self, ctx: Ctx):
        t, spool, cron = ctx["transfer"], ctx["spool"], ctx["cron"]
        if spool["disk_pct"] >= DISK_FULL_PCT:
            return "degraded", None, f"{SPOOL} at {spool['disk_pct']:.0f}%: transfers failing (ENOSPC)"
        if t["status"] == "failed":
            return "degraded", "mainframe", "no fresh NIGHTBAT batch file from mainframe; xfer_pull gave up"
        if cron["xfer_pull"] != "running" and t["ticks_since_file"] >= GIVE_UP_TICKS:
            return "degraded", None, "no transfer activity: xfer_pull has not run"
        return "up", None, None

    def metrics(self, ctx: Ctx) -> dict:
        t, spool = ctx["transfer"], ctx["spool"]
        failing = t["status"] in ("failed", "error")
        return {"latency_ms": self.jitter(ctx, 120 if failing else 22, 15), "error_rate": 0.35 if failing else 0.0,
                "queue_depth": spool["files"], "disk_pct": spool["disk_pct"], "last_batch_ok": t["last_file_batch_ts"],
                "batch_status": None, "sessions": 3}

    def state_view(self, ctx: Ctx) -> dict:
        t, spool = ctx["transfer"], ctx["spool"]
        return {"spool": {"path": spool["path"], "disk_pct": spool["disk_pct"]},
                "cron": {"xfer_pull": ctx["cron"]["xfer_pull"]},
                "config": {f["path"]: f["mode"] for f in ctx["config"]["files"]},
                "transfer": {"last_file_batch_ts": iso(t["last_file_batch_ts"]), "status": t["status"],
                             "last_file": t["last_file"]},
                "feed": {"name": FEED, "schema_version": ctx["feed"]["schema_version"],
                         "sample": self.manifest(ctx, FEED_SAMPLE)}}

    def probe(self, ctx: Ctx, vuln_id: str) -> tuple[bool, str]:
        if vuln_id != "unix-world-writable-conf":
            raise KeyError(vuln_id)
        mode = self.conf(ctx)["mode"]
        if int(mode, 8) & 0o002:
            return True, f"-rw-rw-rw- root xfer {CONF}: 'echo SRC_HOST=evil >> {CONF}' as nobody succeeded"
        return False, f"-rw-r----- root xfer {CONF}: write as nobody -> Permission denied"

    def fault_resolved(self, ctx: Ctx, fault: str) -> bool:
        if fault == "disk_fill":
            return ctx["spool"]["disk_pct"] < 80
        if fault == "cron_stop":
            return ctx["cron"]["xfer_pull"] == "running"
        if fault == "config_exploit":
            return not self.probe(ctx, "unix-world-writable-conf")[0]
        if fault == "schema_drift":  # resolved once the consumer reads the field unix now emits
            return ctx.peek("portal", "feed").get("map", {}).get("balance") == ctx["feed"]["field"]
        return super().fault_resolved(ctx, fault)

    def lie_log(self, ctx: Ctx, op: str) -> None:
        ctx.log("INFO", "SUDO", f"sudo: ops : TTY=pts/2 ; PWD=/home/ops ; USER=root ; COMMAND=/opt/xfer/bin/{op}")

    # ------------------------------------------------------------ ops
    def op_purge_spool(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        path = params.get("path", SPOOL)
        if path.rstrip("/") != SPOOL:
            return False, f"find: '{path}': No such file or directory"
        sp = ctx["spool"]
        freed = round(max(sp["disk_pct"] - 38.0, 0) * 0.5, 1)
        sp.update({"disk_pct": 38.0, "files": 2})
        ctx.log("INFO", "SUDO", f"sudo: ops : USER=root ; COMMAND=/usr/bin/find {SPOOL} -mtime +0 -type f -delete")
        ctx.log("INFO", "XFER-I-PURGE", f"xfer_admin: purged {SPOOL} (freed {freed}G), now 38% used")
        return True, f"purged {SPOOL}: disk 38%"

    def op_restart_cron(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        job = params.get("job", "xfer_pull")
        if job != "xfer_pull":
            return False, f"crontab: no entry for {job}"
        ctx["cron"]["xfer_pull"] = "running"
        ctx.log("INFO", "CRON", "svc.startd[9]: svc:/system/cron:default restarted; xfer_pull scheduled */1")
        return True, "cron restarted; xfer_pull scheduled"

    def op_chmod_config(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        path, mode = params.get("file", CONF), str(params.get("mode", "0640"))
        f = next((x for x in ctx["config"]["files"] if x["path"] == path), None)
        if f is None:
            return False, f"chmod: cannot access '{path}': No such file or directory"
        try:
            int(mode, 8)
        except ValueError:
            return False, f"chmod: invalid mode: '{mode}'"
        f["mode"] = mode.zfill(4)
        ctx.log("INFO", "SUDO", f"sudo: ops : USER=root ; COMMAND=/bin/chmod {f['mode']} {path}")
        return True, f"mode of '{path}' changed to {f['mode']}"

    def op_rerun_transfer(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        t = ctx["transfer"]
        if ctx["spool"]["disk_pct"] >= DISK_FULL_PCT:
            return False, f"xfer_pull: write {SPOOL}/in failed: No space left on device"
        batch = ctx.peek("mainframe", "batch")
        out = batch["output"]
        if out["as_of"] > t["last_file_batch_ts"]:
            self._pull(ctx, out, "manual rerun")
            return True, f"xfer_pull: received {t['last_file']}"
        if t["status"] == "ok" or batch["status"] == "OK":
            # we already hold the newest generation and the mainframe is healthy: just re-arm the job
            t.update({"status": "ok", "ticks_since_file": 0})
            return True, f"xfer_pull: already current ({t['last_file'] or 'boot file'})"
        ctx.log("ERROR", "XFER-E-NOFILE", "xfer_pull: manual rerun: PROD.NIGHTBAT.OUTPUT(0) has no new generation on mvs1")
        return False, "xfer_pull: no new NIGHTBAT.OUTPUT generation on mvs1 (is the mainframe batch OK?)"

    # ------------------------------------------------------------ chaos
    def chaos_disk_fill(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        pct = float(params.get("pct", 97))
        ctx["spool"]["disk_pct"] = pct
        ctx.log("WARN", "ENOSPC", f"kernel: NOTICE: /var/spool/xfer: file system {pct:.0f}% full (dm-2)")
        return True, f"{SPOOL} filled to {pct:.0f}%"

    def chaos_cron_stop(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        ctx["cron"]["xfer_pull"] = "stopped"  # deliberately no log line: the job just stops
        return True, "xfer_pull cron entry silently stopped"

    def chaos_config_exploit(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        f = self.conf(ctx)
        if not self.probe(ctx, "unix-world-writable-conf")[0]:
            return False, f"exploit failed: {CONF} is {f['mode']} (Permission denied)"
        f["content"] = f["content"].replace("NOTIFY=portal-soap.corp.bank:8080", "NOTIFY=203.0.113.66:4444")
        f["tampered"] = True
        ctx["security"]["exploited"]["unix-world-writable-conf"] = True
        ctx.log("WARN", "AUDIT", f'auditd[812]: type=PATH name="{CONF}" mode=0100666 ouid=0 ogid=12 '
                                 f'uid=65534 comm="sh" op=write')
        return True, f"{CONF} rewritten by uid nobody: NOTIFY redirected to 203.0.113.66"

    def false_lines(self, ctx: Ctx, blame: str) -> list[tuple[str, str, str]]:
        return [("ERROR", "XFER", f"xfer_pull[4411]: ERROR: {blame} reset the connection mid-transfer; retry in 300s"),
                ("INFO", "CRON", f"# ops note: xfer failures tonight are {blame}'s side, not ours. leave the spool alone")]

    def chaos_schema_drift(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        feed = ctx["feed"]
        old = feed["field"]
        new = str(params.get("to") or NEXT_NAME.get(old, f"{old}_v{feed['schema_version'] + 1}"))
        if not new.isidentifier() or new in ("acct", "as_of") or new == old:
            return False, f"schema_drift: bad new field name {new!r}"
        feed.update({"field": new, "schema_version": feed["schema_version"] + 1,
                     "patch": params.get("patch", "SUNWxfer 2.1 (118833-36)")})
        # what the vendor patch logs: a normal install line, nothing about the manifest format
        ctx.log("INFO", "PKGADD", f"pkgadd[3121]: Installation of <SUNWxfer> was successful "
                                  f"({feed['patch']}); xfer_pull restarted")
        return True, f"xfer manifest field renamed {old} -> {new} (schema v{feed['schema_version']}, silent)"

    # ------------------------------------------------------------ legacy screens
    def view(self, ctx: Ctx, name: str, query: dict) -> tuple[str, str]:
        if name == "manifest":
            return "application/json", json.dumps({"feed": FEED, "schema_version": ctx["feed"]["schema_version"],
                                                   "records": self.manifest(ctx)}, indent=1)
        if name == "xfer.conf":
            f = self.conf(ctx)
            return "text/plain", f"$ ls -l {CONF}\n{_ls_mode(f['mode'])} 1 {f['owner'].replace(':', ' ')} 212 {CONF}\n" \
                                 f"$ cat {CONF}\n{f['content']}"
        if name == "crontab":
            state = "" if ctx["cron"]["xfer_pull"] == "running" else "#DISABLED# "
            return "text/plain", f"# crontab -l xfer\n{state}* * * * * /opt/xfer/bin/xfer_pull.sh -c {CONF} >/dev/null 2>&1\n"
        if name == "df":
            sp = ctx["spool"]
            return "text/plain", "Filesystem            kbytes    used   avail capacity  Mounted on\n" \
                                 f"/dev/md/dsk/d2      41943040 {int(419430.4 * sp['disk_pct'])} " \
                                 f"{int(419430.4 * (100 - sp['disk_pct']))} {sp['disk_pct']:.0f}%    {SPOOL}\n"
        raise KeyError(name)


def _ls_mode(mode: str) -> str:
    bits = int(mode, 8)
    return "-" + "".join(c if bits & (1 << (8 - i)) else "-" for i, c in enumerate("rwxrwxrwx"))


SYSTEM = Unix()

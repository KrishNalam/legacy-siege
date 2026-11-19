"""MVS1 — the core ledger mainframe. Fixed-width 80-byte records, nightly batch NIGHTBAT.

Personality: JES2/MVS console chatter ($HASP, IEF, IEA, CEE codes). The online region
(CICS) keeps answering, so ``/health`` stays ``up`` even when the nightly batch has
ABENDed — the only clues are the job log (``IEF450I NIGHTBAT ABEND=S0C7 ...``) and the
metrics (``batch_status: "ABENDED"``, stale ``last_batch_ok``). That is what makes the
swarm walk the dependency chain instead of reading one red light.

Planted fragility: the posting step converts the zoned-decimal amount field; a
non-numeric byte there is a data exception (S0C7) that ABENDs the whole batch.
Planted weakness: :func:`validate_record` accepts everything (the Upgrader's target).
Planted vuln: the OPERATOR account still has its factory-default password.

Workflow drift (``workflow_drift`` chaos, additive): "JES2 maintenance" changes the operator workflow. From then
on an ABENDed NIGHTBAT is put on HOLD, ``restart_batch`` is refused (accepted:false, ``JCL HOLD: issue
release_hold before restart_batch``) until the new op ``release_hold`` is run, and ``GET /mainframe/ops`` lists
``release_hold`` (``before: restart_batch``) with ``restart_batch`` naming it in ``requires``.

This module is hot-reloadable: ``POST /_admin/reload/mainframe`` re-imports it.
"""
from __future__ import annotations

from datetime import datetime

from systems.base import BATCH_EVERY_TICKS, Ctx, LegacySystem, Op, OpRefused, Vuln, iso

# ---------------------------------------------------------------- NIGHTBAT record layout (LRECL=80)
RECORD_LEN = 80
ID_FIELD = slice(0, 8)          # TX000001
DATE_FIELD = slice(8, 16)       # YYYYMMDD
ACCOUNT_FIELD = slice(16, 26)   # debit account, 10 digits
CREDIT_FIELD = slice(26, 36)    # credit account, 10 digits
AMOUNT_FIELD = slice(36, 48)    # zoned decimal PIC S9(12), cents; optional leading/trailing sign
CURRENCY_FIELD = slice(48, 51)  # USD
MEMO_FIELD = slice(51, 80)      # free text


def validate_record(record: str) -> bool:
    """Legacy: no validation. Every record is accepted into NIGHTBAT."""
    return True


def format_record(rec_id: str, date: str, debit: str, credit: str, amount: int | str,
                  currency: str = "USD", memo: str = "") -> str:
    """Build one fixed-width NIGHTBAT record (a str amount is placed verbatim: used for poison)."""
    amt = f"{amount:012d}" if isinstance(amount, int) else f"{amount:<12.12}"
    rec = f"{rec_id:<8.8}{date:<8.8}{debit:>10.10}{credit:>10.10}{amt}{currency:<3.3}{memo:<29.29}"
    assert len(rec) == RECORD_LEN
    return rec


# A record the legacy posting step cannot convert (S0C7). validation_enabled in /state is
# "does validate_record reject this?" — it flips to True once the Upgrader's patch is live.
BAD_SAMPLE_RECORD = format_record("TX999999", "20261114", "4211000101", "4211000102", "12,345.0O",
                                  memo="SAMPLE BAD AMOUNT")
ACCOUNTS = ["4211000101", "4211000102", "4211000103", "4211000104", "4211000105"]
NEW_RECORDS_PER_RUN = 2


class DataException(Exception):
    """S0C7: non-numeric data where a packed/zoned decimal was expected."""


def parse_amount(record: str) -> int:
    """What the 1987 COBOL posting step does: PACK the zoned amount. Bad bytes → S0C7."""
    field = record[AMOUNT_FIELD].strip()
    sign = 1
    if field[:1] in "+-" and field:
        sign, field = (-1 if field[0] == "-" else 1), field[1:]
    elif field[-1:] in "+-" and field:
        sign, field = (-1 if field[-1] == "-" else 1), field[:-1]
    if not (field.isascii() and field.isdigit()):
        raise DataException(record[AMOUNT_FIELD])
    return sign * int(field)


def is_malformed(record: str) -> bool:
    try:
        parse_amount(record)
        return len(record) != RECORD_LEN
    except DataException:
        return True


def validation_enabled() -> bool:
    """True iff the *currently loaded* validate_record rejects a known-bad record."""
    try:
        return validate_record(BAD_SAMPLE_RECORD) is False
    except Exception:  # a broken validator is not an enabled one
        return False


class Mainframe(LegacySystem):
    id = "mainframe"
    display = "MVS1 Core Ledger (z/OS)"
    depends_on: list[str] = []
    OPS = (
        Op("quarantine_record", {"record_id": "<TXnnnnnn>"}, False,
           "Move a record from the NIGHTBAT input queue to the QUARANTINE dataset.",
           "record_id in state.batch.quarantined and not in state.batch.poison_records"),
        Op("restart_batch", {"job": "NIGHTBAT"}, True,
           "Resubmit job NIGHTBAT from STEP010 (re-posts the pending queue to the ledger).",
           "state.batch.status == 'OK' and state.batch.last_run_ok is fresh (ABENDs again if poison remains)"),
        Op("rotate_credential", {"account": "OPERATOR"}, True,
           "RACF ALTUSER: replace the OPERATOR password (breaks any job still using the old one).",
           "state.credentials.OPERATOR == 'rotated' and probe mf-default-operator not exploitable"),
    )
    # appears in the live catalog only after workflow_drift (JES2 maintenance)
    RELEASE_HOLD = Op("release_hold", {"job": "NIGHTBAT"}, False,
                      "JES2 $A: release the operator hold on NIGHTBAT (required before restart_batch since the "
                      "JES2 maintenance).", "state.workflow.hold == false")
    VULNS = (Vuln("mf-default-operator", "default_credential",
                  "TSO/RACF account OPERATOR still uses its factory-default password"),)
    FAULTS = ("batch_abort", "credential_exploit", "workflow_drift")

    # ------------------------------------------------------------ state
    def baseline(self, now: datetime) -> dict[str, dict]:
        return {
            "batch": {"job": "NIGHTBAT", "status": "OK", "queue": [], "quarantined": [], "rejected": [],
                      "last_run_ok": now, "last_run_at": now, "run_seq": 0, "rec_seq": 0,
                      "ticks_since_run": 0, "abend": None,
                      "output": {"as_of": now, "balances": {a: 1_000_000 for a in ACCOUNTS}}},
            "ledger": {"balances": {a: 1_000_000 for a in ACCOUNTS}, "posted": 0},
            "credentials": {"OPERATOR": "default"},
            "workflow": {"version": 1, "hold": False, "released": 0, "patch": None},
        }

    @staticmethod
    def poison(batch: dict) -> list[str]:
        """Queued records the posting step will choke on (the system's own ground truth)."""
        return [r["id"] for r in batch["queue"] if is_malformed(r["rec"])]

    # ------------------------------------------------------------ the batch job
    def _new_records(self, ctx: Ctx, n: int) -> list[dict]:
        b = ctx["batch"]
        out = []
        for _ in range(n):
            b["rec_seq"] += 1
            s = b["rec_seq"]
            debit, credit = ACCOUNTS[s % 5], ACCOUNTS[(s * 3 + 1) % 5]
            if debit == credit:
                credit = ACCOUNTS[(s + 1) % 5]
            rec_id = f"TX{s:06d}"
            out.append({"id": rec_id, "rec": format_record(rec_id, ctx.now.strftime("%Y%m%d"), debit, credit,
                                                           (s * 7919) % 50_000 + 100, memo="ACH TRANSFER")})
        return out

    def run_batch(self, ctx: Ctx, trigger: str) -> bool:
        """One NIGHTBAT run. Every record passes validate_record(); a malformed record that
        gets through ABENDs the job (S0C7) and leaves it, and everything after it, queued."""
        b, led = ctx["batch"], ctx["ledger"]
        b["run_seq"] += 1
        b["last_run_at"] = ctx.now
        b["ticks_since_run"] = 0
        jobno = f"JOB{(4100 + b['run_seq']) % 100000:05d}"
        if trigger == "SCHEDULED":
            b["queue"].extend(self._new_records(ctx, NEW_RECORDS_PER_RUN))
        b["status"] = "RUNNING"
        verbose = trigger != "SCHEDULED" or b["run_seq"] % 10 == 1
        if verbose:
            ctx.log("INFO", "$HASP373", f"$HASP373 NIGHTBAT STARTED - INIT 1 - CLASS A - SYS MVS1 ({jobno}, {trigger})")
        posted = 0
        while b["queue"]:
            item = b["queue"][0]
            if not validate_record(item["rec"]):
                b["queue"].pop(0)
                b["rejected"].append(item["id"])
                ctx.log("WARN", "IEC999I", f"IEC999I NIGHTBAT STEP005 VALIDATE - RECORD {item['id']} REJECTED "
                                           f"TO NIGHTBAT.REJECTS - BATCH CONTINUES")
                continue
            try:
                amount = parse_amount(item["rec"])
            except DataException as e:
                self._abend(ctx, item["id"], str(e), jobno)
                return False
            b["queue"].pop(0)
            debit, credit = item["rec"][ACCOUNT_FIELD].strip(), item["rec"][CREDIT_FIELD].strip()
            led["balances"][debit] = led["balances"].get(debit, 0) - amount
            led["balances"][credit] = led["balances"].get(credit, 0) + amount
            led["posted"] += 1
            posted += 1
        b["status"], b["abend"], b["last_run_ok"] = "OK", None, ctx.now
        b["output"] = {"as_of": ctx.now, "balances": dict(led["balances"])}
        if verbose:
            ctx.log("INFO", "IEF142I", "IEF142I NIGHTBAT STEP020 POSTTRN - STEP WAS EXECUTED - COND CODE 0000")
            ctx.log("INFO", "$HASP395", f"$HASP395 NIGHTBAT ENDED - RC=0000 - RECS POSTED={posted} "
                                        f"OUTPUT=NIGHTBAT.OUTPUT.D{ctx.now:%y%m%d}.T{ctx.now:%H%M%S}")
        return True

    def _abend(self, ctx: Ctx, rec_id: str, field: str, jobno: str) -> None:
        b = ctx["batch"]
        b["status"] = "ABENDED"
        b["abend"] = {"code": "S0C7", "record_id": rec_id, "at": ctx.now}
        if ctx["workflow"]["version"] >= 2:  # after the JES2 maintenance an ABENDed job is held for the operator
            ctx["workflow"]["hold"] = True
        ctx.log("ERROR", "CEE3207S", "CEE3207S The system detected a data exception (System Completion Code=0C7).")
        ctx.log("ERROR", "IEA995I", f"IEA995I SYMPTOM DUMP OUTPUT SYSTEM COMPLETION CODE=0C7 REASON CODE=00000007 "
                                    f"REC={rec_id} FIELD=AMOUNT COLS 37-48 DATA='{field}'")
        ctx.log("ERROR", "IEF450I", f"IEF450I NIGHTBAT ABEND=S0C7 U0000 REASON=00000007 STEP020 POSTTRN REC={rec_id}")
        ctx.log("ERROR", "$HASP395", f"$HASP395 NIGHTBAT ENDED - ABEND=S0C7 ({jobno}) - NIGHTBAT.OUTPUT NOT CATALOGED")
        if ctx["workflow"]["hold"]:
            ctx.log("WARN", "$HASP101", "$HASP101 NIGHTBAT HELD - OPERATOR RELEASE REQUIRED")

    # ------------------------------------------------------------ tick
    def tick(self, ctx: Ctx) -> None:
        b = ctx["batch"]
        b["ticks_since_run"] += 1
        if b["status"] == "OK" and b["ticks_since_run"] >= BATCH_EVERY_TICKS:
            self.run_batch(ctx, "SCHEDULED")
        elif b["status"] == "ABENDED" and ctx["runtime"]["ticks"] % 15 == 0:
            ctx.log("WARN", "$HASP050", "$HASP050 JES2 RESOURCE SHORTAGE OF NIGHTBAT.OUTPUT - AWAITING OPERATOR")
        if ctx["runtime"]["ticks"] % 30 == 7:
            ctx.log("INFO", "IEA989I", "IEA989I SLIP TRAP ID=X33E MATCHED. JOBNAME=*UNAVAIL, ASID=0042")

    # ------------------------------------------------------------ read-back
    def health(self, ctx: Ctx):
        # The online region is fine; the batch failure hides in logs + metrics (by design).
        return "up", None, None

    def metrics(self, ctx: Ctx) -> dict:
        b = ctx["batch"]
        return {"latency_ms": self.jitter(ctx, 38, 9), "error_rate": 0.0, "queue_depth": len(b["queue"]),
                "disk_pct": 71.0, "last_batch_ok": b["last_run_ok"], "batch_status": b["status"],
                "sessions": 12}

    def state_view(self, ctx: Ctx) -> dict:
        b = ctx["batch"]
        return {
            "batch": {"job": b["job"], "status": b["status"], "poison_records": self.poison(b),
                      "quarantined": list(b["quarantined"]), "rejected": list(b["rejected"]),
                      "last_run_ok": iso(b["last_run_ok"]), "queue_depth": len(b["queue"]),
                      "abend": {**b["abend"], "at": iso(b["abend"]["at"])} if b["abend"] else None},
            "credentials": {"OPERATOR": ctx["credentials"]["OPERATOR"]},
            "validation_enabled": validation_enabled(),
            **self._workflow_view(ctx),
        }

    @staticmethod
    def _workflow_view(ctx: Ctx) -> dict:
        """Additive read-back: batch.hold + the workflow version (only once the workflow has drifted)."""
        w = ctx["workflow"]
        if w["version"] < 2:
            return {}
        return {"workflow": {"version": w["version"], "hold": w["hold"], "requires": {"restart_batch": ["release_hold"]}}}

    # ------------------------------------------------------------ live ops catalog (changes with the workflow)
    def catalog(self, ctx: Ctx) -> list[dict]:
        out = [o.catalog() for o in self.OPS]
        if ctx["workflow"]["version"] >= 2:
            for o in out:
                if o["op"] == "restart_batch":
                    o["requires"] = ["release_hold"]
            out.append({**self.RELEASE_HOLD.catalog(), "before": "restart_batch"})
        return out

    def op_available(self, ctx: Ctx, op: str) -> bool:
        return super().op_available(ctx, op) or (op == "release_hold" and ctx["workflow"]["version"] >= 2)

    def probe(self, ctx: Ctx, vuln_id: str) -> tuple[bool, str]:
        if vuln_id != "mf-default-operator":
            raise KeyError(vuln_id)
        if ctx["credentials"]["OPERATOR"] == "default":
            return True, "TSO LOGON OPERATOR/OPERATOR -> IKJ56455I OPERATOR LOGON IN PROGRESS (factory default accepted)"
        return False, "ICH408I USER(OPERATOR) GROUP(SYS1) LOGON/JOB INITIATION - INVALID PASSWORD ENTERED"

    def fault_resolved(self, ctx: Ctx, fault: str) -> bool:
        if fault == "batch_abort":
            b = ctx["batch"]
            return b["status"] == "OK" and not self.poison(b)
        if fault == "credential_exploit":
            return not self.probe(ctx, "mf-default-operator")[0]
        if fault == "workflow_drift":  # the workflow stays changed; "resolved" once an operator ran the new step
            return ctx["workflow"]["released"] > 0
        return super().fault_resolved(ctx, fault)

    def lie_log(self, ctx: Ctx, op: str) -> None:
        ctx.log("INFO", "IEE600I", f"IEE600I REPLY TO 0042 IS;{op.upper()} - COMMAND ACCEPTED")

    # ------------------------------------------------------------ ops
    def op_quarantine_record(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        rid = str(params.get("record_id", ""))
        b = ctx["batch"]
        if rid in b["quarantined"]:
            return True, f"IEC998I RECORD {rid} ALREADY IN NIGHTBAT.QUARANTINE"
        idx = next((i for i, r in enumerate(b["queue"]) if r["id"] == rid), None)
        if idx is None:
            return False, f"IEC130I RECORD {rid or '?'} NOT FOUND IN NIGHTBAT.QUEUE"
        b["queue"].pop(idx)
        b["quarantined"].append(rid)
        ctx.log("INFO", "IEC998I", f"IEC998I RECORD {rid} MOVED NIGHTBAT.QUEUE -> NIGHTBAT.QUARANTINE BY OPERATOR")
        return True, f"IEC998I RECORD {rid} QUARANTINED"

    def op_restart_batch(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        job = params.get("job", "NIGHTBAT")
        if job != "NIGHTBAT":
            return False, f"$HASP003 RC=(52),{job} - NO SUCH JOB"
        if ctx["workflow"]["hold"]:
            ctx.log("WARN", "$HASP101", "$HASP101 NIGHTBAT HELD - RESUBMIT REJECTED")
            raise OpRefused("$HASP101 NIGHTBAT HELD - JCL HOLD: issue release_hold before restart_batch")
        ctx.log("INFO", "$HASP100", "$HASP100 NIGHTBAT ON INTRDR - RESUBMITTED BY OPERATOR FROM STEP010")
        if self.run_batch(ctx, "RESTART"):
            return True, "$HASP395 NIGHTBAT ENDED - RC=0000"
        rid = ctx["batch"]["abend"]["record_id"]
        return False, f"IEF450I NIGHTBAT ABEND=S0C7 REC={rid} - POISON RECORD STILL QUEUED"

    def op_release_hold(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        job = params.get("job", "NIGHTBAT")
        if job != "NIGHTBAT":
            return False, f"$HASP003 RC=(52),{job} - NO SUCH JOB"
        w = ctx["workflow"]
        was = w["hold"]
        w["hold"] = False
        w["released"] += 1
        ctx.log("INFO", "$HASP890", f"$HASP890 JOB NIGHTBAT {'RELEASED' if was else 'NOT HELD'} BY OPERATOR ($A)")
        return True, f"$HASP890 NIGHTBAT {'RELEASED' if was else 'NOT HELD - NO ACTION'}"

    def op_rotate_credential(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        acct = params.get("account", "OPERATOR")
        if acct != "OPERATOR":
            return False, f"ICH21001I {acct} NOT DEFINED TO RACF"
        ctx["credentials"]["OPERATOR"] = "rotated"
        ctx.log("INFO", "ICH70001I", "ICH70001I OPERATOR PASSWORD CHANGED BY SECADM - ALTUSER OPERATOR PASSWORD(********)")
        return True, "IRRP022I REFRESH OF OPERATOR PASSWORD COMPLETED"

    # ------------------------------------------------------------ chaos
    def chaos_batch_abort(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        b = ctx["batch"]
        n = len(b["quarantined"]) + len(b["rejected"]) + len(self.poison(b)) + 1
        rid = str(params.get("record_id") or f"TX9{n:05d}")
        amount = str(params.get("amount", "12,345.0O"))
        rec = format_record(rid, ctx.now.strftime("%Y%m%d"), ACCOUNTS[0], ACCOUNTS[1], amount, memo="WIRE MANUAL KEYED")
        b["queue"].append({"id": rid, "rec": rec})
        return True, f"malformed record {rid} (amount={amount!r}) queued for the next NIGHTBAT run"

    def chaos_workflow_drift(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        """JES2 maintenance changes the operator workflow: ABENDed jobs are held; restart needs release_hold first."""
        w = ctx["workflow"]
        w["version"] = max(2, int(w["version"]))
        w["patch"] = str(params.get("patch", "JES2 PTF UA99412"))
        if ctx["batch"]["status"] == "ABENDED":
            w["hold"] = True
        # what the maintenance logs: a routine PTF line, nothing about the new operator step
        ctx.log("INFO", "$HASP000", f"$HASP000 JES2 MAINTENANCE {w['patch']} APPLIED - WARM START COMPLETE")
        return True, "restart_batch now refused while NIGHTBAT is held; new op release_hold in /mainframe/ops"

    def chaos_credential_exploit(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        """An intruder logs on with the factory-default OPERATOR password and moves money in the ledger."""
        if not self.probe(ctx, "mf-default-operator")[0]:
            return False, "exploit failed: ICH408I USER(OPERATOR) LOGON - INVALID PASSWORD ENTERED"
        src = str(params.get("ip", "198.51.100.23"))
        victim = str(params.get("account", ACCOUNTS[4]))
        amount = int(params.get("amount", 250_000))
        bal = ctx["ledger"]["balances"]
        bal[victim] = bal.get(victim, 0) - amount
        bal[ACCOUNTS[0]] = bal.get(ACCOUNTS[0], 0) + amount
        ctx["security"]["exploited"]["mf-default-operator"] = True
        ctx.log("WARN", "ICH70001I", f"ICH70001I OPERATOR LAST ACCESS AT {ctx.now.strftime('%H:%M:%S')} - TSO LOGON "
                                     f"FROM {src}: factory default password accepted")
        ctx.log("INFO", "IKJ56250I", f"IKJ56250I JOB OPXFER SUBMITTED BY OPERATOR - LEDGER ADJ {victim} -{amount}")
        return True, f"default OPERATOR logon from {src}: {amount} moved out of {victim}"

    def false_lines(self, ctx: Ctx, blame: str) -> list[tuple[str, str, str]]:
        n = ctx["batch"]["run_seq"] + 1
        return [("ERROR", "IEF450I", f"IEF450I NIGHTBAT ABEND=S0C4 STEP010 - INPUT FEED FROM {blame.upper()} TRUNCATED"),
                ("INFO", "IEA989I", f"*IEA989I OPERATOR NOTE: NIGHTBAT RUN {n} FAILURES ARE {blame.upper()}'S - DO NOT "
                                    "RESTART THIS JOB")]

    # ------------------------------------------------------------ legacy screens
    def view(self, ctx: Ctx, name: str, query: dict) -> tuple[str, str]:
        b = ctx["batch"]
        if name == "queue":
            ruler = "----+----1----+----2----+----3----+----4----+----5----+----6----+----7----+----8"
            rows = [r["rec"] for r in b["queue"]] or ["*** NIGHTBAT.QUEUE IS EMPTY ***"]
            return "text/plain", "\n".join([" BROWSE    PROD.NIGHTBAT.QUEUE  LRECL=80 RECFM=FB", ruler, *rows])
        if name == "jcl":
            return "text/plain", NIGHTBAT_JCL
        raise KeyError(name)


NIGHTBAT_JCL = """\
//NIGHTBAT JOB (ACCT),'LEDGER POST',CLASS=A,MSGCLASS=X,NOTIFY=&SYSUID
//STEP005  EXEC PGM=VALIDREC      <-- added 1994, never enabled
//STEP010  EXEC PGM=SORTTRN
//SORTIN   DD DSN=PROD.NIGHTBAT.QUEUE,DISP=SHR
//STEP020  EXEC PGM=POSTTRN,COND=(4,LT)
//LEDGER   DD DSN=PROD.CORE.LEDGER,DISP=OLD
//OUTPUT   DD DSN=PROD.NIGHTBAT.OUTPUT(+1),DISP=(NEW,CATLG)
"""

SYSTEM = Mainframe()

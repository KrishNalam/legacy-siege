"""NetBank Online — the customer-facing legacy web portal (Tomcat 5.5 + SOAP/XML).

Personality: Tomcat/log4j lines (``INFO [http-8080-exec-4] ...``, ``SEVERE ... SOAP Fault``),
a session store that leaks, and the classic legacy sin: **HTTP 200 with an error page** —
when its balance feed goes stale it keeps serving yesterday's balances with a cheerful 200.

Data flow: the balance cache refreshes from the unix transfer every tick while unix is up.
If unix stays unhealthy for CASCADE_TICKS ticks the cache *freezes* (``cache.stale``) and
the portal turns ``degraded`` blaming ``unix``; only ``refresh_cache`` thaws it, and only
once the unix transfer is fresh again.

Its XferClient unmarshals the unix manifest by field name (``feed.map``: logical field -> manifest
field, ``{"balance": "balance"}`` out of the box). If unix starts emitting a different name (schema
drift) every record fails to unmarshal; after CASCADE_TICKS the cache freezes exactly like a stale
feed. ``map_field`` re-points the mapping; ``refresh_cache`` then thaws the cache.

Planted vuln: the account search concatenates user input into SQL.
"""
from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

from systems.base import CASCADE_TICKS, Ctx, LegacySystem, Op, Vuln, iso

SESSION_MAX = 500
SESSION_BASE = 40
LEAK_PER_TICK = 40
CUSTOMERS = ["ALVAREZ M", "BRENNAN K", "CHOWDHURY S", "DUBOIS A", "EZE O"]


class Portal(LegacySystem):
    id = "portal"
    display = "NetBank Online Portal (Tomcat 5.5 / SOAP)"
    depends_on = ["unix"]
    OPS = (
        Op("flush_sessions", {}, False, "Invalidate idle HTTP sessions (StandardManager.expireAll).",
           f"state.sessions < {int(SESSION_MAX * 0.8)}"),
        Op("apply_patch", {"patch": "parameterize_search"}, False,
           "Hot-deploy patch: account search uses a PreparedStatement.",
           "state.search_patch == 'parameterize_search' and probe portal-sqli-search not exploitable"),
        Op("map_field", {"field": "balance", "source": "acct_bal"}, False,
           "Re-point the XferClient field mapping (xfer-mapping.properties, hot reload).",
           "state.feed.map[field] == source and the unix manifest's source values line up with the last good sample"),
        Op("refresh_cache", {}, False, "Reload the balance cache from the xfer gateway (fails while the feed is stale).",
           "state.cache.stale == false (only possible once unix transfer is fresh)"),
    )
    VULNS = (Vuln("portal-sqli-search", "sql_injection", "Account search builds SQL by string concatenation",
                  aliases=("sqli_search",)),)
    FAULTS = ("session_leak", "sqli_exploit")

    # ------------------------------------------------------------ state
    def baseline(self, now: datetime) -> dict[str, dict]:
        return {
            "sessions": {"active": SESSION_BASE, "leak": False},
            "search": {"patch": "none"},
            "cache": {"balances_as_of": now, "stale": False, "unhealthy_ticks": 0, "balances": None,
                      "tampered": False},
            "feed": {"map": {"balance": "balance"}, "errors": 0},
        }

    @staticmethod
    def feed_ok(ctx: Ctx) -> tuple[bool, str, str]:
        """(can unmarshal, field we read, field unix emits)."""
        want = ctx["feed"]["map"].get("balance", "balance")
        have = ctx.peek("unix", "feed").get("field", "balance")
        return want == have, want, have

    # ------------------------------------------------------------ tick
    def tick(self, ctx: Ctx) -> None:
        ticks = ctx["runtime"]["ticks"]
        s = ctx["sessions"]
        if s["leak"]:
            s["active"] += LEAK_PER_TICK
            if s["active"] >= SESSION_MAX * 0.8 and ticks % 2 == 0:
                ctx.log("WARN", "SESS-W-FULL", f"WARN  [ContainerBackgroundProcessor] o.a.c.session.StandardManager - "
                                               f"session store {min(100, s['active'] * 100 // SESSION_MAX)}% full "
                                               f"({s['active']}/{SESSION_MAX} active)")
            if s["active"] >= SESSION_MAX and ticks % 3 == 0:
                ctx.log("ERROR", "SESS-E-OOM", "SEVERE [http-8080-exec-11] o.a.c.c.StandardWrapperValve - "
                                               "java.lang.OutOfMemoryError: Java heap space (StandardSession)")
        else:
            s["active"] = SESSION_BASE + (ticks * 13) % 9

        cache = ctx["cache"]
        if not cache["stale"]:
            if ctx.peek_status("unix") == "up":
                cache["unhealthy_ticks"] = 0
                f = ctx.peek("unix", "transfer")["file"]
                if f["as_of"] > cache["balances_as_of"]:
                    ok, want, have = self.feed_ok(ctx)
                    feed = ctx["feed"]
                    if ok:
                        feed["errors"] = 0
                        cache.update({"balances_as_of": f["as_of"], "balances": f.get("balances"), "tampered": False})
                    else:
                        feed["errors"] += 1
                        ctx.log("ERROR", "SOAP-E-PARSE", f"SEVERE [http-8080-exec-3] c.bank.soap.XferClient - unmarshal "
                                                         f"failed: element <{want}> missing in xfer manifest record "
                                                         f"(found: acct, {have}, as_of), retry {feed['errors']}/{CASCADE_TICKS}")
                        if feed["errors"] >= CASCADE_TICKS:
                            cache["stale"] = True
                            ctx.log("ERROR", "SOAP-FAULT", "SEVERE [http-8080-exec-3] c.bank.soap.XferClient - SOAP Fault "
                                                           "faultcode=Client.Unmarshal faultstring=\"xfer manifest "
                                                           "unreadable\"; balance cache frozen")
            else:
                cache["unhealthy_ticks"] += 1
                ctx.log("WARN", "SOAP-W-SLOW", f"WARN  [http-8080-exec-3] c.bank.soap.XferClient - xfer manifest not "
                                               f"updated, retry {cache['unhealthy_ticks']}/{CASCADE_TICKS}")
                if cache["unhealthy_ticks"] >= CASCADE_TICKS:
                    cache["stale"] = True
                    ctx.log("ERROR", "SOAP-FAULT", "SEVERE [http-8080-exec-3] c.bank.soap.XferClient - SOAP Fault "
                                                   "faultcode=Server.Unavailable faultstring=\"xfer gateway (xferhost01) "
                                                   "manifest stale\"; balance cache frozen")
        if ticks % 3 == 0:
            ms = int(self.jitter(ctx, 180, 60))
            ctx.log("INFO", "HTTP-200", f"INFO  [http-8080-exec-{ticks % 12 + 1}] access GET /netbank/balance.do "
                                        f"200 {ms}ms")
            if cache["stale"]:
                ctx.log("WARN", "CACHE-STALE", f"WARN  [http-8080-exec-{ticks % 12 + 1}] c.bank.web.BalanceAction - "
                                               f"serving cached balances as_of={iso(cache['balances_as_of'])} (HTTP 200)")

    # ------------------------------------------------------------ read-back
    def health(self, ctx: Ctx):
        s, cache = ctx["sessions"], ctx["cache"]
        if s["active"] >= SESSION_MAX:
            return "degraded", None, f"session store exhausted ({s['active']}/{SESSION_MAX}); logins failing"
        if cache["stale"]:
            if ctx["feed"]["errors"]:
                return "degraded", "unix", (f"balance feed unreadable: field '{ctx['feed']['map'].get('balance')}' missing "
                                            f"from xfer manifest; stale balances as of {iso(cache['balances_as_of'])} (HTTP 200)")
            return "degraded", "unix", f"customers see stale balances as of {iso(cache['balances_as_of'])} (HTTP 200)"
        return "up", None, None

    def metrics(self, ctx: Ctx) -> dict:
        s, cache = ctx["sessions"], ctx["cache"]
        lat = self.jitter(ctx, 180, 60) + max(0, s["active"] - SESSION_BASE) * 2.5
        err = 0.25 if ctx["feed"]["errors"] else (0.02 if s["active"] >= SESSION_MAX else 0.0)
        return {"latency_ms": round(lat, 1), "error_rate": err,
                "queue_depth": max(0, s["active"] - SESSION_MAX) // 10, "disk_pct": 55.0,
                "last_batch_ok": cache["balances_as_of"], "batch_status": None, "sessions": s["active"]}

    def state_view(self, ctx: Ctx) -> dict:
        cache = ctx["cache"]
        return {"sessions": ctx["sessions"]["active"], "search_patch": ctx["search"]["patch"],
                "cache": {"balances_as_of": iso(cache["balances_as_of"]), "stale": cache["stale"]},
                "feed": {"map": dict(ctx["feed"]["map"]), "errors": ctx["feed"]["errors"]}}

    def probe(self, ctx: Ctx, vuln_id: str) -> tuple[bool, str]:
        if vuln_id != "portal-sqli-search":
            raise KeyError(vuln_id)
        if ctx["search"]["patch"] == "none":
            return True, f"GET /netbank/search.do?q=%27%20OR%20%271%27%3D%271 -> 200, {len(CUSTOMERS)} rows (expected 0)"
        return False, "GET /netbank/search.do?q=%27%20OR%20%271%27%3D%271 -> 200, 0 rows (parameterized)"

    def fault_resolved(self, ctx: Ctx, fault: str) -> bool:
        if fault == "session_leak":
            return not ctx["sessions"]["leak"]
        if fault == "sqli_exploit":
            return ctx["search"]["patch"] != "none"
        return super().fault_resolved(ctx, fault)

    def lie_log(self, ctx: Ctx, op: str) -> None:
        ctx.log("INFO", "MANAGER", f"INFO  [http-8080-exec-2] o.a.c.manager.ManagerServlet - {op}: OK")

    # ------------------------------------------------------------ ops
    def op_flush_sessions(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        before = ctx["sessions"]["active"]
        ctx["sessions"].update({"active": SESSION_BASE, "leak": False})
        ctx.log("INFO", "MANAGER", f"INFO  [http-8080-exec-2] o.a.c.manager.ManagerServlet - expire: "
                                   f"{before - SESSION_BASE} sessions invalidated")
        return True, f"OK - {before - SESSION_BASE} sessions expired"

    def op_apply_patch(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        patch = params.get("patch", "parameterize_search")
        if patch != "parameterize_search":
            return False, f"FAIL - No such patch: {patch}"
        ctx["search"]["patch"] = patch
        ctx.log("INFO", "MANAGER", "INFO  [http-8080-exec-2] o.a.c.startup.HostConfig - Deploying patch "
                                   "netbank-search-ps.war: SearchDAO now uses PreparedStatement")
        return True, "OK - Deployed application at context path /netbank"

    def op_map_field(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        field, source = str(params.get("field", "balance")), str(params.get("source") or "")
        if field != "balance":
            return False, f"FAIL - XferClient has no logical field {field!r}"
        if not source.isidentifier() or source in ("acct", "as_of"):
            return False, f"FAIL - invalid manifest field name {source!r}"
        before = ctx["feed"]["map"].get(field)
        ctx["feed"]["map"][field] = source
        ctx["feed"]["errors"] = 0
        ctx.log("INFO", "MANAGER", f"INFO  [http-8080-exec-2] c.bank.soap.XferClient - xfer-mapping.properties "
                                   f"reloaded: {field} <- {source} (was {before})")
        return True, f"OK - {field} now read from manifest field {source} (was {before})"

    def op_refresh_cache(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        t = ctx.peek("unix", "transfer")
        if ctx.peek_status("unix") != "up" or t["status"] != "ok":
            ctx.log("ERROR", "SOAP-FAULT", "SEVERE [http-8080-exec-5] c.bank.soap.XferClient - refresh failed: "
                                           "SOAP Fault Server.Unavailable (xfer manifest stale)")
            return False, "SOAP Fault: Server.Unavailable - xfer gateway manifest is stale"
        ok, want, have = self.feed_ok(ctx)
        if not ok:
            ctx.log("ERROR", "SOAP-E-PARSE", f"SEVERE [http-8080-exec-5] c.bank.soap.XferClient - refresh failed: "
                                             f"element <{want}> missing in xfer manifest record (found: acct, {have}, as_of)")
            return False, f"unmarshal failed: manifest has no <{want}> field (it now sends {have})"
        cache = ctx["cache"]
        cache.update({"balances_as_of": t["file"]["as_of"], "balances": t["file"].get("balances"), "stale": False,
                      "unhealthy_ticks": 0, "tampered": False})
        ctx.log("INFO", "CACHE-OK", f"INFO  [http-8080-exec-5] c.bank.web.BalanceCache - reloaded balances "
                                    f"as_of={iso(cache['balances_as_of'])}")
        return True, f"balance cache reloaded (as_of {iso(cache['balances_as_of'])})"

    # ------------------------------------------------------------ chaos
    def chaos_session_leak(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        ctx["sessions"]["leak"] = True
        return True, f"session store leaking ~{LEAK_PER_TICK} sessions/s"

    def chaos_sqli_exploit(self, ctx: Ctx, params: dict) -> tuple[bool, str]:
        if ctx["search"]["patch"] != "none":
            return False, "exploit failed: search is parameterized"
        cache = ctx["cache"]
        balances = dict(cache.get("balances") or {})
        victim = str(params.get("account", "4211000105"))
        balances[victim] = 99_999_999
        cache.update({"balances": balances, "tampered": True})
        ctx["security"]["exploited"]["portal-sqli-search"] = True
        ctx.log("INFO", "HTTP-200", "INFO  [http-8080-exec-9] access GET /netbank/search.do?q=%27%3B%20UPDATE%20"
                                    "BALANCES%20SET%20AMT%3D99999999--  200 41ms")
        ctx.log("WARN", "SQL", f"WARN  [http-8080-exec-9] c.bank.dao.SearchDAO - SQL: SELECT * FROM ACCOUNTS WHERE "
                               f"NAME LIKE '%'; UPDATE BALANCES SET AMT=99999999 WHERE ACCT='{victim}'--%'")
        return True, f"SQL injection via search.do: balance of {victim} tampered"

    # ------------------------------------------------------------ legacy screens
    def view(self, ctx: Ctx, name: str, query: dict) -> tuple[str, str]:
        cache = ctx["cache"]
        if name == "balance":  # the SOAP response — always 200, even when stale
            acct = query.get("acct", "4211000101")
            bal = (cache.get("balances") or {}).get(acct, 1_000_000)
            return "text/xml", (
                '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
                '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">\n<soapenv:Body>\n'
                f'  <ns1:getBalanceResponse xmlns:ns1="urn:netbank">\n    <account>{escape(acct)}</account>\n'
                f'    <balance currency="USD">{bal / 100:.2f}</balance>\n'
                f'    <asOf>{iso(cache["balances_as_of"])}</asOf>\n  </ns1:getBalanceResponse>\n'
                '</soapenv:Body>\n</soapenv:Envelope>\n')
        if name == "search":
            q = query.get("q", "")
            if ctx["search"]["patch"] == "none" and "'" in q:
                rows = CUSTOMERS  # the injected OR '1'='1 returns everything
            else:
                rows = [c for c in CUSTOMERS if q and q.upper() in c]
            sql = (f"SELECT * FROM ACCOUNTS WHERE NAME LIKE '%{q}%'" if ctx["search"]["patch"] == "none"
                   else "SELECT * FROM ACCOUNTS WHERE NAME LIKE ? -- bound")
            items = "".join(f"<li>{escape(r)}</li>" for r in rows)
            return "text/html", f"<html><body><!-- {escape(sql)} --><h3>NetBank search</h3><ul>{items}</ul></body></html>"
        raise KeyError(name)


SYSTEM = Portal()

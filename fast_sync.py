"""Targeted ("fast") sync — one named customer or plant instead of the whole fleet.

`sync_all` walks every device at ~1 req/s, so a full sweep is a ~20 minute
commitment to the whole fleet. That is the wrong tool when a single account needs
to be current *now* — a support call, a customer linked five minutes ago, a site
someone is standing in front of. This resolves a **name** to the plants behind it
and syncs only those, which for a typical one-plant customer is a few seconds.

Names resolve against our own mirror first, so the usual case spends zero SAJ
calls before the readings pull. A name the catalog has never seen falls back to
one live plant-list page-through, and the rows it finds are written into the
catalog on the way past, so the next run is fast again.

While we are there, an unlinked plant whose name matches exactly one customer is
linked using the same conservative rule as `sync_customer_plants` — otherwise
"sync this customer" would quietly sync nothing for the customers that need it
most, the ones nobody has linked yet.

    python fast_sync.py --customer "Ah Seng"
    python fast_sync.py --plant "Taman Molek" --days 7
    POST /sync/fast?customer=Ah%20Seng
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from collections import defaultdict
from contextlib import contextmanager

import fetcher
import pg
from saj_api import SajClient
from sync_customer_plants import (
    MATCH_CONFIDENCE,
    MATCH_METHOD,
    insert_device_maps,
    link_plants,
    norm_name,
    to_float,
)

REQ_INTERVAL = float(os.environ.get("SAJ_REQ_INTERVAL", "1.0"))
JITTER = float(os.environ.get("SAJ_REQ_JITTER", "0.3"))
# Debug detail on by default for every fast sync; set FAST_SYNC_DEBUG=0 to quiet it.
DEBUG = os.environ.get("FAST_SYNC_DEBUG", "1").strip().lower() not in ("0", "false", "")

_PLANT_COLS = ["plant_uid", "plant_name", "owner_name", "installer_name",
               "pv_power_wp", "running_state", "type_name"]


class RunLog:
    """The run's narration — printed to the service log *and* returned to the caller.

    Testing this on prod means curling the endpoint, so the response carries the
    same lines Railway sees; chasing them through deploy logs to find out why a
    name did not resolve is exactly the friction this is here to remove.

    Every line is stamped with seconds since the run started, so a slow phase is
    visible without reading timestamps off separate log lines.
    """

    def __init__(self, debug: bool = DEBUG, prefix: str = "fast", echo: bool = True):
        self.debug_on = debug
        self.prefix = prefix
        self.echo = echo
        self.lines: list[str] = []
        self.t0 = time.time()
        self.timings: dict[str, float] = {}

    def _emit(self, level: str, msg: str) -> None:
        line = f"[{self.elapsed():6.2f}s] {level:<5} {msg}"
        self.lines.append(line)
        if self.echo:
            print(f"[{self.prefix}] {line}", flush=True)

    def elapsed(self) -> float:
        return time.time() - self.t0

    def info(self, msg: str) -> None:
        self._emit("info", msg)

    def warn(self, msg: str) -> None:
        self._emit("warn", msg)

    def debug(self, msg: str) -> None:
        if self.debug_on:
            self._emit("debug", msg)

    @contextmanager
    def step(self, name: str):
        """Time a phase and record it, so the summary shows where the time went."""
        start = time.time()
        self.debug(f"{name}: start")
        try:
            yield
        finally:
            took = round(time.time() - start, 3)
            self.timings[name] = round(self.timings.get(name, 0.0) + took, 3)
            self.debug(f"{name}: done in {took}s")


class TargetNotFound(LookupError):
    """No customer or plant answers to the given name."""


class TargetAmbiguous(LookupError):
    """The name matched several distinct records; the caller has to be specific.

    `candidates` reads well in a log line. `choices` is the machine-usable form:
    each carries what to send back to pick it, which is the only way through when
    several customers share a name outright and no spelling can separate them.
    """

    def __init__(self, query: str, candidates: list[str],
                 choices: list[dict] | None = None):
        super().__init__(
            f"{query!r} matches {len(candidates)}: " + ", ".join(candidates[:10])
        )
        self.query = query
        self.candidates = candidates
        self.choices = choices or [{"label": c} for c in candidates]


# ---- name resolution (pure — no DB, no portal) ----------------------------
def name_tokens(value: str | None) -> list[str]:
    """Whole words of a name, lowercased. "SDN.BHD." -> ["sdn", "bhd"]."""
    return [t for t in re.split(r"[^a-z0-9]+", (value or "").lower()) if t]


def _choice(rows: list[dict], field: str) -> dict:
    """A machine-usable way to pick this group of same-named rows."""
    choice = {"label": (rows[0].get(field) or "").strip()}
    uids = {str(r["plant_uid"]) for r in rows if r.get("plant_uid")}
    if field == "plant_name" and len(uids) == 1:
        choice["plant_uid"] = uids.pop()
    return choice


def select_by_name(rows: list[dict], field: str, query: str) -> list[dict]:
    """Rows whose `field` answers to `query`.

    An exact normalized match wins outright — that is the rule the catalog sync
    links on. Otherwise every *word* of the query must appear as a whole word in
    the candidate. Both halves of that matter, and each was learned the hard way:

    * Plants are named "<customer> (<site>) - <installer>", e.g. "JClands capital
      SDN BHD (Restaurant) - SELCO". A customer name is only ever a prefix of its
      plants, so requiring an exact match finds a company's sites never.
    * Matching raw substrings on the space-stripped form put "chen" inside
      "cheng", so customer "Chen" reached 32 unrelated sites. Whole words don't.

    Several distinct names still matching is reported, not guessed at: the caller
    gets `choices` to pick from, which for a company with two sites is the two
    sites, and for a shared surname is the people who share it.
    """
    q = norm_name(query)
    if not q:
        raise TargetNotFound("no name given")

    exact = [r for r in rows if norm_name(r.get(field)) == q]
    if exact:
        return exact

    want = set(name_tokens(query))
    hits = [r for r in rows if want and want <= set(name_tokens(r.get(field)))]
    if not hits:
        raise TargetNotFound(f"no match for {query!r}")

    groups: dict[str, list[dict]] = {}
    for r in hits:
        groups.setdefault(norm_name(r.get(field)), []).append(r)
    if len(groups) > 1:
        by_name = sorted(groups.values(), key=lambda g: (g[0].get(field) or ""))
        raise TargetAmbiguous(query,
                              [(g[0].get(field) or "").strip() for g in by_name],
                              [_choice(g, field) for g in by_name])
    return hits


def customer_for_name(name: str | None, customers: list[dict]) -> str | None:
    """The one customer with exactly this name, or None if zero or several."""
    key = norm_name(name)
    if not key:
        return None
    ids = sorted({str(c["customer_id"]) for c in customers
                  if norm_name(c.get("name")) == key})
    return ids[0] if len(ids) == 1 else None


# ---- mirror / portal loading ----------------------------------------------
def _rows(result: dict, label: str) -> list[dict]:
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"{label} failed: {result.get('detail') or result['error']}")
    return result.get("rows") or []


def load_customers() -> list[dict]:
    return _rows(
        pg.run("select customer_id, name from customer "
               "where name is not null and name <> ''"),
        "load customers",
    )


def load_plants() -> list[dict]:
    return _rows(
        pg.run("select plant_uid, plant_name, customer_id from saj_plant"),
        "load plants",
    )


def _portal_plants(client: SajClient) -> list[dict]:
    """The fleet plant list, reshaped to look like our own saj_plant rows."""
    out = []
    for p in client.list_plants():
        uid = str(p.get("plantUid") or "").strip()
        if uid:
            out.append({"plant_uid": uid, "plant_name": p.get("plantName"),
                        "customer_id": None, "_portal": p})
    return out


def _store_portal_plants(rows: list[dict]) -> None:
    """Write freshly discovered plants into the catalog so the next run is fast."""
    tuples = []
    for r in rows:
        p = r.get("_portal") or {}
        state = p.get("runningState")
        tuples.append((r["plant_uid"], p.get("plantName"), p.get("ownerName"),
                       p.get("installerName"), to_float(p.get("pvPower")),
                       str(state) if state is not None else None, p.get("typeName")))
    if tuples:
        pg.upsert("saj_plant", _PLANT_COLS, tuples, "plant_uid", _PLANT_COLS[1:])


def find_plants(name: str, stored: list[dict], client: SajClient,
                refresh: bool = False,
                log: RunLog | None = None) -> tuple[list[dict], str]:
    """Plants answering to `name` — mirror first, portal only if it has to be.

    Returns (plants, source). An ambiguous mirror hit is *not* retried against
    the portal: a wider search cannot make an ambiguous name less ambiguous.
    """
    if not refresh:
        try:
            return select_by_name(stored, "plant_name", name), "catalog"
        except TargetNotFound:
            if log:
                log.warn(f"{name!r} is not in the catalog — paging the portal's "
                         "plant list")

    portal = _portal_plants(client)
    if log:
        log.debug(f"portal returned {len(portal)} plants")
    found = select_by_name(portal, "plant_name", name)
    _store_portal_plants(found)
    if log:
        log.info(f"catalogued {len(found)} plant(s) discovered on the portal")
    # Carry over links we already hold, so a refresh does not look like an
    # unlinked plant and re-run the linking rules against a stale name.
    linked = {p["plant_uid"]: p.get("customer_id") for p in stored}
    for r in found:
        r["customer_id"] = linked.get(r["plant_uid"])
    return found, "portal"


def refresh_by_uid(client: SajClient, plants: list[dict],
                   log: RunLog | None = None) -> list[dict]:
    """Re-read these exact plants from the portal and write them into the catalog.

    Used when the caller already knows *which* plants they want. Searching the
    portal by name would be wrong here: a plant renamed since it was linked no
    longer answers to the customer's name, and we would drop it.
    """
    wanted = {p["plant_uid"] for p in plants}
    found = [r for r in _portal_plants(client) if r["plant_uid"] in wanted]
    _store_portal_plants(found)
    links = {p["plant_uid"]: p.get("customer_id") for p in plants}
    for r in found:
        r["customer_id"] = links.get(r["plant_uid"])
    # A plant the portal no longer lists is still synced from what we hold.
    seen = {r["plant_uid"] for r in found}
    stale = [p for p in plants if p["plant_uid"] not in seen]
    if log:
        log.debug(f"refreshed {len(found)} plant(s) from the portal by uid")
        for p in stale:
            log.warn(f"plant {p['plant_uid']} ({p.get('plant_name')!r}) is no "
                     "longer listed by the portal — syncing from the catalog")
    return found + stale


def _device_sns(client: SajClient, plant_uid: str) -> tuple[list[str], str]:
    """Device SNs for a plant — catalog first, one portal call if it is empty."""
    sns = fetcher.plant_sns(plant_uid)
    if sns:
        return sns, "catalog"
    sns = [str(sn) for sn in client.plant_device_sns(plant_uid) if sn]
    if sns:
        pg.upsert("saj_device", ["device_sn", "plant_uid"],
                  [(sn, plant_uid) for sn in sns], "device_sn", ["plant_uid"])
    return sns, "portal"


# ---- the run ---------------------------------------------------------------
def run(client: SajClient, *, customer: str | None = None, plant: str | None = None,
        customer_id: str | None = None, plant_uid: str | None = None,
        days: int = 1, link: bool = True, refresh_catalog: bool = False,
        interval: float | None = None, jitter: float | None = None,
        debug: bool | None = None, echo: bool = True,
        log: RunLog | None = None) -> dict:
    """Sync every device behind one named customer or plant. Returns a summary.

    Always a full pull, like the nightly sweep: asking for this account by name
    is an explicit request for current data, so the per-visit freshness gate
    that protects `/fetch/*` from refresh-hammering does not apply here.

    The summary carries the run's `log`, per-phase `timings` and the number of
    SAJ requests it actually cost, so a prod run can be diagnosed from its own
    response. Pass a `log` to have a caller collect the narration of a run that
    raises — the resolution failures are the ones worth reading.
    """
    if sum(map(bool, (customer, plant, customer_id, plant_uid))) != 1:
        raise ValueError("give exactly one of customer= / customer_id= / "
                         "plant= / plant_uid=")
    interval = REQ_INTERVAL if interval is None else interval
    jitter = JITTER if jitter is None else jitter
    log = log or RunLog(debug=DEBUG if debug is None else debug, echo=echo)
    calls0 = getattr(client, "calls", 0)

    kind = "plant" if (plant or plant_uid) else "customer"
    query = customer or plant or customer_id or plant_uid
    by_id = bool(customer_id or plant_uid)
    label = "customer_id" if customer_id else "plant_uid" if plant_uid else kind
    log.info(f"start {label}={query!r} "
             f"days={days} link={link} refresh_catalog={refresh_catalog}")
    log.debug(f"db backend={pg.backend()} interval={interval}s jitter={jitter}s")

    with log.step("load_mirror"):
        customers = load_customers()
        stored = load_plants()
    log.debug(f"mirror: customers={len(customers)} plants={len(stored)} "
              f"linked={sum(1 for p in stored if p.get('customer_id'))}")
    with log.step("resolve"):
        if customer or customer_id:
            if customer_id:
                # Picked from an ambiguous result: the id is the only thing that
                # separates several customers sharing one name.
                matched = [c for c in customers
                           if str(c["customer_id"]) == str(customer_id)]
                if not matched:
                    raise TargetNotFound(f"no customer with id {customer_id!r}")
                customer_id, matched_name = str(customer_id), matched[0].get("name")
            else:
                matched = select_by_name(customers, "name", customer)
                ids = sorted({str(r["customer_id"]) for r in matched})
                if len(ids) != 1:
                    raise TargetAmbiguous(
                        customer,
                        [f"{r.get('name')} ({r['customer_id']})" for r in matched],
                        [{"label": r.get("name"),
                          "customer_id": str(r["customer_id"])} for r in matched])
                customer_id, matched_name = ids[0], matched[0].get("name")
            log.info(f"customer {matched_name!r} -> {customer_id}")
            plants = [p for p in stored
                      if str(p.get("customer_id") or "") == customer_id]
            source = "catalog"
            log.debug(f"plants linked to {customer_id}: {len(plants)}")
            if plants and refresh_catalog:
                plants, source = refresh_by_uid(client, plants, log=log), "portal"
            elif not plants:
                # Known customer, nothing linked yet: reach for the plant that
                # carries their name — the edge the nightly catalog sync draws.
                log.warn(f"customer {matched_name!r} has no linked plant; "
                         "falling back to a plant of the same name")
                try:
                    plants, source = find_plants(matched_name, stored, client,
                                                 refresh=refresh_catalog, log=log)
                except TargetNotFound:
                    raise TargetNotFound(
                        f"customer {matched_name!r} has no linked plant and no "
                        f"plant is named {matched_name!r}") from None
        elif plant_uid:
            # Picked from an ambiguous result — a uid names one plant exactly.
            plants = [p for p in stored if str(p["plant_uid"]) == str(plant_uid)]
            source = "catalog"
            if not plants:
                plants = [r for r in _portal_plants(client)
                          if r["plant_uid"] == str(plant_uid)]
                if not plants:
                    raise TargetNotFound(f"no plant with uid {plant_uid!r}")
                _store_portal_plants(plants)
                source = "portal"
            matched_name = plants[0].get("plant_name")
        else:
            matched_name = plant
            plants, source = find_plants(plant, stored, client,
                                         refresh=refresh_catalog, log=log)

    log.info(f"matched {len(plants)} plant(s) via {source}")
    for p in plants:
        log.debug(f"  plant {p['plant_uid']} name={p.get('plant_name')!r} "
                  f"customer={p.get('customer_id')}")

    # Resolve devices and repair the customer edge before any readings are pulled,
    # so a slow readings phase never leaves the catalog half-updated.
    targets, jobs = [], []
    with log.step("devices_and_links"):
        for p in plants:
            uid = p["plant_uid"]
            sns, sn_source = _device_sns(client, uid)
            log.debug(f"  plant {uid}: {len(sns)} device(s) from {sn_source} "
                      f"{sns[:5]}{'...' if len(sns) > 5 else ''}")
            if not sns:
                log.warn(f"plant {uid} ({p.get('plant_name')!r}) has no devices")
            cid = str(p.get("customer_id") or "") or None
            linked_now = False
            if link and cid is None:
                # *Finding* a plant may be loose, but *writing* a customer edge
                # stays strict: an exact name match is the only evidence this
                # project links on, and a wrong link costs far more than none.
                cid = customer_for_name(p.get("plant_name"), customers)
                if cid and link_plants([(uid, cid)]):
                    linked_now = True
                    log.info(f"linked plant {uid} -> customer {cid}")
                elif cid:
                    cid = None  # refused: no such customer, or already set
                    log.debug(f"  plant {uid}: link refused by the db guard")
                else:
                    log.debug(f"  plant {uid}: unlinked, no exact customer-name "
                              "match — readings only")
            if link and cid and sns:
                mapped = insert_device_maps(
                    [(cid, sn, uid, MATCH_METHOD, MATCH_CONFIDENCE, False)
                     for sn in sns])
                log.debug(f"  plant {uid}: {mapped} new device map(s)")
            targets.append({"plant_uid": uid, "plant_name": p.get("plant_name"),
                            "customer_id": cid, "linked_now": linked_now,
                            "device_source": sn_source, "devices": sns})
            jobs.extend((uid, sn) for sn in sns)

    saj_before_readings = getattr(client, "calls", 0) - calls0
    log.info(f"devices={len(jobs)} days={days} "
             f"(saj calls so far: {saj_before_readings})")

    written: dict[str, int] = defaultdict(int)
    errors: list[dict] = []
    ok = 0
    with log.step("readings"):
        for i, (uid, sn) in enumerate(jobs):
            t_dev = time.time()
            try:
                res = fetcher.fetch_device(client, sn, days=days)
                written[uid] += res["rows_written"]
                ok += 1
                log.debug(f"  {sn}: {res['rows_written']} rows "
                          f"({res['source']}) in {time.time() - t_dev:.1f}s")
            except Exception as e:  # noqa: BLE001 — one bad inverter, not the run
                errors.append({"device_sn": sn, "plant_uid": uid,
                               "error": f"{type(e).__name__}: {e}"[:300]})
                log.warn(f"{sn} FAILED after {time.time() - t_dev:.1f}s: "
                         f"{type(e).__name__}: {e}")
            if i < len(jobs) - 1:
                time.sleep(interval + random.uniform(0, jitter))

    for t in targets:
        t["rows_written"] = written[t["plant_uid"]]
    summary = {
        "mode": "fast",
        "target": {"kind": kind,
                   "by": "id" if by_id else "name",
                   "query": query,
                   "matched": matched_name,
                   "customer_id": customer_id},
        "days": days,
        "catalog_source": source,
        "plants": targets,
        "plant_count": len(targets),
        "device_count": len(jobs),
        "rows_written": sum(written.values()),
        "ok": ok,
        "err": len(errors),
        "errors": errors[:10],
        "elapsed_s": round(log.elapsed(), 1),
        "debug": {
            "db_backend": pg.backend(),
            "timings_s": log.timings,
            "saj_calls": getattr(client, "calls", 0) - calls0,
            "saj_calls_before_readings": saj_before_readings,
            "mirror": {"customers": len(customers), "plants": len(stored)},
        },
        "log": log.lines,
    }
    log.info(f"DONE plants={len(targets)} devices={len(jobs)} ok={ok} "
             f"err={len(errors)} rows={summary['rows_written']} "
             f"saj_calls={summary['debug']['saj_calls']} "
             f"in {summary['elapsed_s']}s")
    summary["log"] = log.lines  # include the DONE line
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--customer", help="customer name to sync")
    group.add_argument("--plant", help="plant name to sync")
    group.add_argument("--customer-id", help="exact customer id")
    group.add_argument("--plant-uid", help="exact plant uid")
    parser.add_argument("--days", type=int, default=1,
                        help="days back to pull per device (1 = today only)")
    parser.add_argument("--no-link", action="store_true",
                        help="skip customer linking; only pull readings")
    parser.add_argument("--refresh-catalog", action="store_true",
                        help="re-read plant name/owner/state from the portal first")
    parser.add_argument("--quiet", action="store_true",
                        help="drop the debug lines; keep info and warnings")
    parser.add_argument("--json", action="store_true",
                        help="print only the summary JSON, no live log")
    args = parser.parse_args()

    user, pw = os.environ.get("SAJ_USER"), os.environ.get("SAJ_PASS")
    client = SajClient(username=user, password=pw) if user and pw else SajClient()
    # Shared with run() so a failed resolution still has its narration to print.
    log = RunLog(debug=not args.quiet, echo=not args.json)
    try:
        summary = run(client, customer=args.customer, plant=args.plant,
                      customer_id=args.customer_id, plant_uid=args.plant_uid,
                      days=args.days, link=not args.no_link,
                      refresh_catalog=args.refresh_catalog, log=log)
    except (TargetAmbiguous, TargetNotFound) as e:
        kind = "ambiguous" if isinstance(e, TargetAmbiguous) else "not found"
        log.warn(f"{kind}: {e}")
        if args.json:
            print(json.dumps({"mode": "fast", "error": kind, "detail": str(e),
                              "candidates": getattr(e, "candidates", None),
                              "log": log.lines}, indent=2, default=str))
        return 2 if isinstance(e, TargetAmbiguous) else 3
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0 if not summary["err"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

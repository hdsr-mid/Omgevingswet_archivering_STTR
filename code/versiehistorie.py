"""Every STTR version a bestuursorgaan has published, reconstructed from the RTR.

The RTR answers "which STTR is valid on date D" per regelbeheerobject, never
"list all versions".  So the history is rebuilt in two steps:

1. **Which regelbeheerobjecten exist(ed)?**  Walk the activity tree on a set
   of dates (monthly, plus every date the archive cares about).  An object
   that lived only between two walks can in principle be missed; monthly
   walks keep that window under a month.
2. **Which versions did each have?**  Follow the chain: a version is valid
   through its ``einddatum`` (inclusive) and the next one starts the day
   after.  ``toekomstigeSttrBegindatum`` names the *latest* version, not the
   next, so it only bounds the search; before the first version and in a
   real gap the search steps forward a week at a time.

The result is a JSON file that can be extended later: new walks only add
objects, and chains continue from the last known version.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import catalogus
from catalogus import Bestuursorgaan
from rtr_client import RtrClient, RtrError

log = logging.getLogger("rtr.versiehistorie")

STAP_IN_GAT = timedelta(days=7)


def nl_datum(tekst: str) -> date:
    return datetime.strptime(tekst, "%d-%m-%Y").date()


def maandelijks(van: date, tot: date) -> list[date]:
    """The first of every month from *van* through *tot*."""
    d, uit = date(van.year, van.month, 1), []
    while d <= tot:
        uit.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return uit


class Versiehistorie:
    """The STTR versions of one bestuursorgaan, kept in a JSON file."""

    def __init__(self, pad: str, bestuursorgaan: Bestuursorgaan, bron: str):
        self.pad = pad
        self.data: dict[str, Any] = {
            "bestuursorgaan": bestuursorgaan.naam, "oin": bestuursorgaan.oin,
            "roots": list(bestuursorgaan.roots), "bron": bron,
            "walks": [], "activiteiten": {}, "refs": {}}
        if os.path.exists(pad):
            with open(pad, encoding="utf-8") as f:
                self.data.update(json.load(f))
        self.bestuursorgaan = bestuursorgaan

    def bewaar(self) -> None:
        tijdelijk = self.pad + ".tmp"
        with open(tijdelijk, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tijdelijk, self.pad)

    # ── step 1: which regelbeheerobjecten ────────────────────────────

    def loop_boom(self, client: RtrClient, datums: Iterable[date]) -> int:
        """Walk the tree on every date not walked before; returns new refs."""
        nieuw = 0
        for d in sorted(set(datums)):
            if d.isoformat() in self.data["walks"]:
                continue
            for a in catalogus.enumereer_activiteiten(
                    client, self.bestuursorgaan, datum=d.isoformat(),
                    overgeslagen_suffix=()):
                act = self.data["activiteiten"].setdefault(
                    a.identifier, {"omschrijving": {}, "gezien_op": []})
                act["omschrijving"][d.isoformat()] = a.omschrijving
                act["gezien_op"].append(d.isoformat())
                for label, ref in a.regelbeheerobjecten.items():
                    if ref not in self.data["refs"]:
                        nieuw += 1
                        self.data["refs"][ref] = {
                            "activiteit": a.identifier, "label": label,
                            "gezien_op": [], "versies": []}
                    self.data["refs"][ref]["gezien_op"].append(d.isoformat())
            self.data["walks"].append(d.isoformat())
            self.data["walks"].sort()
            self.bewaar()
            log.info("Boom op %s: %d regelbeheerobjecten bekend", d,
                     len(self.data["refs"]))
        return nieuw

    # ── step 2: which versions ───────────────────────────────────────

    def volg_ketens(self, client: RtrClient, van: date, tot: date) -> int:
        """Follow every chain up to *tot*; returns the number of new versions."""
        nieuw = 0
        refs = sorted(self.data["refs"].items())
        for i, (ref, r) in enumerate(refs, 1):
            if r.get("gevolgd_tot", "") >= tot.isoformat():
                continue
            bekend = {v["sttrId"] for v in r["versies"]}
            if r["versies"]:
                # Continue from the last known version.
                start = max(van, max(nl_datum(v["begindatum"]) for v in r["versies"]))
            else:
                # Not seen on the walk before it was first seen, so its first
                # version starts after that walk.
                eerst = min(r["gezien_op"]) if r["gezien_op"] else tot.isoformat()
                eerder = [w for w in self.data["walks"] if w < eerst]
                start = max(van, date.fromisoformat(eerder[-1]) if eerder else van)
            for v in self._keten(client, ref, start, tot):
                if v["sttrId"] not in bekend:
                    r["versies"].append(v)
                    bekend.add(v["sttrId"])
                    nieuw += 1
            r["versies"].sort(key=lambda v: nl_datum(v["begindatum"]))
            r["gevolgd_tot"] = tot.isoformat()
            if i % 25 == 0:
                self.bewaar()
                log.info("Ketens: %d/%d regelbeheerobjecten", i, len(refs))
        self.bewaar()
        return nieuw

    @staticmethod
    def _keten(client: RtrClient, ref: str, van: date, tot: date):
        d = van
        while d <= tot:
            try:
                m = client.sttr_metadata(ref, d.isoformat())
            except RtrError as exc:
                log.error("%s op %s: %s", ref, d, exc)
                return
            if m.get("sttrId"):
                yield {"sttrId": str(m["sttrId"]),
                       "begindatum": m.get("begindatum"),
                       "einddatum": m.get("einddatum"),
                       "laatsteWijzigingDatum": m.get("laatsteWijzigingDatum")}
                if not m.get("einddatum"):
                    return
                d = max(nl_datum(m["einddatum"]) + timedelta(days=1),
                        d + timedelta(days=1))
            elif m.get("toekomstigeSttr") and m.get("toekomstigeSttrBegindatum"):
                d = max(min(d + STAP_IN_GAT, nl_datum(m["toekomstigeSttrBegindatum"])),
                        d + timedelta(days=1))
            else:
                return

    def voeg_toe(self, ref: str, activiteit: str, label: str,
                 versie: dict[str, Any]) -> bool:
        """Record a version seen elsewhere (a snapshot); True when it was new."""
        r = self.data["refs"].setdefault(
            ref, {"activiteit": activiteit, "label": label, "gezien_op": [],
                  "versies": []})
        if any(v["sttrId"] == versie["sttrId"] for v in r["versies"]):
            return False
        r["versies"].append(versie)
        r["versies"].sort(key=lambda v: nl_datum(v["begindatum"]))
        return True

    # ── reading ──────────────────────────────────────────────────────

    def versies(self) -> list[dict[str, Any]]:
        """Every version, flattened, with its regelbeheerobject and activity."""
        uit = []
        for ref, r in self.data["refs"].items():
            for v in r["versies"]:
                uit.append({**v, "ref": ref, "activiteit": r["activiteit"],
                            "soort": r["label"],
                            "omschrijving": self.omschrijving(r["activiteit"])})
        return uit

    def omschrijving(self, activiteit: str) -> str:
        namen = self.data["activiteiten"].get(activiteit, {}).get("omschrijving", {})
        return namen[max(namen)] if namen else activiteit

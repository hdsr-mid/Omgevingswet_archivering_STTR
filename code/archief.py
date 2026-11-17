"""
Archive a regeling per definitief besluit: its publications, the whole
regeling, and every version of the toepasbare regels (STTR) under it.

    python code/archief.py data/archief_CVDR707248.json
    python code/archief.py data/archief_CVDR707248.json --stappen publicaties,regeling
    python code/archief.py data/archief_CVDR707248.json --stappen rtr,wijzigingen,index

One folder per definitief besluit, named after its besluitdatum:

    archief/
      README.md  index.csv  versiehistorie.json  <config>.json
      20250715_wijzigingsbesluit-h2-3-4_wsb-2025-17997/
        LEESMIJ.md
        Ontwerp/     wsb-2025-11894_ontwerp.pdf  wsb-2025-12517_kennisgeving.pdf
        Definitief/  wsb-2025-17997_besluit.pdf
                     regeling_toestand-5_geldend-vanaf-20250724.pdf
        RTR/
          20250801_volledig/   status.xlsx  werkingsgebieden.txt  STTR/ (+ manifest.json)
          20250808_volledig/   …
          20250902_gewijzigd/  STTR/ (+ manifest.json): only what took effect that day

Completeness: every STTR version the RTR knows for the bestuursorgaan is filed
under the besluit in whose period (its in-werking date up to the next one) it
took effect.  It is in one of that besluit's full snapshots when it was valid
on a peildatum; otherwise it gets a ``<date>_gewijzigd`` folder named after
the day it took effect.  That holds for later modifications and for
short-lived first uploads alike.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import vertrouwen
from catalogus import Bestuursorgaan
from rtr import RTR
from rtr_client import BASE_URLS, RtrClient
from versiehistorie import Versiehistorie, maandelijks, nl_datum

log = logging.getLogger("rtr.archief")

PUBLICATIE_PDF = "https://zoek.officielebekendmakingen.nl/{id}.pdf"
CVDR_TOESTAND = "https://lokaleregelgeving.overheid.nl/{regeling}/{toestand}"
USER_AGENT = "rtr-archivering/2.0 (python-urllib)"
STAPPEN = ("publicaties", "regeling", "rtr", "wijzigingen", "index")


# ── configuration ────────────────────────────────────────────────────


@dataclass
class Besluit:
    besluitdatum: date
    naam: str
    in_werking: date
    ontwerp: list[dict[str, str]]
    definitief: list[dict[str, str]]
    toestanden: list[dict[str, Any]]
    peildata: list[date]
    opmerking: str = ""
    tot: date | None = None  # next besluit's in-werking date (exclusive)

    @property
    def map(self) -> str:
        besluit = next((d["id"] for d in self.definitief if d["soort"] == "besluit"),
                       self.definitief[0]["id"])
        return f"{self.besluitdatum:%Y%m%d}_{self.naam}_{besluit}"

    def bevat(self, d: date) -> bool:
        return self.in_werking <= d and (self.tot is None or d < self.tot)


@dataclass
class Config:
    pad: str
    bestuursorgaan: Bestuursorgaan
    regeling: dict[str, str]
    omgeving: str
    besluiten: list[Besluit] = field(default_factory=list)

    def besluit_voor(self, d: date) -> Besluit:
        """The besluit in whose period *d* falls (the first for anything earlier)."""
        for b in self.besluiten:
            if b.bevat(d):
                return b
        return self.besluiten[0]


def lees_config(pad: str) -> Config:
    with open(pad, encoding="utf-8") as f:
        ruw = json.load(f)
    bo = ruw["bestuursorgaan"]
    besluiten = [Besluit(
        besluitdatum=date.fromisoformat(b["besluitdatum"]), naam=b["naam"],
        in_werking=date.fromisoformat(b["in_werking"]),
        ontwerp=b.get("ontwerp", []), definitief=b["definitief"],
        toestanden=b.get("toestanden", []),
        peildata=[date.fromisoformat(d) for d in b.get("rtr_peildata", [])],
        opmerking=b.get("opmerking", "")) for b in ruw["besluiten"]]
    besluiten.sort(key=lambda b: b.in_werking)
    for huidig, volgend in zip(besluiten, besluiten[1:]):
        huidig.tot = volgend.in_werking
    for b in besluiten:
        buiten = [d for d in b.peildata if not b.bevat(d)]
        if buiten:
            raise ValueError(f"{b.map}: peildata {buiten} vallen buiten de periode "
                             f"van het besluit ({b.in_werking} tot {b.tot})")
    return Config(pad, Bestuursorgaan(bo["naam"], bo["oin"], (bo["root"],)),
                  ruw["regeling"], ruw.get("omgeving", "prod"), besluiten)


# ── downloads ────────────────────────────────────────────────────────


def sha256(pad: str) -> str:
    h = hashlib.sha256()
    with open(pad, "rb") as f:
        for blok in iter(lambda: f.read(1 << 20), b""):
            h.update(blok)
    return h.hexdigest()


def schrijf_atomair(pad: str, inhoud: bytes) -> None:
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    tijdelijk = pad + ".tmp"
    with open(tijdelijk, "wb") as f:
        f.write(inhoud)
    os.replace(tijdelijk, pad)


def haal(url: str, *, timeout: float = 300, pogingen: int = 3) -> bytes:
    verzoek = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for poging in range(1, pogingen + 1):
        try:
            with vertrouwen.open_url(urllib.request.urlopen, verzoek, url, timeout) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise
            fout = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            fout = exc
        log.warning("%s mislukt (poging %d/%d): %s", url, poging, pogingen, fout)
        time.sleep(2 ** poging)
    raise RuntimeError(f"{url} niet opgehaald: {fout}")


class ArchiefClient(RtrClient):
    """An RtrClient that downloads each STTR once, whatever folders it ends up in."""

    def __init__(self, *args, cache: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache = cache
        os.makedirs(cache, exist_ok=True)

    def download_sttr(self, sttr_id):
        pad = os.path.join(self.cache, f"{sttr_id}.xml")
        if os.path.exists(pad):
            with open(pad, "rb") as f:
                return f.read()
        inhoud = super().download_sttr(sttr_id)
        schrijf_atomair(pad, inhoud)
        return inhoud


# ── the archive ──────────────────────────────────────────────────────


class Archief:
    def __init__(self, config: Config, uit: str, client: ArchiefClient):
        self.config = config
        self.uit = uit
        self.client = client

    def map(self, besluit: Besluit, *delen: str) -> str:
        return os.path.join(self.uit, besluit.map, *delen)

    # publicaties ─────────────────────────────────────────────────────

    def publicaties(self) -> None:
        for b in self.config.besluiten:
            for submap, documenten in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief)):
                for doc in documenten:
                    pad = self.map(b, submap, f"{doc['id']}_{doc['soort']}.pdf")
                    if self._is_pdf(pad):
                        continue
                    inhoud = haal(PUBLICATIE_PDF.format(id=doc["id"]))
                    if not inhoud.startswith(b"%PDF"):
                        raise RuntimeError(f"{doc['id']}: geen PDF ontvangen")
                    schrijf_atomair(pad, inhoud)
                    log.info("%s: %s (%.1f MB)", b.map, os.path.basename(pad), len(inhoud) / 1e6)

    @staticmethod
    def _is_pdf(pad: str) -> bool:
        if not os.path.exists(pad):
            return False
        with open(pad, "rb") as f:
            return f.read(4) == b"%PDF"

    # de hele regeling ────────────────────────────────────────────────

    def regeling(self, browser: str | None) -> None:
        """Each toestand as PDF, printed from its official consolidated page.

        The wijzigingsbesluiten only carry their changes (renvooi), so the
        whole regeling as it read from each in-werking date is printed from
        lokaleregelgeving.overheid.nl with the site's own print style, in A4,
        with the source and print date in the footer.  The page must show the
        expected "Geldend van" date, or nothing is written.
        """
        taken = []
        for b in self.config.besluiten:
            for t in b.toestanden:
                vanaf = date.fromisoformat(t["geldig_vanaf"])
                pad = self.map(b, "Definitief",
                               f"regeling_toestand-{t['nummer']}_geldend-vanaf-{vanaf:%Y%m%d}.pdf")
                if not self._is_pdf(pad):
                    url = CVDR_TOESTAND.format(regeling=self.config.regeling["cvdr"],
                                               toestand=t["nummer"])
                    taken.append((url, vanaf, pad))
        if taken:
            print_pdfs(taken, browser)

    # RTR: volledige vastleggingen ───────────────────────────────────

    def rtr(self) -> None:
        for b in self.config.besluiten:
            for peildatum in b.peildata:
                uit = self.map(b, "RTR", f"{peildatum:%Y%m%d}_volledig")
                if os.path.exists(os.path.join(uit, "STTR", "manifest.json")):
                    log.info("%s: vastlegging %s bestaat al", b.map, peildatum)
                    continue
                log.info("%s: vastlegging op %s", b.map, peildatum)
                args = SimpleNamespace(
                    date=f"{peildatum:%d-%m-%Y}", env=self.config.omgeving,
                    rate=None,  # the archive's own client is used
                    sttr=True, location=True, include_t5=False,
                    oin=self.config.bestuursorgaan.oin,
                    overheid=self.config.bestuursorgaan.naam)
                RTR(args, self.client, uitvoer=uit, datumveld="begin",
                    bestuursorgaan=self.config.bestuursorgaan).archive_activities()

    def vastleggingen(self) -> dict[str, dict[str, Any]]:
        """Every full snapshot's manifest, keyed by its folder (relative)."""
        uit = {}
        for b in self.config.besluiten:
            for peildatum in b.peildata:
                pad = self.map(b, "RTR", f"{peildatum:%Y%m%d}_volledig", "STTR", "manifest.json")
                if os.path.exists(pad):
                    with open(pad, encoding="utf-8") as f:
                        uit[os.path.relpath(os.path.dirname(pad), self.uit)] = json.load(f)
        return uit

    # RTR: alles wat niet in een vastlegging zit ─────────────────────

    def historie(self, tot: date) -> Versiehistorie:
        h = Versiehistorie(os.path.join(self.uit, "versiehistorie.json"),
                           self.config.bestuursorgaan, self.client.base_url)
        eerste = self.config.besluiten[0].in_werking
        walks = set(maandelijks(eerste, tot)) | {tot}
        walks |= {d for b in self.config.besluiten for d in b.peildata + [b.in_werking] if d <= tot}
        nieuw = h.loop_boom(self.client, walks)
        log.info("Boom: %d nieuwe regelbeheerobjecten", nieuw)
        nieuw = h.volg_ketens(self.client, eerste, tot)
        log.info("Ketens: %d nieuwe versies", nieuw)
        # What a snapshot saw is history too: a version only seen there is added.
        for manifest in self.vastleggingen().values():
            for s in manifest["sttrs"]:
                if s.get("functioneleStructuurRef") and h.voeg_toe(
                        s["functioneleStructuurRef"], s["activiteit"], s["soort"],
                        {k: s.get(k) for k in ("sttrId", "begindatum", "einddatum",
                                               "laatsteWijzigingDatum")}):
                    log.warning("Versie %s kwam alleen in een vastlegging voor", s["sttrId"])
        h.bewaar()
        return h

    def wijzigingen(self, tot: date) -> None:
        h = self.historie(tot)
        in_vastlegging: dict[str, set[str]] = defaultdict(set)
        for rel, manifest in self.vastleggingen().items():
            in_vastlegging[rel.split(os.sep)[0]].update(s["sttrId"] for s in manifest["sttrs"])

        per_map = {os.path.join(self.uit, besluitmap, "RTR", naam): items
                   for (besluitmap, naam), items in
                   verdeel(self.config, h.data["refs"], in_vastlegging, tot,
                           h.omschrijving).items()}
        for map_pad, items in sorted(per_map.items()):
            sttr_map = os.path.join(map_pad, "STTR")
            os.makedirs(sttr_map, exist_ok=True)
            manifest = []
            for v in sorted(items, key=lambda x: (x["activiteit"], x["soort"])):
                if v.get("vervallen"):
                    manifest.append(v)
                    continue
                naam = (f"STTR_{v['sttrId']}_{v['activiteit'].split('.')[-1]}_"
                        f"{v['soort'].replace(' ', '_')}.xml")
                inhoud = self.client.download_sttr(v["sttrId"])
                pad = os.path.join(sttr_map, naam)
                if not os.path.exists(pad):
                    schrijf_atomair(pad, inhoud)
                manifest.append({"bestand": naam, **{k: v[k] for k in (
                    "activiteit", "omschrijving", "soort", "sttrId", "begindatum",
                    "einddatum", "laatsteWijzigingDatum", "ref", "vervangt", "vervangen_door")},
                    "sha256": hashlib.sha256(inhoud).hexdigest()})
            with open(os.path.join(sttr_map, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"bestuursorgaan": self.config.bestuursorgaan.naam,
                           "datum": os.path.basename(map_pad)[:8],
                           "toelichting": "STTR-versies die op deze dag ingingen en niet in een "
                                          "volledige vastlegging van dit besluit staan, en "
                                          "regelbeheerobjecten die op deze dag vervielen.",
                           "bron": self.client.base_url, "sttrs": manifest},
                          f, ensure_ascii=False, indent=2)
            log.info("%s: %d versies, %d vervallen", os.path.relpath(map_pad, self.uit),
                     sum(1 for m in manifest if not m.get("vervallen")),
                     sum(1 for m in manifest if m.get("vervallen")))

    # index ───────────────────────────────────────────────────────────

    def index(self) -> dict[str, Any]:
        h = Versiehistorie(os.path.join(self.uit, "versiehistorie.json"),
                           self.config.bestuursorgaan, self.client.base_url)
        alle = {v["sttrId"] for v in h.versies()}
        gearchiveerd: dict[str, list[str]] = defaultdict(list)
        mappen: dict[str, dict[str, Any]] = {}
        for wortel, _, bestanden in os.walk(self.uit):
            if "manifest.json" in bestanden and os.path.basename(wortel) == "STTR":
                with open(os.path.join(wortel, "manifest.json"), encoding="utf-8") as f:
                    m = json.load(f)
                rel = os.path.relpath(os.path.dirname(wortel), self.uit)
                mappen[rel] = m
                for s in m["sttrs"]:
                    if not s.get("vervallen"):
                        gearchiveerd[s["sttrId"]].append(rel)
        ontbreekt = sorted(alle - set(gearchiveerd))

        weg = verdwenen(h.data)
        for b in self.config.besluiten:
            self._leesmij(b, mappen, [v for v in weg if self.config.besluit_voor(
                date.fromisoformat(v["eerst_weg"])).map == b.map])
        self._readme(mappen, len(alle), len(alle) - len(ontbreekt), ontbreekt, weg)
        shutil.copy2(self.config.pad, os.path.join(self.uit, os.path.basename(self.config.pad)))
        # Last, so the hashes cover the README files just written.
        with open(os.path.join(self.uit, "index.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["pad", "bytes", "sha256"])
            for wortel, mapnamen, bestanden in os.walk(self.uit):
                mapnamen.sort()
                for naam in sorted(bestanden):
                    if naam in ("index.csv", "archief.log") or naam.endswith(".tmp"):
                        continue
                    pad = os.path.join(wortel, naam)
                    w.writerow([os.path.relpath(pad, self.uit).replace(os.sep, "/"),
                                os.path.getsize(pad), sha256(pad)])
        return {"versies": len(alle), "gearchiveerd": len(alle) - len(ontbreekt),
                "ontbreekt": ontbreekt}

    def _bestanden(self, b: Besluit, submap: str) -> list[str]:
        pad = self.map(b, submap)
        return sorted(os.listdir(pad)) if os.path.isdir(pad) else []

    def _leesmij(self, b: Besluit, mappen: dict[str, dict[str, Any]],
                 weg: list[dict[str, Any]]) -> None:
        regels = [f"# {self.config.regeling['titel']}: besluit van {b.besluitdatum:%d-%m-%Y}", ""]
        regels += [f"- In werking: {b.in_werking:%d-%m-%Y}"
                   + (f" (tot {b.tot:%d-%m-%Y})" if b.tot else ""),
                   f"- Bekendmaking: " + ", ".join(d["id"] for d in b.definitief if d["soort"] == "besluit"),
                   f"- Toestand(en) in CVDR {self.config.regeling['cvdr']}: "
                   + ", ".join(str(t["nummer"]) for t in b.toestanden), ""]
        if b.opmerking:
            regels += [b.opmerking, ""]
        for submap, docs in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief)):
            regels.append(f"## {submap}")
            regels.append("")
            if not docs and submap == "Ontwerp":
                regels += ["Geen ontwerp gepubliceerd.", ""]
            for doc in docs:
                regels.append(f"- `{doc['id']}_{doc['soort']}.pdf`: {doc['titel']} "
                              f"({PUBLICATIE_PDF.format(id=doc['id'])})")
            if submap == "Definitief":
                for t in b.toestanden:
                    url = CVDR_TOESTAND.format(regeling=self.config.regeling["cvdr"], toestand=t["nummer"])
                    vanaf = date.fromisoformat(t["geldig_vanaf"])
                    regels.append(f"- `regeling_toestand-{t['nummer']}_geldend-vanaf-{vanaf:%Y%m%d}.pdf`: "
                                  f"de hele regeling zoals die gold vanaf {vanaf:%d-%m-%Y}, "
                                  f"afgedrukt van {url}")
            regels.append("")
        regels += ["## Toepasbare regels (RTR)", "",
                   "| Map | Soort | STTR-bestanden | Vervallen |", "|---|---|---|---|"]
        for rel in sorted(k for k in mappen if k.split(os.sep)[0] == b.map):
            m = mappen[rel]
            naam = os.path.basename(rel)
            soort = ("volledige vastlegging op " if naam.endswith("_volledig") else "gewijzigd op ") \
                + f"{naam[6:8]}-{naam[4:6]}-{naam[:4]}"
            regels.append(f"| `RTR/{naam}` | {soort} | "
                          f"{sum(1 for s in m['sttrs'] if not s.get('vervallen'))} | "
                          f"{sum(1 for s in m['sttrs'] if s.get('vervallen'))} |")
        regels += ["", "Een volledige vastlegging bevat alle toepasbare regels die op die dag golden "
                   "(`status.xlsx` met per soort de datum waarop de versie inging, "
                   "`werkingsgebieden.txt`, en `STTR/`). Een map `…_gewijzigd` bevat alleen de "
                   "versies die op die dag ingingen en in geen volledige vastlegging van dit besluit "
                   "staan, zoals latere wijzigingen; `STTR/manifest.json` noemt per bestand welke "
                   "versie het vervangt en waardoor het werd vervangen.", ""]
        if weg:
            regels += ["## Uit de RTR verdwenen", "",
                       "De RTR haalt activiteiten en regelbeheerobjecten uit de boom zonder hun "
                       "STTR-versies te beëindigen. Hun bestanden staan in dit archief; dit is "
                       "wanneer ze verdwenen (tussen twee rondes door de boom).", "",
                       "| Wat | Activiteit | Laatst gezien | Weg op |", "|---|---|---|---|"]
            for v in weg:
                wat = v["soort"] + (f" ({v['label']})" if v.get("label") else "")
                regels.append(f"| {wat} | {v['omschrijving']} (`{v['id'].split('/')[-1]}`) | "
                              f"{date.fromisoformat(v['laatst_gezien']):%d-%m-%Y} | "
                              f"{date.fromisoformat(v['eerst_weg']):%d-%m-%Y} |")
            regels.append("")
        with open(self.map(b, "LEESMIJ.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(regels))

    def _readme(self, mappen, totaal: int, gearchiveerd: int, ontbreekt: list[str],
                weg: list[dict[str, Any]]) -> None:
        bo = self.config.bestuursorgaan
        regels = [f"# Archief {self.config.regeling['titel']}", "",
                  f"{bo.naam} (OIN {bo.oin}), regeling {self.config.regeling['cvdr']}. "
                  f"Samengesteld op {date.today():%d-%m-%Y} met `code/archief.py`.", "",
                  "Per definitief besluit één map, genoemd naar de besluitdatum. Daarin het ontwerp "
                  "en de kennisgevingen, het besluit zelf, de hele regeling zoals die vanaf de "
                  "inwerkingtreding gold, en de toepasbare regels: volledige vastleggingen op de "
                  "gekozen peildata en een map per dag waarop later nog iets veranderde.", "",
                  "| Besluit | Map | In werking | Ontwerp | Definitief | Vastleggingen | Gewijzigd op |",
                  "|---|---|---|---|---|---|---|"]
        for b in self.config.besluiten:
            eigen = sorted(os.path.basename(k) for k in mappen if k.split(os.sep)[0] == b.map)
            vast = ", ".join(f"{n[6:8]}-{n[4:6]}-{n[:4]}" for n in eigen if n.endswith("_volledig"))
            gew = sum(1 for n in eigen if n.endswith("_gewijzigd"))
            regels.append(
                f"| {b.besluitdatum:%d-%m-%Y} | `{b.map}` | {b.in_werking:%d-%m-%Y} | "
                f"{len(b.ontwerp)} | {len(b.definitief)} + {len(b.toestanden)} regeling | "
                f"{vast or '-'} | {gew} dag(en) |")
        regels += ["", "## Volledigheid", "",
                   f"De RTR kent {totaal} versies van toepasbare regels voor dit bestuursorgaan "
                   f"(`versiehistorie.json`); {gearchiveerd} daarvan staan in dit archief"
                   + (" (alle)." if not ontbreekt else f". Ontbrekend: {', '.join(ontbreekt)}."), "",
                   "De versiegeschiedenis is opgebouwd door de activiteitenboom maandelijks en op "
                   "elke peildatum af te lopen en per regelbeheerobject de keten van versies te "
                   "volgen. Een regelbeheerobject dat korter dan een maand bestond tussen twee "
                   "rondes in, kan daardoor in theorie ontbreken.", "",
                   f"{sum(1 for v in weg if v['soort'] == 'activiteit')} activiteit(en) en "
                   f"{sum(1 for v in weg if v['soort'] == 'regelbeheerobject')} losse "
                   "regelbeheerobject(en) verdwenen in die periode uit de RTR; het `LEESMIJ.md` "
                   "van het besluit waaronder dat gebeurde, noemt ze.", "",
                   "`index.csv` noemt elk bestand met grootte en SHA-256.", ""]
        with open(os.path.join(self.uit, "README.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(regels))


# ── filing: which version goes where ─────────────────────────────────


def verdeel(config: Config, refs: dict[str, dict[str, Any]],
            in_vastlegging: dict[str, set[str]], tot: date,
            omschrijving=lambda activiteit: activiteit
            ) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every version that no full snapshot of its own besluit holds.

    A version belongs to the besluit in whose period it took effect.  When
    one of that besluit's snapshots holds it, it is archived there; otherwise
    it goes to ``(besluitmap, "<jjjjmmdd>_gewijzigd")`` for the day it took
    effect.  A regelbeheerobject whose last version ended is recorded as
    vervallen on the day after, in the same way.
    """
    uit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ref, r in refs.items():
        versies = sorted(r["versies"], key=lambda v: nl_datum(v["begindatum"]))
        for i, v in enumerate(versies):
            begin = nl_datum(v["begindatum"])
            b = config.besluit_voor(begin)
            if v["sttrId"] in in_vastlegging.get(b.map, set()):
                continue
            uit[(b.map, f"{begin:%Y%m%d}_gewijzigd")].append({
                **v, "ref": ref, "activiteit": r["activiteit"], "soort": r["label"],
                "omschrijving": omschrijving(r["activiteit"]),
                "vervangt": versies[i - 1]["sttrId"] if i else None,
                "vervangen_door": versies[i + 1]["sttrId"] if i + 1 < len(versies) else None})
        if versies and versies[-1].get("einddatum"):
            weg = nl_datum(versies[-1]["einddatum"]) + timedelta(days=1)
            if weg <= tot:
                b = config.besluit_voor(weg)
                uit[(b.map, f"{weg:%Y%m%d}_gewijzigd")].append({
                    "vervallen": True, "ref": ref, "activiteit": r["activiteit"],
                    "soort": r["label"], "omschrijving": omschrijving(r["activiteit"]),
                    "laatste_versie": versies[-1]["sttrId"]})
    return dict(uit)


def verdwenen(historie: dict[str, Any]) -> list[dict[str, Any]]:
    """Activities and regelbeheerobjecten that left the activity tree.

    The RTR removes them from the functionele structuur without ending their
    STTR versions, so only the walks show it: seen on one walk, absent on
    every later one.  It happened after ``laatst_gezien`` and on or before
    ``eerst_weg``.  A regelbeheerobject is only listed while its activity is
    still there; otherwise the activity says it all.
    """
    walks = sorted(historie.get("walks", []))
    if not walks:
        return []

    def na(datum: str) -> str | None:
        later = [w for w in walks if w > datum]
        return later[0] if later else None

    uit = []
    acts = historie.get("activiteiten", {})
    for ident, a in acts.items():
        laatst = max(a["gezien_op"])
        if laatst < walks[-1]:
            namen = a.get("omschrijving", {})
            uit.append({"soort": "activiteit", "id": ident,
                        "omschrijving": namen[max(namen)] if namen else ident,
                        "laatst_gezien": laatst, "eerst_weg": na(laatst)})
    for ref, r in historie.get("refs", {}).items():
        if not r.get("gezien_op"):
            continue
        laatst = max(r["gezien_op"])
        eerst_weg = na(laatst)
        act = acts.get(r["activiteit"], {})
        if eerst_weg and eerst_weg in act.get("gezien_op", []):
            namen = act.get("omschrijving", {})
            uit.append({"soort": "regelbeheerobject", "id": ref, "label": r["label"],
                        "omschrijving": namen[max(namen)] if namen else r["activiteit"],
                        "laatst_gezien": laatst, "eerst_weg": eerst_weg})
    return sorted(uit, key=lambda v: (v["eerst_weg"], v["soort"] != "activiteit",
                                      v["omschrijving"].casefold(), v.get("label", "")))


# ── printing the regeling ────────────────────────────────────────────


def print_pdfs(taken: list[tuple[str, date, str]], browser: str | None) -> None:
    """Print each (url, expected Geldend-van date, pdf path) with Chromium/Edge.

    Uses Playwright: with *browser* an explicit Chromium-family executable,
    otherwise the installed Microsoft Edge or Google Chrome, otherwise
    Playwright's own Chromium.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("De hele regeling is niet afgedrukt: installeer eerst Playwright "
                  "(pip install -r requirements-archief.txt)")
        return
    vandaag = f"{date.today():%d-%m-%Y}"
    with sync_playwright() as p:
        pogingen = [dict(executable_path=browser)] if browser else \
            [dict(channel="msedge"), dict(channel="chrome"), {}]
        for opties in pogingen:
            try:
                b = p.chromium.launch(**opties)
                break
            except Exception as exc:  # noqa: BLE001 - try the next browser
                log.debug("Browser %s niet beschikbaar: %s", opties, exc)
        else:
            log.error("Geen Chromium, Chrome of Edge gevonden om de regeling af te drukken")
            return
        for url, vanaf, pad in taken:
            pagina = b.new_page()
            pagina.goto(url, wait_until="load", timeout=600_000)
            tekst = pagina.inner_text("body")
            verwacht = f"Geldend van {vanaf:%d-%m-%Y}"
            if verwacht not in tekst:
                pagina.close()
                raise RuntimeError(f"{url} toont niet '{verwacht}'; niets geschreven")
            voet = (f'<div style="font-size:7pt;width:100%;margin:0 12mm;display:flex;'
                    f'justify-content:space-between;color:#555">'
                    f'<span>{url} (afgedrukt {vandaag})</span>'
                    f'<span>Pagina <span class="pageNumber"></span> van '
                    f'<span class="totalPages"></span></span></div>')
            os.makedirs(os.path.dirname(pad), exist_ok=True)
            pagina.pdf(path=pad + ".tmp", format="A4", print_background=True,
                       display_header_footer=True, header_template="<span></span>",
                       footer_template=voet,
                       margin={"top": "15mm", "bottom": "18mm", "left": "12mm", "right": "12mm"})
            pagina.close()
            os.replace(pad + ".tmp", pad)
            log.info("Regeling afgedrukt: %s (%.1f MB)", os.path.basename(pad),
                     os.path.getsize(pad) / 1e6)
        b.close()


# ── command line ─────────────────────────────────────────────────────


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    basis = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description="Archiveert een regeling per definitief besluit: "
                                            "publicaties, hele regeling en alle toepasbare regels.")
    p.add_argument("config", help="JSON met het bestuursorgaan, de regeling en de besluiten")
    p.add_argument("--uit", default=os.path.join(basis, "archief"), help="doelmap (standaard: archief/)")
    p.add_argument("--stappen", default=",".join(STAPPEN),
                   help=f"komma-gescheiden, uit: {', '.join(STAPPEN)} (standaard: alle)")
    p.add_argument("--rate", type=float, default=2.0, help="verzoeken per seconde aan de RTR")
    p.add_argument("--tot", default=date.today().isoformat(),
                   help="versiegeschiedenis tot en met deze datum (jjjj-mm-dd, standaard: vandaag)")
    p.add_argument("--browser", default=None,
                   help="pad naar een Chromium/Chrome/Edge om de regeling af te drukken")
    p.add_argument("--cache", default=os.path.join(basis, "log", "sttr-cache"),
                   help="map voor eenmaal gedownloade STTR-bestanden")
    args = p.parse_args(argv)

    stappen = [s.strip() for s in args.stappen.split(",") if s.strip()]
    onbekend = set(stappen) - set(STAPPEN)
    if onbekend:
        p.error(f"onbekende stap(pen): {', '.join(sorted(onbekend))}")
    os.makedirs(args.uit, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(args.uit, "archief.log"), encoding="utf-8")])

    config = lees_config(args.config)
    client = ArchiefClient(BASE_URLS[config.omgeving], rate=args.rate, cache=args.cache)
    archief = Archief(config, args.uit, client)
    tot = date.fromisoformat(args.tot)

    if "publicaties" in stappen:
        archief.publicaties()
    if "regeling" in stappen:
        archief.regeling(args.browser)
    if "rtr" in stappen:
        archief.rtr()
    if "wijzigingen" in stappen:
        archief.wijzigingen(tot)
    if "index" in stappen:
        uitkomst = archief.index()
        log.info("Volledigheid: %d van %d versies gearchiveerd%s", uitkomst["gearchiveerd"],
                 uitkomst["versies"], "" if not uitkomst["ontbreekt"] else
                 f"; ontbrekend: {', '.join(uitkomst['ontbreekt'])}")
        if uitkomst["ontbreekt"]:
            return 1
    log.info("Klaar; %d verzoeken aan de RTR", client.requests_made)
    return 0


if __name__ == "__main__":
    sys.exit(main())

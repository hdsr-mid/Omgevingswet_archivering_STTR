"""
Archive a regeling per definitief besluit: its publications, the whole
regeling, and every version of the toepasbare regels (STTR) under it.

    python code/archief.py data/archief_CVDR707248.json
    python code/archief.py data/archief_CVDR707248.json --stappen publicaties,bijlagen,regeling
    python code/archief.py data/archief_CVDR707248.json --stappen rtr,wijzigingen,index

One folder per definitief besluit, named after its besluitdatum:

    archief/
      README.md  index.csv  versiehistorie.json  <config>.json
      20250715_wijzigingsbesluit-h2-3-4_wsb-2025-17997/
        README.txt   what is in the folder, and with a '!' what is expected but not
        bijlagen.json  where every kaart and bijlage came from
        Ontwerp/     wsb-2025-11894_ontwerp.pdf  wsb-2025-12517_kennisgeving.pdf
                     bijlagen/  informatieobjecten (PDF) and externe bijlagen
        Definitief/  wsb-2025-17997_besluit.pdf  bijlagen/
                     regeling_toestand-5_geldend-vanaf-20250724.pdf
        kaarten/     the GIOs (GML) the besluit sets; ONTWERP_… for one only the ontwerp had
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
import io
import json
import logging
import os
import re
import shutil
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from fnmatch import fnmatch
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
PUBLICATIE_HTML = "https://zoek.officielebekendmakingen.nl/{id}.html"
PUBLICATIE_METADATA = ("https://repository.overheid.nl/frbr/officielepublicaties/"
                       "{blad}/{jaar}/{id}/1/metadata/metadata.xml")
JOIN = "https://identifier.officielebekendmakingen.nl{join}"
DATACOLLECTIE = "https://repository.officiele-overheidspublicaties.nl/Datacollecties/{jaar}/{dc}/1/"
EXTERNE_BIJLAGE = "https://repository.officiele-overheidspublicaties.nl/externebijlagen/{exb}/currentItem"
CVDR_TOESTAND = "https://lokaleregelgeving.overheid.nl/{regeling}/{toestand}"
USER_AGENT = "rtr-archivering/2.0 (python-urllib)"
STAPPEN = ("publicaties", "bijlagen", "regeling", "rtr", "wijzigingen", "index")
GEWIJZIGD = re.compile(r"^\d{8}_gewijzigd$")

# What a besluit has outside the public sources: the organisation's own files.
# README.txt names each with a '!' in front until a file matching the pattern
# is put in the besluit folder (under the fase's submap, or anywhere outside
# RTR/ for fase "").  ``intern`` in the config names the expected file.
# (sleutel, fase, sectie, omschrijving, patroon, alleen bij een wijziging)
VERWACHT = (
    ("mutaties", "", "readme", "overzicht mutaties (.xlsx)", "*.xlsx", True),
    ("ontwerp.kennisgeving", "Ontwerp", "kennisgeving terinzagelegging",
     "kennisgeving terinzagelegging (.docx)", "*.docx", False),
    ("ontwerp.renvooi", "Ontwerp", "renvooi", "renvooi (.pdf)", "*renvooi*", True),
    ("ontwerp.kb", "Ontwerp", "Match", "Match-bestand (.kb)", "*.kb", False),
    ("ontwerp.zip", "Ontwerp", "Match", "aanleverpakket (.zip)", "*.zip", False),
    ("ontwerp.geconsolideerd", "Ontwerp", "Match", "geconsolideerde versie uit Match (.pdf)",
     "*geconsolideerd*", False),
    ("besluit.kb", "Definitief", "Match", "Match-bestand (.kb)", "*.kb", False),
    ("besluit.zip", "Definitief", "Match", "aanleverpakket (.zip)", "*.zip", False),
)


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
    dm: list[dict[str, str]] = field(default_factory=list)  # {omschrijving, nummer, fase?}
    intern: dict[str, str] = field(default_factory=dict)    # VERWACHT-sleutel -> bestandsnaam
    tot: date | None = None  # next besluit's in-werking date (exclusive)

    @property
    def map(self) -> str:
        besluit = next((d["id"] for d in self.definitief if d["soort"] == "besluit"),
                       self.definitief[0]["id"])
        return f"{self.besluitdatum:%Y%m%d}_{self.naam}_{besluit}"

    @property
    def nieuw(self) -> bool:
        """Sets up the regeling (its first toestand) rather than changing it."""
        return any(t["nummer"] == 1 for t in self.toestanden)

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
        opmerking=b.get("opmerking", ""), dm=b.get("dm", []),
        intern=b.get("intern", {})) for b in ruw["besluiten"]]
    bekend = {v[0] for v in VERWACHT}
    for b in besluiten:
        onbekend = set(b.intern) - bekend
        if onbekend:
            raise ValueError(f"{b.map}: onbekende sleutel(s) in intern: {', '.join(sorted(onbekend))} "
                             f"(bekend: {', '.join(sorted(bekend))})")
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
    return haal_met_url(url, timeout=timeout, pogingen=pogingen)[0]


def haal_met_url(url: str, *, timeout: float = 300, pogingen: int = 3) -> tuple[bytes, str]:
    """The body and the URL it came from after redirects."""
    verzoek = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for poging in range(1, pogingen + 1):
        try:
            with vertrouwen.open_url(urllib.request.urlopen, verzoek, url, timeout) as r:
                return r.read(), r.geturl()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise
            fout = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            fout = exc
        log.warning("%s mislukt (poging %d/%d): %s", url, poging, pogingen, fout)
        time.sleep(2 ** poging)
    raise RuntimeError(f"{url} niet opgehaald: {fout}")


def lees_metadata(inhoud: bytes) -> dict[str, list[str]]:
    """The ``<metadata name=… content=…/>`` of a publication, as name -> contents."""
    uit: dict[str, list[str]] = defaultdict(list)
    for el in ET.fromstring(inhoud).iter():
        if el.tag.split("}")[-1] == "metadata" and el.get("name") and el.get("content") is not None:
            uit[el.get("name")].append(el.get("content"))
    return dict(uit)


def publicatie_metadata(pub_id: str) -> dict[str, list[str]]:
    blad, jaar, _ = pub_id.split("-", 2)
    return lees_metadata(haal(PUBLICATIE_METADATA.format(blad=blad, jaar=jaar, id=pub_id)))


def veilig(tekst: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", tekst).strip("_")


def io_bestandsnaam(join: str) -> str:
    """``/join/id/regdata/ws0636/2023/WG_O_ARK/nld@2024-12-02;1004`` -> ``WG_O_ARK_2024-12-02_1004``."""
    werk, expressie = join.rstrip("/").split("/")[-2:]
    return veilig(f"{werk}_{expressie.split('@', 1)[-1].replace(';', '_')}")


def frbr_expressie(inhoud: bytes) -> str | None:
    """The first FRBRExpression in a GIO's GML: which version the file says it is."""
    for _, el in ET.iterparse(io.BytesIO(inhoud), events=("end",)):
        if el.tag.split("}")[-1] == "FRBRExpression":
            return (el.text or "").strip()
    return None


def informatieobject(join: str) -> dict[str, Any]:
    """Where the authentic file of a GIO or PDF informatieobject is, and its title.

    The JOIN identifier redirects to the informatieobject's page on
    zoek.officielebekendmakingen.nl (``dc-<jaar>-<nr>``); that page links the
    authentic file (GML or PDF) in the repository, and the repository's
    metadata must name the same JOIN identifier.
    """
    pagina, landing = haal_met_url(JOIN.format(join=urllib.parse.quote(join, safe="/@;:")))
    m = re.search(r"\b(dc-(\d{4})-\d+)\b", landing)
    if not m:
        raise RuntimeError(f"{join}: geen informatieobject gevonden ({landing})")
    dc, jaar = m.groups()
    basis = DATACOLLECTIE.format(jaar=jaar, dc=dc)
    meta = lees_metadata(haal(basis + "metadata/metadata.xml"))
    if join not in meta.get("IMOP.JOINIdentification", []):
        raise RuntimeError(f"{join}: {dc} is {meta.get('IMOP.JOINIdentification')}")
    links = re.findall(re.escape(basis) + r"(?:gml|bijlage)/[^\"'<>\s]+",
                       pagina.decode("utf-8", "replace"))
    if not links:
        raise RuntimeError(f"{join}: {landing} noemt geen bestand")
    return {"soort": "kaart" if "/gml/" in links[0] else "informatieobject", "id": join,
            "dc": dc, "titel": (meta.get("DC.title") or [""])[0], "url": links[0], "bron": landing}


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

    # bijlagen: kaarten, informatieobjecten, externe bijlagen ─────────

    def bijlagen(self) -> None:
        """Everything the publications carry besides their own text.

        A publication's metadata names what it sets (``steltVast``): the GIOs
        (kaarten, GML) and PDF informatieobjecten such as an inspraakrapport,
        and its externe bijlagen.  The kaarten go into ``kaarten/`` in the
        besluit folder, next to ``RTR/``, in the version the besluit set.  A
        kaart of the ontwerp is only kept when the besluit has none of the
        same GIO or with the same title (identifiers were sometimes renamed
        between ontwerp and besluit); it is then named ``ONTWERP_…``.  Other
        bijlagen go into ``bijlagen/`` under the publication's submap.
        ``bijlagen.json`` says where every file came from.  Within a besluit a
        file is stored once: a bijlage identical to one already there (a
        kennisgeving published twice) is recorded as ``identiek_aan``.  A GIO
        must name the version it was asked for, or nothing is written.  This
        step owns ``kaarten/`` and the ``bijlagen/`` folders.
        """
        for b in self.config.besluiten:
            pad = self.map(b, "bijlagen.json")
            oud: dict[str, Any] = {}
            if os.path.exists(pad):
                with open(pad, encoding="utf-8") as f:
                    oud = json.load(f).get("publicaties", {})
            # 1. What each publication carries.
            plan = []
            for submap, documenten in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief)):
                for doc in documenten:
                    meta = publicatie_metadata(doc["id"])
                    eerder = {x["id"]: x for x in oud.get(doc["id"], {}).get("bijlagen", [])}
                    items = []
                    for join in meta.get("OVERHEIDop.steltVast", []):
                        e = eerder.get(join, {})
                        items.append({k: e[k] for k in ("soort", "id", "dc", "titel", "url", "bron")}
                                     if e.get("url") and e.get("dc") else informatieobject(join))
                    for exb in meta.get("OVERHEIDop.externeBijlage", []):
                        titel, _, exb_id = (s.strip() for s in exb.rpartition("|"))
                        items.append({"soort": "externe bijlage", "id": exb_id, "titel": titel,
                                      "bron": EXTERNE_BIJLAGE.format(exb=exb_id),
                                      "basis": f"{submap}/bijlagen/{exb_id}_{veilig(titel)}"})
                    plan.append((submap, doc, meta, items, eerder))
            # 2. The kaarten the besluit set: an ontwerp kaart among them is not kept.
            werk = lambda join: join.rsplit("/", 1)[0]  # noqa: E731
            vastgesteld = {}
            for submap, _, _, items, _ in plan:
                for x in items:
                    if submap == "Definitief" and x["soort"] == "kaart":
                        vastgesteld.setdefault(werk(x["id"]), x["id"])
                        vastgesteld.setdefault(("titel", x["titel"]), x["id"])
            # 3. Fetch and store.
            publicaties: dict[str, Any] = {}
            opgeslagen: dict[str, str] = {}  # sha256 -> bestand, relative to the besluit folder
            for submap, doc, meta, items, eerder in plan:
                uit = []
                for x in items:
                    if x["soort"] == "kaart":
                        als = vastgesteld.get(werk(x["id"])) or vastgesteld.get(("titel", x["titel"]))
                        if submap == "Ontwerp" and als:
                            uit.append({**x, "bestand": None, "vastgesteld_als": als})
                            continue
                        x["bestand"] = (f"kaarten/{'ONTWERP_' if submap == 'Ontwerp' else ''}"
                                        f"{io_bestandsnaam(x['id'])}.gml")
                    elif x["soort"] == "informatieobject":
                        ext = os.path.splitext(urllib.parse.urlparse(x["url"]).path)[1].lower()
                        x["bestand"] = f"{submap}/bijlagen/{io_bestandsnaam(x['id'])}{ext}"
                    uit.append(self._bewaar(b, x, eerder.get(x["id"]), opgeslagen))
                publicaties[doc["id"]] = {
                    "titel": (meta.get("DC.title") or [doc["titel"]])[0],
                    "gepubliceerd": (meta.get("DCTERMS.available") or [""])[0],
                    "link": PUBLICATIE_HTML.format(id=doc["id"]), "bijlagen": uit}
            bewaard = set(opgeslagen.values())
            # Ontwerp/kaarten and Definitief/kaarten are where earlier versions put them.
            for sub in ("kaarten", "Ontwerp/kaarten", "Definitief/kaarten",
                        "Ontwerp/bijlagen", "Definitief/bijlagen"):
                map_ = self.map(b, *sub.split("/"))
                for naam in sorted(os.listdir(map_)) if os.path.isdir(map_) else []:
                    if f"{sub}/{naam}" not in bewaard:
                        os.remove(os.path.join(map_, naam))
                        log.info("%s: %s/%s niet meer nodig, verwijderd", b.map, sub, naam)
                if os.path.isdir(map_) and not os.listdir(map_):
                    os.rmdir(map_)
            with open(pad, "w", encoding="utf-8") as f:
                json.dump({"toelichting": "Per publicatie de kaarten (GIO's, GML), informatieobjecten "
                                          "en externe bijlagen die ze vaststelt of meestuurt: waar "
                                          "het bestand vandaan komt, en zijn SHA-256. Een kaart van "
                                          "het ontwerp die het besluit ook vaststelt, staat alleen "
                                          "in de versie van het besluit (vastgesteld_als). Een "
                                          "bijlage die gelijk is aan een eerdere staat er één keer "
                                          "(identiek_aan).",
                           "publicaties": publicaties}, f, ensure_ascii=False, indent=2)
            n = sum(len(p["bijlagen"]) for p in publicaties.values())
            log.info("%s: %d bijlagen, %d bestanden", b.map, n, len(bewaard))

    def _bewaar(self, b: Besluit, item: dict[str, Any], eerder: dict[str, Any] | None,
                opgeslagen: dict[str, str]) -> dict[str, Any]:
        """Put *item* at its ``bestand``: from an earlier run if possible, else fetched."""
        item, inhoud = dict(item), None
        basis = item.pop("basis", None)
        if eerder and eerder.get("sha256"):
            doel = item.get("bestand") or eerder["bestand"]
            if eerder.get("identiek_aan"):
                if opgeslagen.get(eerder["sha256"]) == eerder["identiek_aan"]:
                    item.update(bestand=doel, bytes=eerder["bytes"], sha256=eerder["sha256"])
            else:
                for kandidaat in dict.fromkeys((doel, eerder["bestand"])):
                    pad = self.map(b, *kandidaat.split("/"))
                    if os.path.exists(pad) and os.path.getsize(pad) == eerder["bytes"] \
                            and sha256(pad) == eerder["sha256"]:
                        if kandidaat != doel:  # an earlier version kept it elsewhere
                            os.makedirs(os.path.dirname(self.map(b, *doel.split("/"))), exist_ok=True)
                            os.replace(pad, self.map(b, *doel.split("/")))
                            log.info("%s: %s verplaatst naar %s", b.map, kandidaat, doel)
                        item.update(bestand=doel, bytes=eerder["bytes"], sha256=eerder["sha256"])
                        break
        if "sha256" not in item:
            inhoud, url = haal_met_url(item.get("url") or item["bron"])
            ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
            item["url"] = url
            item.setdefault("bestand", f"{basis}{ext}")
            if item["soort"] == "kaart" and frbr_expressie(inhoud) != item["id"]:
                raise RuntimeError(f"{url} is {frbr_expressie(inhoud)}, niet {item['id']}")
            if ext == ".pdf" and not inhoud.startswith(b"%PDF"):
                raise RuntimeError(f"{url}: geen PDF ontvangen")
            item.update(bytes=len(inhoud), sha256=hashlib.sha256(inhoud).hexdigest())
        item.pop("identiek_aan", None)
        if item["sha256"] in opgeslagen and opgeslagen[item["sha256"]] != item["bestand"]:
            item["identiek_aan"] = opgeslagen[item["sha256"]]
        else:
            opgeslagen[item["sha256"]] = item["bestand"]
            if inhoud is not None:
                schrijf_atomair(self.map(b, *item["bestand"].split("/")), inhoud)
                log.info("%s: %s (%s, %.1f MB)", b.map, item["bestand"], item["titel"], len(inhoud) / 1e6)
        return item

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
        # The "<date>_gewijzigd" folders are derived from the snapshots and the
        # history, so this step owns them: one it no longer plans goes.
        for b in self.config.besluiten:
            rtr_map = self.map(b, "RTR")
            for naam in sorted(os.listdir(rtr_map)) if os.path.isdir(rtr_map) else []:
                pad = os.path.join(rtr_map, naam)
                if GEWIJZIGD.match(naam) and os.path.isdir(pad) and pad not in per_map:
                    shutil.rmtree(pad)
                    log.info("%s/RTR/%s: niet meer nodig, verwijderd", b.map, naam)

        for map_pad, items in sorted(per_map.items()):
            sttr_map = os.path.join(map_pad, "STTR")
            os.makedirs(sttr_map, exist_ok=True)
            gepland = {f"STTR_{v['sttrId']}_{v['activiteit'].split('.')[-1]}_"
                       f"{v['soort'].replace(' ', '_')}.xml" for v in items if not v.get("vervallen")}
            for naam in os.listdir(sttr_map):
                if naam.endswith(".xml") and naam not in gepland:
                    os.remove(os.path.join(sttr_map, naam))
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
        nakijken = {b.map: self._readme_txt(b, mappen, [v for v in weg if self.config.besluit_voor(
            date.fromisoformat(v["eerst_weg"])).map == b.map]) for b in self.config.besluiten}
        self._readme(mappen, len(alle), len(alle) - len(ontbreekt), ontbreekt, weg, nakijken)
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

    def _readme_txt(self, b: Besluit, mappen: dict[str, dict[str, Any]],
                    weg: list[dict[str, Any]]) -> int:
        """README.txt of a besluit folder; returns the number of '!' lines.

        Section by section what is in the folder, and with a '!' in front
        what belongs to the besluit but is not there (VERWACHT, DM numbers).
        A file put in the folder by hand is listed where its name fits an
        expected document, otherwise under "overige bestanden".
        """
        T = "\t"
        bijl: dict[str, Any] = {}
        if os.path.exists(self.map(b, "bijlagen.json")):
            with open(self.map(b, "bijlagen.json"), encoding="utf-8") as f:
                bijl = json.load(f).get("publicaties", {})
        alle = []  # every file outside RTR/, relative to the besluit folder
        for wortel, mapnamen, bestanden in os.walk(self.map(b)):
            rel = os.path.relpath(wortel, self.map(b)).replace(os.sep, "/")
            if rel.split("/")[0] == "RTR":
                mapnamen[:] = []
                continue
            mapnamen.sort()
            alle += [naam if rel == "." else f"{rel}/{naam}" for naam in sorted(bestanden)
                     if not naam.endswith(".tmp") and naam != "LEESMIJ.md"]
        genoemd = {"README.txt"}
        uitroep = 0
        secties: list[tuple[str, list[str]]] = []

        def sectie(kop: str) -> list[str]:
            secties.append((kop, []))
            return secties[-1][1]

        def bestand(regels: list[str], rel: str, *extra: str) -> None:
            genoemd.add(rel)
            regels.append(T * 2 + rel)
            regels += [T * 2 + e for e in extra if e]

        def ontbreekt(regels: list[str], tekst: str, inspringen: int = 2) -> None:
            nonlocal uitroep
            uitroep += 1
            regels.append("!" + T * inspringen + tekst)

        def verwacht(regels: list[str], sleutel: str) -> None:
            _, fase, _, omschrijving, patroon, alleen_wijziging = next(v for v in VERWACHT if v[0] == sleutel)
            if alleen_wijziging and b.nieuw:
                return
            naam = b.intern.get(sleutel, "")
            gevonden = [r for r in alle if r not in genoemd and (not fase or r.startswith(fase + "/"))
                        and (fnmatch(os.path.basename(r).lower(), patroon)
                             or os.path.basename(r) == naam)]
            for r in gevonden:
                bestand(regels, r)
            if not gevonden:
                ontbreekt(regels, f"{naam}  ({omschrijving})" if naam else omschrijving)

        def publicatie(regels: list[str], submap: str, doc: dict[str, str]) -> None:
            p = bijl.get(doc["id"], {})
            datum = (f", Waterschapsblad {date.fromisoformat(p['gepubliceerd']):%d-%m-%Y}"
                     if p.get("gepubliceerd") else "")
            rel = f"{submap}/{doc['id']}_{doc['soort']}.pdf"
            if rel in alle:
                bestand(regels, rel, doc["titel"] + datum)
            else:
                ontbreekt(regels, f"{rel}: niet gedownload ({doc['titel']})")
            if doc.get("opmerking"):
                regels.append(T * 2 + f"Opmerking: {doc['opmerking']}")
            regels.append(T * 2 + f"Link: {PUBLICATIE_HTML.format(id=doc['id'])}")

        def bijlagen_van(docs: list[dict[str, str]], soort: str) -> list[dict[str, Any]]:
            return [dict(x, publicatie=d["id"]) for d in docs
                    for x in bijl.get(d["id"], {}).get("bijlagen", []) if (x["soort"] == "kaart") == (soort == "kaart")]

        def kaarten(regels: list[str], items: list[dict[str, Any]]) -> None:
            for x in items:
                if not x.get("bestand"):  # an ontwerp kaart the besluit set: kept in that version
                    continue
                if x["bestand"] in alle:
                    alleen = ", alleen in het ontwerp" if x["bestand"].startswith("kaarten/ONTWERP_") else ""
                    bestand(regels, x["bestand"] + (f"  ({x.get('titel', '')}{alleen})"
                                                    if x.get("titel") or alleen else ""))
                    genoemd.add(x["bestand"])
                else:
                    ontbreekt(regels, f"{x['bestand']}: niet gedownload ({x['id']})")

        def overige_bijlagen(regels: list[str], items: list[dict[str, Any]]) -> None:
            for x in items:
                if x["bestand"] in genoemd:
                    continue
                if x.get("identiek_aan"):
                    regels.append(T * 2 + f"{x['id']} ({x['titel']}) bij {x['publicatie']}: "
                                  f"identiek aan {x['identiek_aan']}, niet nog eens opgeslagen")
                elif x["bestand"] in alle:
                    bestand(regels, x["bestand"], f"{x['titel']} ({x['soort']} bij {x['publicatie']})")
                else:
                    ontbreekt(regels, f"{x['bestand']}: niet gedownload ({x['id']})")

        ontwerp = [d for d in b.ontwerp if d["soort"] != "kennisgeving"]
        kennisgevingen = [d for d in b.ontwerp if d["soort"] == "kennisgeving"]
        besluit = [d for d in b.definitief if d["soort"] != "kennisgeving"]

        r = sectie("readme")
        r.append(T * 2 + "README.txt")
        if "bijlagen.json" in alle:
            bestand(r, "bijlagen.json")
        verwacht(r, "mutaties")

        if b.ontwerp:
            r = sectie("ontwerp")
            for d in ontwerp:
                publicatie(r, "Ontwerp", d)
            r = sectie("kennisgeving terinzagelegging")
            for d in kennisgevingen:
                publicatie(r, "Ontwerp", d)
            verwacht(r, "ontwerp.kennisgeving")
            if not b.nieuw:
                verwacht(sectie("renvooi ontwerp"), "ontwerp.renvooi")
            r = sectie("Match ontwerp")
            for sleutel in ("ontwerp.kb", "ontwerp.zip", "ontwerp.geconsolideerd"):
                verwacht(r, sleutel)
            if bijlagen_van(b.ontwerp, "bijlage"):
                overige_bijlagen(sectie("bijlagen ontwerp"), bijlagen_van(b.ontwerp, "bijlage"))
        # Only when there is one: a besluit without a published inspraakrapport
        # does not mention it.
        r = sectie("inspraakrapport")
        for rel in alle:
            if rel not in genoemd and fnmatch(os.path.basename(rel).lower(), "*inspraakrapport*"):
                bestand(r, rel)
        r = sectie("besluit")
        for d in besluit:
            publicatie(r, "Definitief", d)
        if len(besluit) < len(b.definitief):
            r = sectie("kennisgeving besluit")
            for d in b.definitief:
                if d["soort"] == "kennisgeving":
                    publicatie(r, "Definitief", d)
        r = sectie("Match besluit")
        for sleutel in ("besluit.kb", "besluit.zip"):
            verwacht(r, sleutel)
        kaarten(sectie("kaarten" if b.nieuw else "gewijzigde kaarten"),
                bijlagen_van(b.definitief, "kaart") + bijlagen_van(b.ontwerp, "kaart"))
        if any(x["bestand"] not in genoemd for x in bijlagen_van(b.definitief, "bijlage")):
            overige_bijlagen(sectie("bijlagen besluit"), bijlagen_van(b.definitief, "bijlage"))
        r = sectie("hele regeling")
        for t in b.toestanden:
            vanaf = date.fromisoformat(t["geldig_vanaf"])
            rel = f"Definitief/regeling_toestand-{t['nummer']}_geldend-vanaf-{vanaf:%Y%m%d}.pdf"
            if rel in alle:
                bestand(r, rel, f"de hele regeling zoals die gold vanaf {vanaf:%d-%m-%Y}")
            else:
                ontbreekt(r, f"{rel}: niet afgedrukt")
            r.append(T * 2 + "Link: " + CVDR_TOESTAND.format(regeling=self.config.regeling["cvdr"],
                                                            toestand=t["nummer"]))
        r = sectie("toepasbare regels (RTR)")
        for rel in sorted(k for k in mappen if k.split(os.sep)[0] == b.map):
            m, naam = mappen[rel], os.path.basename(rel)
            dag = f"{naam[6:8]}-{naam[4:6]}-{naam[:4]}"
            n = sum(1 for s in m["sttrs"] if not s.get("vervallen"))
            vervallen = sum(1 for s in m["sttrs"] if s.get("vervallen"))
            r.append(T * 2 + f"RTR/{naam}: " + (
                f"volledige vastlegging op {dag}, {n} STTR-bestanden, status.xlsx, werkingsgebieden.txt"
                if naam.endswith("_volledig") else f"gewijzigd op {dag}, {n} STTR-bestanden")
                + (f", {vervallen} vervallen" if vervallen else ""))
        if not r:
            ontbreekt(r, "RTR/: geen toepasbare regels vastgelegd")
        overig = [x for x in alle if x not in genoemd]
        if overig:
            r = sectie("overige bestanden")
            for x in overig:
                bestand(r, x)
        if weg:
            r = sectie("uit de RTR verdwenen")
            for v in weg:
                wat = v["soort"] + (f" ({v['label']})" if v.get("label") else "")
                r.append(T * 2 + f"{wat}: {v['omschrijving']} ({v['id'].split('/')[-1]}), laatst gezien "
                         f"{date.fromisoformat(v['laatst_gezien']):%d-%m-%Y}, weg op "
                         f"{date.fromisoformat(v['eerst_weg']):%d-%m-%Y}")

        dm = []
        for x in self.config.regeling.get("dm", []) + b.dm:
            dm.append(T + f"{x['omschrijving']}: DM {x['nummer']}")
        fasen = {x.get("fase") for x in b.dm}
        for fase, tekst in (("ontwerp", "Bestuursstukken ontwerp"), ("besluit", "Bestuursstukken besluit")):
            if fase not in fasen and (fase == "besluit" or b.ontwerp):
                ontbreekt(dm, f"{tekst}: DM-nummer", inspringen=1)

        bekendmaking = ", ".join(d["id"] for d in besluit)
        kop = [self.config.regeling["titel"],
               f"Besluit van {b.besluitdatum:%d-%m-%Y}, bekendmaking {bekendmaking}",
               f"In werking {b.in_werking:%d-%m-%Y}" + (f" tot {b.tot:%d-%m-%Y}" if b.tot else "")
               + f"; toestand {', '.join(str(t['nummer']) for t in b.toestanden)} van "
               f"{self.config.regeling['cvdr']}", ""]
        if b.opmerking:
            kop += [b.opmerking, ""]
        kop += [f"Samengesteld op {date.today():%d-%m-%Y} met code/archief.py.",
                "Een '!' vooraan een regel: dit hoort bij het besluit maar zit niet in deze map. "
                f"Nakijken: {uitroep} regel(s).", ""]
        regels = kop + ["Bestanden", ""]
        for naam, inhoud in secties:
            if inhoud:  # what was not published is not mentioned
                regels += [T + naam] + inhoud + [""]
        regels += ["DM"] + dm + [""]
        regels += ["Toelichting"] + [textwrap.fill(p, 100, initial_indent=T, subsequent_indent=T) + "\n"
                                     for p in (
            "Publicaties komen uit het Waterschapsblad (zoek.officielebekendmakingen.nl), de hele "
            "regeling per toestand uit lokaleregelgeving.overheid.nl. Kaarten zijn de authentieke "
            "GML van de werkingsgebieden (GIO's) die een publicatie vaststelt; bijlagen zijn de "
            "informatieobjecten (PDF) en externe bijlagen die ze meestuurt. bijlagen.json noemt per "
            "bestand de bron en SHA-256.",
            "Een volledige vastlegging bevat alle toepasbare regels die op die dag golden (status.xlsx "
            "met per soort de datum waarop de versie inging, werkingsgebieden.txt, en STTR/). Een map "
            "..._gewijzigd bevat alleen de versies die op die dag ingingen en in geen volledige "
            "vastlegging van dit besluit staan; STTR/manifest.json noemt per bestand welke versie het "
            "vervangt en waardoor het werd vervangen.",
            "Uit de RTR verdwenen: de RTR haalt activiteiten en regelbeheerobjecten uit de boom zonder "
            "hun STTR-versies te beëindigen. Hun bestanden staan in dit archief; de lijst zegt wanneer "
            "ze verdwenen (tussen twee rondes door de boom).")]
        with open(self.map(b, "README.txt"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(regels))
        leesmij = self.map(b, "LEESMIJ.md")
        if os.path.exists(leesmij):
            os.remove(leesmij)
        return uitroep

    def _readme(self, mappen, totaal: int, gearchiveerd: int, ontbreekt: list[str],
                weg: list[dict[str, Any]], nakijken: dict[str, int]) -> None:
        bo = self.config.bestuursorgaan
        regels = [f"# Archief {self.config.regeling['titel']}", "",
                  f"{bo.naam} (OIN {bo.oin}), regeling {self.config.regeling['cvdr']}. "
                  f"Samengesteld op {date.today():%d-%m-%Y} met `code/archief.py`.", "",
                  "Per definitief besluit één map, genoemd naar de besluitdatum. Daarin het ontwerp "
                  "en de kennisgevingen, het besluit zelf, de kaarten (GML) en bijlagen die ze "
                  "vaststellen of meesturen, de hele regeling zoals die vanaf de inwerkingtreding "
                  "gold, en de toepasbare regels: volledige vastleggingen op de gekozen peildata en "
                  "een map per dag waarop later nog iets veranderde. Het `README.txt` in elke map "
                  "noemt wat erin zit, en met een `!` vooraan wat bij het besluit hoort maar er niet "
                  "in zit (interne bestanden, DM-nummers); de kolom Nakijken telt die regels.", "",
                  "| Besluit | Map | In werking | Ontwerp | Definitief | Kaarten | Bijlagen "
                  "| Vastleggingen | Gewijzigd op | Nakijken |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for b in self.config.besluiten:
            eigen = sorted(os.path.basename(k) for k in mappen if k.split(os.sep)[0] == b.map)
            vast = ", ".join(f"{n[6:8]}-{n[4:6]}-{n[:4]}" for n in eigen if n.endswith("_volledig"))
            gew = sum(1 for n in eigen if n.endswith("_gewijzigd"))
            tel = {soort: sum(len(os.listdir(p)) for p in paden if os.path.isdir(p)) for soort, paden in (
                ("kaarten", [self.map(b, "kaarten")]),
                ("bijlagen", [self.map(b, "Ontwerp", "bijlagen"), self.map(b, "Definitief", "bijlagen")]))}
            regels.append(
                f"| {b.besluitdatum:%d-%m-%Y} | `{b.map}` | {b.in_werking:%d-%m-%Y} | "
                f"{len(b.ontwerp)} | {len(b.definitief)} + {len(b.toestanden)} regeling | "
                f"{tel['kaarten']} | {tel['bijlagen']} | {vast or '-'} | {gew} dag(en) | "
                f"{nakijken.get(b.map, 0)} |")
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
                   "regelbeheerobject(en) verdwenen in die periode uit de RTR; het `README.txt` "
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
            if str(v["sttrId"]) in in_vastlegging.get(b.map, set()):
                continue
            uit[(b.map, f"{begin:%Y%m%d}_gewijzigd")].append({
                **v, "sttrId": str(v["sttrId"]), "ref": ref, "activiteit": r["activiteit"], "soort": r["label"],
                "omschrijving": omschrijving(r["activiteit"]),
                "vervangt": str(versies[i - 1]["sttrId"]) if i else None,
                "vervangen_door": str(versies[i + 1]["sttrId"]) if i + 1 < len(versies) else None})
        if versies and versies[-1].get("einddatum"):
            weg = nl_datum(versies[-1]["einddatum"]) + timedelta(days=1)
            if weg <= tot:
                b = config.besluit_voor(weg)
                uit[(b.map, f"{weg:%Y%m%d}_gewijzigd")].append({
                    "vervallen": True, "ref": ref, "activiteit": r["activiteit"],
                    "soort": r["label"], "omschrijving": omschrijving(r["activiteit"]),
                    "laatste_versie": str(versies[-1]["sttrId"])})
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
    if "bijlagen" in stappen:
        archief.bijlagen()
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

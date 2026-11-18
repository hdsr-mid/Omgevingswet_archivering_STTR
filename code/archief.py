"""
Archive a regeling per definitief besluit: its publications, the whole
regeling, and every version of the toepasbare regels (STTR) under it.

    python code/archief.py                     (data/archief_CVDR707248.json, every step)
    python code/archief.py data/archief_CVDR707248.json --stappen publicaties,bijlagen,regeling
    python code/archief.py data/archief_CVDR707248.json --stappen rtr,wijzigingen,index

The first step, zoeken (code/zoeken.py), adds the regeling's new publications
to the JSON file.

One folder per definitief besluit, named after its besluitdatum:

    archief/
      README.md  index.csv (every file with its SHA-256)
      20250715_wijzigingsbesluit-h2-3-4_wsb-2025-17997/
        README.txt   what is in the folder, and with a '!' what is expected but not
        bijlagen.json  where every kaart and bijlage came from
        Ontwerp_20250514/     wsb-2025-11894_ontwerp.pdf  wsb-2025-12517_kennisgeving.pdf
                              bijlagen/  informatieobjecten (PDF) and externe bijlagen
        Definitief_20250724/  wsb-2025-17997_besluit.pdf  bijlagen/
                              regeling_toestand-5_geldend-vanaf-20250724.pdf
                     (each named after the day it became visible in the DSO: zichtbaar_in_dso)
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

Each run adds to the archive.  A besluit that is complete (see
``Archief.compleet``) is not fetched again; it only gets new
``<date>_gewijzigd`` folders while its period is open.  A besluit with only
an ontwerp gets a folder named after the ontwerp, renamed when the besluit
comes.  README.txt, README.md and index.csv are written again only when
something changed.
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
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import catalogus
import vertrouwen
import zoeken
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
STAPPEN = ("zoeken", "publicaties", "bijlagen", "regeling", "rtr", "wijzigingen", "index")
GEWIJZIGD = re.compile(r"^\d{8}_gewijzigd$")
# The blad a decentralised government publishes in, by the prefix of the publication id.
BLADEN = {"wsb": "Waterschapsblad", "prb": "Provinciaal blad", "gmb": "Gemeenteblad"}
RIJKSBLADEN = {"stcrt": "Staatscourant", "stb": "Staatsblad"}


class ConfigFout(ValueError):
    """The config file cannot be used (exit code 2)."""


def blad(pub_id: str) -> str:
    """``wsb-2025-17997`` -> ``Waterschapsblad``."""
    voorvoegsel = pub_id.split("-", 1)[0]
    if voorvoegsel in RIJKSBLADEN:
        raise catalogus.RijkNietOndersteund(
            f"{pub_id} staat in de {RIJKSBLADEN[voorvoegsel]}: regelingen van het Rijk worden "
            "niet ondersteund, alleen die van waterschappen, provincies en gemeenten")
    if voorvoegsel not in BLADEN:
        raise ConfigFout(f"{pub_id}: onbekend blad '{voorvoegsel}' (bekend: {', '.join(BLADEN)})")
    return BLADEN[voorvoegsel]


# ── configuration ────────────────────────────────────────────────────


@dataclass
class Besluit:
    """One besluit, from its ontwerp to its STTR.

    It can be incomplete: an ontwerp without a besluit yet (no
    ``besluitdatum``, no ``definitief``), or a besluit whose in-werking date
    is not known yet (the toestand is not in CVDR).  Only a besluit in werking
    has a period, and with it toepasbare regels.
    """

    besluitdatum: date | None
    naam: str
    in_werking: date | None
    ontwerp: list[dict[str, str]]
    definitief: list[dict[str, str]]
    toestanden: list[dict[str, Any]]
    peildata: list[date]
    opmerking: str = ""
    # The day the ontwerp and the besluit became visible in the DSO (their bekendmaking).
    zichtbaar: dict[str, date] = field(default_factory=dict)  # {"ontwerp": …, "besluit": …}
    tot: date | None = None  # next besluit's in-werking date (exclusive)

    @property
    def map(self) -> str:
        """``<besluitdatum>_<naam>_<besluit>``; before the besluit ``<ontwerp zichtbaar>_<naam>_<ontwerp>``."""
        if self.definitief:
            besluit = next((d["id"] for d in self.definitief if d["soort"] == "besluit"),
                           self.definitief[0]["id"])
            return f"{self.besluitdatum:%Y%m%d}_{self.naam}_{besluit}"
        ontwerp = next((d["id"] for d in self.ontwerp if d["soort"] == "ontwerp"), self.ontwerp[0]["id"])
        return f"{self.zichtbaar['ontwerp']:%Y%m%d}_{self.naam}_{ontwerp}"

    def submap(self, fase: str) -> str:
        """``Ontwerp_<jjjjmmdd>`` or ``Definitief_<jjjjmmdd>``: the day it became visible in the DSO."""
        datum = self.zichtbaar.get("ontwerp" if fase == "Ontwerp" else "besluit")
        return f"{fase}_{datum:%Y%m%d}" if datum else fase

    @property
    def nieuw(self) -> bool:
        """Sets up the regeling (its first toestand) rather than changing it."""
        return any(t["nummer"] == 1 for t in self.toestanden)

    @property
    def publicaties(self) -> list[dict[str, str]]:
        return self.ontwerp + self.definitief

    def bevat(self, d: date) -> bool:
        return self.in_werking is not None and self.in_werking <= d and (self.tot is None or d < self.tot)


@dataclass
class Config:
    pad: str
    bestuursorgaan: Bestuursorgaan
    regeling: dict[str, str]
    omgeving: str
    besluiten: list[Besluit] = field(default_factory=list)

    @property
    def in_werking(self) -> list[Besluit]:
        """The besluiten in werking, by date: the ones with a period and STTR."""
        return [b for b in self.besluiten if b.in_werking is not None]

    def besluit_voor(self, d: date) -> Besluit | None:
        """The besluit in whose period *d* falls (the first for anything earlier); None before any."""
        for b in self.in_werking:
            if b.bevat(d):
                return b
        return next(iter(self.in_werking), None)


def lees_config(pad: str) -> Config:
    """The config file, checked: ``ConfigFout`` when it cannot be used, ``RijkNietOndersteund``
    when it is about a national regeling."""
    try:
        with open(pad, encoding="utf-8") as f:
            ruw = json.load(f)
        return _lees_config(pad, ruw)
    except (ConfigFout, catalogus.RijkNietOndersteund):
        raise
    except json.JSONDecodeError as exc:
        raise ConfigFout(f"{pad}: geen geldige JSON ({exc})") from exc
    except KeyError as exc:
        raise ConfigFout(f"{pad}: '{exc.args[0]}' ontbreekt") from exc
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigFout(f"{pad}: {exc}") from exc


def _lees_config(pad: str, ruw: dict[str, Any]) -> Config:
    bo = ruw["bestuursorgaan"]
    regeling = ruw["regeling"]
    if "bwb" in regeling or any(str(v).upper().startswith("BWB") for v in regeling.values()):
        raise catalogus.RijkNietOndersteund(
            f"{pad}: een regeling uit het BWB is van het Rijk; alleen regelingen in CVDR "
            "(waterschappen, provincies en gemeenten) worden ondersteund")
    if not re.fullmatch(r"CVDR\d+", regeling.get("cvdr", "")):
        raise ConfigFout(f"{pad}: regeling.cvdr moet een CVDR-nummer zijn (CVDR…), "
                         f"niet {regeling.get('cvdr')!r}")
    datum = lambda b, k: date.fromisoformat(b[k]) if b.get(k) else None  # noqa: E731
    besluiten = [Besluit(
        besluitdatum=datum(b, "besluitdatum"), naam=b["naam"], in_werking=datum(b, "in_werking"),
        ontwerp=b.get("ontwerp", []), definitief=b.get("definitief", []),
        toestanden=b.get("toestanden", []),
        peildata=[date.fromisoformat(d) for d in b.get("rtr_peildata", [])],
        opmerking=b.get("opmerking", ""),
        zichtbaar={k: date.fromisoformat(v) for k, v in b.get("zichtbaar_in_dso", {}).items()})
        for b in ruw["besluiten"]]
    for nr, b in enumerate(besluiten, 1):
        wie = f"besluit {nr} ({b.naam})"
        if not b.publicaties:
            raise ConfigFout(f"{wie}: geen ontwerp en geen definitief")
        if b.definitief and not b.besluitdatum:
            raise ConfigFout(f"{wie}: besluitdatum ontbreekt")
        if b.in_werking and not b.definitief:
            raise ConfigFout(f"{wie}: in_werking zonder definitief besluit")
        if (b.toestanden or b.peildata) and not b.in_werking:
            raise ConfigFout(f"{wie}: toestanden of rtr_peildata, maar in_werking ontbreekt")
        for fase, docs in (("ontwerp", b.ontwerp), ("besluit", b.definitief)):
            for doc in docs:
                blad(doc["id"])
            if docs and fase not in b.zichtbaar:
                raise ConfigFout(f"{wie}: zichtbaar_in_dso.{fase} ontbreekt (de dag waarop het "
                                 f"{fase} zichtbaar werd in het DSO; de map wordt ernaar genoemd)")
    # In werking by date, then the rest: a besluit not in werking yet has no period.
    besluiten.sort(key=lambda b: (b.in_werking is None, b.in_werking or date.min,
                                  b.besluitdatum or b.zichtbaar.get("ontwerp") or date.min))
    gaand = [b for b in besluiten if b.in_werking]
    for huidig, volgend in zip(gaand, gaand[1:]):
        huidig.tot = volgend.in_werking
    for b in besluiten:
        buiten = [d for d in b.peildata if not b.bevat(d)]
        if buiten:
            raise ConfigFout(f"{b.map}: peildata {buiten} vallen buiten de periode "
                             f"van het besluit ({b.in_werking} tot {b.tot})")
    # root (one) or roots (a list); neither: found from the OIN when the RTR is needed.
    roots = bo.get("roots", [])
    roots = [roots] if isinstance(roots, str) else list(roots)
    if bo.get("root"):
        roots.insert(0, bo["root"])
    catalogus.niet_rijk(bo["oin"], roots)
    omgeving = ruw.get("omgeving", "prod")
    if omgeving not in BASE_URLS:
        raise ConfigFout(f"{pad}: omgeving moet {' of '.join(BASE_URLS)} zijn, niet {omgeving!r}")
    return Config(pad, Bestuursorgaan(bo["naam"], bo["oin"], tuple(dict.fromkeys(roots))),
                  regeling, omgeving, besluiten)


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


def documenten_van(b: Besluit, submap: str) -> list[dict[str, str]]:
    return b.ontwerp if submap == "Ontwerp" else b.definitief


def werknaam(config_pad: str) -> str:
    """``data/archief_CVDR707248.json`` -> ``archief_CVDR707248``: names the tool's own files."""
    return os.path.splitext(os.path.basename(config_pad))[0]


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
    def __init__(self, config: Config, uit: str, client: ArchiefClient, werkmap: str | None = None):
        self.config = config
        self.uit = uit
        self.client = client
        # The tool's own state (the version history) stays out of the archive.
        self.werkmap = werkmap or uit
        # Besluit folders that were complete when the run began: fetched once, never redone.
        self.af: set[str] = set()
        # Besluit folders that got new content in this run ("." for the archive itself).
        self.gewijzigd: set[str] = set()

    def voorbereiden(self) -> None:
        """Rename what an earlier run named otherwise, then note which besluiten are complete."""
        self.hernoem_mappen()
        self.af = {b.map for b in self.config.besluiten if self.compleet(b)}
        for b in self.config.besluiten:
            log.info("%s: %s", b.map, "compleet, wordt overgeslagen" if b.map in self.af else "aanvullen")

    def compleet(self, b: Besluit) -> bool:
        """Everything of the besluit is there, so nothing is fetched again.

        The besluit is in werking, every publication is a PDF in its folder,
        ``bijlagen.json`` names every publication and every kaart and bijlage
        it lists is there (by size), every toestand is printed, and every
        peildatum has a full RTR snapshot.
        """
        if not (b.definitief and b.in_werking and b.toestanden and b.peildata):
            return False
        for submap, docs in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief)):
            if not all(self._is_pdf(self.map(b, b.submap(submap), f"{d['id']}_{d['soort']}.pdf"))
                       for d in docs):
                return False
        try:
            with open(self.map(b, "bijlagen.json"), encoding="utf-8") as f:
                publicaties = json.load(f)["publicaties"]
        except (OSError, ValueError, KeyError):
            return False
        for d in b.publicaties:
            if d["id"] not in publicaties:
                return False
            for x in publicaties[d["id"]].get("bijlagen", []):
                if x.get("bestand") and not x.get("identiek_aan"):
                    pad = self.map(b, *x["bestand"].split("/"))
                    if not os.path.isfile(pad) or os.path.getsize(pad) != x.get("bytes"):
                        return False
        for t in b.toestanden:
            if not self._is_pdf(self._toestand_pad(b, t)):
                return False
        return all(os.path.exists(self.map(b, "RTR", f"{d:%Y%m%d}_volledig", "STTR", "manifest.json"))
                   for d in b.peildata)

    def _toestand_pad(self, b: Besluit, t: dict[str, Any]) -> str:
        vanaf = date.fromisoformat(t["geldig_vanaf"])
        return self.map(b, b.submap("Definitief"),
                        f"regeling_toestand-{t['nummer']}_geldend-vanaf-{vanaf:%Y%m%d}.pdf")

    def te_doen(self) -> list[Besluit]:
        """The besluiten that were not complete when the run began."""
        return [b for b in self.config.besluiten if b.map not in self.af]

    @property
    def historiepad(self) -> str:
        return os.path.join(self.werkmap, werknaam(self.config.pad) + "_versiehistorie.json")

    def map(self, besluit: Besluit, *delen: str) -> str:
        return os.path.join(self.uit, besluit.map, *delen)

    def vind_roots(self, tot: date) -> None:
        """The roots of the bestuursorgaan's activity tree, from its OIN, when the config names none.

        Asked on every in-werking date up to *tot* and on *tot* itself, so a
        tree that was started anew later is walked as well.
        """
        bo = self.config.bestuursorgaan
        if bo.roots:
            return
        roots: dict[str, None] = {}
        for d in sorted({b.in_werking for b in self.config.in_werking if b.in_werking <= tot} | {tot}):
            try:
                roots.update(dict.fromkeys(catalogus.zoek_op_oin(self.client, bo.oin, d.isoformat()).roots))
            except catalogus.CatalogusFout as exc:
                log.info("%s", exc)
        if not roots:
            raise ConfigFout(f"{self.config.pad}: geen activiteitenboom in de RTR voor OIN {bo.oin}; "
                             "klopt het OIN, of zet 'root' in het JSON-bestand")
        self.config.bestuursorgaan = Bestuursorgaan(bo.naam, bo.oin, tuple(roots))
        log.info("%s: root(s) %s", bo.naam, ", ".join(roots))

    def hernoem_mappen(self) -> None:
        """Give folders of an earlier run their current name.

        A besluit folder that was named after the ontwerp, before the besluit
        was there, is found by the id it ends with (``…_<id>``, any of the
        besluit's publications) and gets the besluit's name.  ``Ontwerp`` and
        ``Definitief`` of an earlier archive have no date or another one.
        Folders move as a whole, and the paths in ``bijlagen.json`` (relative
        to the besluit folder) follow, so nothing has to be fetched again.
        """
        namen = {b.map for b in self.config.besluiten}
        bestaand = sorted(n for n in os.listdir(self.uit) if os.path.isdir(os.path.join(self.uit, n))) \
            if os.path.isdir(self.uit) else []
        for b in self.config.besluiten:
            if os.path.exists(self.map(b)):
                continue
            kandidaten = [n for n in bestaand if n not in namen
                          and any(n.endswith(f"_{d['id']}") for d in b.publicaties)]
            if len(kandidaten) == 1:
                os.replace(os.path.join(self.uit, kandidaten[0]), self.map(b))
                bestaand.remove(kandidaten[0])
                self.gewijzigd.add(b.map)
                log.info("%s/ heet nu %s/", kandidaten[0], b.map)
            elif kandidaten:
                log.warning("%s: niet hernoemd, meerdere mappen passen: %s", b.map, ", ".join(kandidaten))
        for b in self.config.besluiten:
            hernoemd = {}
            wortel = self.map(b)
            for fase in ("Ontwerp", "Definitief"):
                nieuw = b.submap(fase)
                oude = [n for n in (sorted(os.listdir(wortel)) if os.path.isdir(wortel) else [])
                        if re.fullmatch(rf"{fase}(_\d{{8}})?", n) and n != nieuw
                        and os.path.isdir(os.path.join(wortel, n))]
                if len(oude) == 1 and not os.path.exists(os.path.join(wortel, nieuw)):
                    os.replace(os.path.join(wortel, oude[0]), os.path.join(wortel, nieuw))
                    hernoemd[oude[0] + "/"] = nieuw + "/"
                    self.gewijzigd.add(b.map)
                    log.info("%s: %s/ heet nu %s/", b.map, oude[0], nieuw)
            pad = self.map(b, "bijlagen.json")
            if hernoemd and os.path.exists(pad):
                with open(pad, encoding="utf-8") as f:
                    data = json.load(f)
                for p in data.get("publicaties", {}).values():
                    for x in p.get("bijlagen", []):
                        for sleutel in ("bestand", "identiek_aan"):
                            for van, naar in hernoemd.items():
                                if (x.get(sleutel) or "").startswith(van):
                                    x[sleutel] = naar + x[sleutel][len(van):]
                with open(pad, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    # publicaties ─────────────────────────────────────────────────────

    def publicaties(self) -> None:
        for b in self.te_doen():
            for submap, documenten in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief)):
                for doc in documenten:
                    pad = self.map(b, b.submap(submap), f"{doc['id']}_{doc['soort']}.pdf")
                    if self._is_pdf(pad):
                        continue
                    inhoud = haal(PUBLICATIE_PDF.format(id=doc["id"]))
                    if not inhoud.startswith(b"%PDF"):
                        raise RuntimeError(f"{doc['id']}: geen PDF ontvangen")
                    schrijf_atomair(pad, inhoud)
                    self.gewijzigd.add(b.map)
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
        for b in self.te_doen():
            pad = self.map(b, "bijlagen.json")
            oud: dict[str, Any] = {}
            vorige = b""
            if os.path.exists(pad):
                with open(pad, "rb") as f:
                    vorige = f.read()
                oud = json.loads(vorige).get("publicaties", {})
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
                                      "basis": f"{b.submap(submap)}/bijlagen/{exb_id}_{veilig(titel)}"})
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
                        x["bestand"] = f"{b.submap(submap)}/bijlagen/{io_bestandsnaam(x['id'])}{ext}"
                    uit.append(self._bewaar(b, x, eerder.get(x["id"]), opgeslagen))
                gepubliceerd = (meta.get("DCTERMS.available") or [""])[0]
                fase = "ontwerp" if submap == "Ontwerp" else "besluit"
                eerste = next((d for d in documenten_van(b, submap) if d["soort"] == fase), None)
                if doc is eerste and gepubliceerd and gepubliceerd != f"{b.zichtbaar[fase]:%Y-%m-%d}":
                    log.warning("%s: %s is bekendgemaakt op %s, zichtbaar_in_dso.%s zegt %s",
                                b.map, doc["id"], gepubliceerd, fase, b.zichtbaar[fase])
                publicaties[doc["id"]] = {
                    "titel": (meta.get("DC.title") or [doc["titel"]])[0],
                    "gepubliceerd": gepubliceerd,
                    "link": PUBLICATIE_HTML.format(id=doc["id"]), "bijlagen": uit}
            bewaard = set(opgeslagen.values())
            # <Ontwerp|Definitief>/kaarten is where earlier versions put them.
            fasen = [b.submap(fase) for fase, docs in (("Ontwerp", b.ontwerp), ("Definitief", b.definitief))
                     if docs]
            for sub in ["kaarten"] + [f"{m}/{soort}" for m in fasen for soort in ("kaarten", "bijlagen")]:
                map_ = self.map(b, *sub.split("/"))
                for naam in sorted(os.listdir(map_)) if os.path.isdir(map_) else []:
                    if f"{sub}/{naam}" not in bewaard:
                        os.remove(os.path.join(map_, naam))
                        self.gewijzigd.add(b.map)
                        log.info("%s: %s/%s niet meer nodig, verwijderd", b.map, sub, naam)
                if os.path.isdir(map_) and not os.listdir(map_):
                    os.rmdir(map_)
            nieuw = {"toelichting": "Per publicatie de kaarten (GIO's, GML), informatieobjecten en "
                                    "externe bijlagen die ze vaststelt of meestuurt: waar het bestand "
                                    "vandaan komt, en zijn SHA-256. Een kaart van het ontwerp die het "
                                    "besluit ook vaststelt, staat alleen in de versie van het besluit "
                                    "(vastgesteld_als). Een bijlage die gelijk is aan een eerdere staat "
                                    "er één keer (identiek_aan).",
                     "publicaties": publicaties}
            if not vorige or json.loads(vorige) != nieuw:
                schrijf_atomair(pad, json.dumps(nieuw, ensure_ascii=False, indent=2).encode("utf-8"))
                self.gewijzigd.add(b.map)
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
        taken, van = [], {}
        for b in self.te_doen():
            for t in b.toestanden:
                pad = self._toestand_pad(b, t)
                if not self._is_pdf(pad):
                    url = CVDR_TOESTAND.format(regeling=self.config.regeling["cvdr"],
                                               toestand=t["nummer"])
                    taken.append((url, date.fromisoformat(t["geldig_vanaf"]), pad))
                    van[pad] = b.map
        if taken:
            print_pdfs(taken, browser)
        self.gewijzigd |= {van[pad] for _, _, pad in taken if self._is_pdf(pad)}

    # RTR: volledige vastleggingen ───────────────────────────────────

    def rtr(self) -> None:
        for b in self.te_doen():
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
                self.gewijzigd.add(b.map)

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
        h = Versiehistorie(self.historiepad,
                           self.config.bestuursorgaan, self.client.base_url)
        eerste = self.config.in_werking[0].in_werking
        walks = set(maandelijks(eerste, tot)) | {tot}
        walks |= {d for b in self.config.in_werking for d in b.peildata + [b.in_werking] if d <= tot}
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
        """Every STTR version no full snapshot holds, in a ``<date>_gewijzigd`` folder of its besluit.

        For a besluit that is not complete, these folders are derived from
        the snapshots and the history, so this step owns them: one it no
        longer plans goes, and files and manifest follow the history.  A
        complete besluit only gets new folders, and what is in it stays as it
        is: changes keep coming under the newest besluit until the next one
        is in werking.
        """
        if not self.config.in_werking:
            log.info("Nog geen besluit in werking: geen toepasbare regels")
            return
        h = self.historie(tot)
        in_vastlegging: dict[str, set[str]] = defaultdict(set)
        for rel, manifest in self.vastleggingen().items():
            in_vastlegging[rel.split(os.sep)[0]].update(s["sttrId"] for s in manifest["sttrs"])
        gepland = verdeel(self.config, h.data["refs"], in_vastlegging, tot, h.omschrijving)

        for b in self.te_doen():
            rtr_map = self.map(b, "RTR")
            for naam in sorted(os.listdir(rtr_map)) if os.path.isdir(rtr_map) else []:
                if GEWIJZIGD.match(naam) and os.path.isdir(os.path.join(rtr_map, naam)) \
                        and (b.map, naam) not in gepland:
                    shutil.rmtree(os.path.join(rtr_map, naam))
                    self.gewijzigd.add(b.map)
                    log.info("%s/RTR/%s: niet meer nodig, verwijderd", b.map, naam)

        for (besluitmap, naam), items in sorted(gepland.items()):
            map_pad = os.path.join(self.uit, besluitmap, "RTR", naam)
            manifest_pad = os.path.join(map_pad, "STTR", "manifest.json")
            if besluitmap in self.af and os.path.exists(manifest_pad):
                with open(manifest_pad, encoding="utf-8") as f:
                    er = {s.get("sttrId") for s in json.load(f)["sttrs"]}
                niet = sorted(v["sttrId"] for v in items if not v.get("vervallen") and v["sttrId"] not in er)
                if niet:
                    log.warning("%s/RTR/%s blijft zoals het is (besluit compleet); niet toegevoegd: %s",
                                besluitmap, naam, ", ".join(niet))
                continue
            if self._schrijf_gewijzigd(map_pad, items):
                self.gewijzigd.add(besluitmap)

    def _schrijf_gewijzigd(self, map_pad: str, items: list[dict[str, Any]]) -> bool:
        """One ``<date>_gewijzigd`` folder: its STTR files and manifest. True when anything changed."""
        sttr_map = os.path.join(map_pad, "STTR")
        os.makedirs(sttr_map, exist_ok=True)
        veranderd = False
        bestandsnaam = lambda v: (f"STTR_{v['sttrId']}_{v['activiteit'].split('.')[-1]}_"  # noqa: E731
                                  f"{v['soort'].replace(' ', '_')}.xml")
        gepland = {bestandsnaam(v) for v in items if not v.get("vervallen")}
        for naam in os.listdir(sttr_map):
            if naam.endswith(".xml") and naam not in gepland:
                os.remove(os.path.join(sttr_map, naam))
                veranderd = True
        manifest = []
        for v in sorted(items, key=lambda x: (x["activiteit"], x["soort"])):
            if v.get("vervallen"):
                manifest.append(v)
                continue
            naam = bestandsnaam(v)
            inhoud = self.client.download_sttr(v["sttrId"])
            pad = os.path.join(sttr_map, naam)
            if not os.path.exists(pad):
                schrijf_atomair(pad, inhoud)
                veranderd = True
            manifest.append({"bestand": naam, **{k: v[k] for k in (
                "activiteit", "omschrijving", "soort", "sttrId", "begindatum",
                "einddatum", "laatsteWijzigingDatum", "ref", "vervangt", "vervangen_door")},
                "sha256": hashlib.sha256(inhoud).hexdigest()})
        nieuw = {"bestuursorgaan": self.config.bestuursorgaan.naam,
                 "datum": os.path.basename(map_pad)[:8],
                 "toelichting": "STTR-versies die op deze dag ingingen en niet in een "
                                "volledige vastlegging van dit besluit staan, en "
                                "regelbeheerobjecten die op deze dag vervielen.",
                 "bron": self.client.base_url, "sttrs": manifest}
        pad = os.path.join(sttr_map, "manifest.json")
        vorige = None
        if os.path.exists(pad):
            with open(pad, encoding="utf-8") as f:
                vorige = json.load(f)
        if vorige != nieuw:
            with open(pad, "w", encoding="utf-8") as f:
                json.dump(nieuw, f, ensure_ascii=False, indent=2)
            veranderd = True
        log.info("%s: %d versies, %d vervallen%s", os.path.relpath(map_pad, self.uit),
                 sum(1 for m in manifest if not m.get("vervallen")),
                 sum(1 for m in manifest if m.get("vervallen")), "" if veranderd else " (ongewijzigd)")
        return veranderd

    # index ───────────────────────────────────────────────────────────

    def index(self) -> dict[str, Any]:
        h = Versiehistorie(self.historiepad,
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

        # What earlier versions kept in the archive and now lives in the werkmap
        # (or, for the config, in the repository).
        for naam in (os.path.basename(self.config.pad), "versiehistorie.json", "archief.log"):
            pad = os.path.join(self.uit, naam)
            if self.werkmap != self.uit and os.path.isfile(pad) \
                    and os.path.abspath(pad) != os.path.abspath(self.config.pad):
                os.remove(pad)
                self.gewijzigd.add(".")
                log.info("%s: hoort niet in het archief, verwijderd", naam)

        weg_per: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for v in verdwenen(h.data):
            b = self.config.besluit_voor(date.fromisoformat(v["eerst_weg"]))
            if b:
                weg_per[b.map].append(v)
        # README.txt of a complete besluit is only written again when its folder
        # got something new; that of another one also when it would say more.
        nakijken = {}
        for b in self.config.besluiten:
            pad = self.map(b, "README.txt")
            if b.map in self.gewijzigd or not os.path.exists(pad) or b.map not in self.af:
                nakijken[b.map] = self._readme_txt(b, mappen, weg_per[b.map],
                                                   alleen_als_anders=b.map not in self.gewijzigd)
            else:
                with open(pad, encoding="utf-8") as f:
                    nakijken[b.map] = sum(1 for r in f if r.startswith("!"))
        weg = [v for b in self.config.besluiten for v in weg_per[b.map]]
        if self.gewijzigd or not os.path.exists(os.path.join(self.uit, "README.md")):
            self._readme(mappen, len(alle), len(alle) - len(ontbreekt), ontbreekt, weg, nakijken)
        elif os.path.exists(os.path.join(self.uit, "index.csv")):
            log.info("Archief ongewijzigd: README.md en index.csv blijven zoals ze zijn")
            return {"versies": len(alle), "gearchiveerd": len(alle) - len(ontbreekt),
                    "ontbreekt": ontbreekt}
        # Last, so the hashes cover the README files just written.
        with open(os.path.join(self.uit, "index.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["pad", "bytes", "sha256"])
            for wortel, mapnamen, bestanden in os.walk(self.uit):
                mapnamen.sort()
                for naam in sorted(bestanden):
                    if naam == "index.csv" or naam.endswith(".tmp"):
                        continue
                    pad = os.path.join(wortel, naam)
                    w.writerow([os.path.relpath(pad, self.uit).replace(os.sep, "/"),
                                os.path.getsize(pad), sha256(pad)])
        return {"versies": len(alle), "gearchiveerd": len(alle) - len(ontbreekt),
                "ontbreekt": ontbreekt}

    def _readme_txt(self, b: Besluit, mappen: dict[str, dict[str, Any]],
                    weg: list[dict[str, Any]], alleen_als_anders: bool = False) -> int:
        """README.txt of a besluit folder; returns the number of '!' lines.

        Section by section what is in the folder, and with a '!' in front
        what belongs to the besluit but is not there: a publication, kaart or
        bijlage that was not downloaded, a toestand that was not printed.  A
        file put in the folder by hand is listed as the inspraakrapport when
        its name says so, otherwise under "overige bestanden".

        Everything from a line starting with ``Handmatig`` to the end is the
        user's own and is kept.  A README.txt that is replaced is first copied
        to ``vorige-readme/`` in the werkmap.  With *alleen_als_anders* one
        that says the same, apart from the day it was made, is left alone.
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

        def publicatie(regels: list[str], submap: str, doc: dict[str, str]) -> None:
            p = bijl.get(doc["id"], {})
            datum = (f", {blad(doc['id'])} {date.fromisoformat(p['gepubliceerd']):%d-%m-%Y}"
                     if p.get("gepubliceerd") else "")
            rel = f"{b.submap(submap)}/{doc['id']}_{doc['soort']}.pdf"
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

        if b.ontwerp:
            r = sectie("ontwerp")
            for d in ontwerp:
                publicatie(r, "Ontwerp", d)
            r = sectie("kennisgeving terinzagelegging")
            for d in kennisgevingen:
                publicatie(r, "Ontwerp", d)
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
        kaarten(sectie("kaarten" if b.nieuw else "gewijzigde kaarten"),
                bijlagen_van(b.definitief, "kaart") + bijlagen_van(b.ontwerp, "kaart"))
        if any(x["bestand"] not in genoemd for x in bijlagen_van(b.definitief, "bijlage")):
            overige_bijlagen(sectie("bijlagen besluit"), bijlagen_van(b.definitief, "bijlage"))
        r = sectie("hele regeling")
        for t in b.toestanden:
            vanaf = date.fromisoformat(t["geldig_vanaf"])
            rel = f"{b.submap('Definitief')}/regeling_toestand-{t['nummer']}_geldend-vanaf-{vanaf:%Y%m%d}.pdf"
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
        if not r and b.in_werking:
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

        pad = self.map(b, "README.txt")
        oud = None
        if os.path.exists(pad):
            with open(pad, encoding="utf-8", newline="") as f:
                oud = f.read().replace("\r\n", "\n")
        m = re.search(r"(?m)^Handmatig", oud or "")
        handmatig = oud[m.start():] if m else ""
        uitroep += sum(1 for r in handmatig.split("\n") if r.startswith("!"))

        if besluit:
            kop = [self.config.regeling["titel"],
                   f"Besluit van {b.besluitdatum:%d-%m-%Y}, bekendmaking {', '.join(d['id'] for d in besluit)}"]
        else:
            kop = [self.config.regeling["titel"],
                   f"Ontwerp van {b.zichtbaar['ontwerp']:%d-%m-%Y}, bekendmaking "
                   f"{', '.join(d['id'] for d in ontwerp or b.ontwerp)}; nog geen besluit"]
        if b.in_werking:
            kop.append(f"In werking {b.in_werking:%d-%m-%Y}" + (f" tot {b.tot:%d-%m-%Y}" if b.tot else "")
                       + (f"; toestand {', '.join(str(t['nummer']) for t in b.toestanden)} van "
                          f"{self.config.regeling['cvdr']}" if b.toestanden else ""))
        elif besluit:
            kop.append("In werking: nog niet bekend")
        kop.append("")
        if b.opmerking:
            kop += [b.opmerking, ""]
        kop += [f"Samengesteld op {date.today():%d-%m-%Y} met code/archief.py.",
                "Een '!' vooraan een regel: dit hoort bij het besluit maar zit niet in deze map. "
                f"Nakijken: {uitroep} regel(s).", ""]
        regels = kop + ["Bestanden", ""]
        for naam, inhoud in secties:
            if inhoud:  # what was not published is not mentioned
                regels += [T + naam] + inhoud + [""]
        bladen = " en het ".join(dict.fromkeys(blad(d["id"]) for d in b.ontwerp + b.definitief))
        regels += ["Toelichting"] + [textwrap.fill(p, 100, initial_indent=T, subsequent_indent=T) + "\n"
                                     for p in (
            f"Publicaties komen uit het {bladen} (zoek.officielebekendmakingen.nl), de hele "
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
        tekst = "\n".join(regels) + ("\n" + handmatig if handmatig else "")
        zonder_datum = lambda t: re.sub(r"(?m)^Samengesteld op [\d-]+ ", "", t)  # noqa: E731
        leesmij = self.map(b, "LEESMIJ.md")
        if os.path.exists(leesmij):
            os.remove(leesmij)
            self.gewijzigd.add(b.map)
        if oud is not None and (oud == tekst or alleen_als_anders and zonder_datum(oud) == zonder_datum(tekst)):
            return uitroep
        if oud is not None:
            vorige = os.path.join(self.werkmap, "vorige-readme",
                                  f"{b.map}_{time.strftime('%Y%m%d-%H%M%S')}_README.txt")
            os.makedirs(os.path.dirname(vorige), exist_ok=True)
            shutil.copy2(pad, vorige)
            log.info("%s: README.txt vernieuwd; de vorige staat in %s", b.map, vorige)
        os.makedirs(self.map(b), exist_ok=True)  # a besluit nothing was fetched for yet
        with open(pad, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(tekst)
        self.gewijzigd.add(b.map)
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
                  "in zit; de kolom Nakijken telt die regels.", "",
                  "| Besluit | Map | In werking | Ontwerp | Definitief | Kaarten | Bijlagen "
                  "| Vastleggingen | Gewijzigd op | Nakijken |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for b in self.config.besluiten:
            eigen = sorted(os.path.basename(k) for k in mappen if k.split(os.sep)[0] == b.map)
            vast = ", ".join(f"{n[6:8]}-{n[4:6]}-{n[:4]}" for n in eigen if n.endswith("_volledig"))
            gew = sum(1 for n in eigen if n.endswith("_gewijzigd"))
            tel = {soort: sum(len(os.listdir(p)) for p in paden if os.path.isdir(p)) for soort, paden in (
                ("kaarten", [self.map(b, "kaarten")]),
                ("bijlagen", [self.map(b, b.submap("Ontwerp"), "bijlagen") if b.ontwerp else "",
                              self.map(b, b.submap("Definitief"), "bijlagen")]))}
            besluitdatum = (f"{b.besluitdatum:%d-%m-%Y}" if b.besluitdatum
                            else f"ontwerp {b.zichtbaar['ontwerp']:%d-%m-%Y}")
            in_werking = f"{b.in_werking:%d-%m-%Y}" if b.in_werking else "-"
            regels.append(
                f"| {besluitdatum} | `{b.map}` | {in_werking} | "
                f"{len(b.ontwerp)} | {len(b.definitief)} + {len(b.toestanden)} regeling | "
                f"{tel['kaarten']} | {tel['bijlagen']} | {vast or '-'} | {gew} dag(en) | "
                f"{nakijken.get(b.map, 0)} |")
        regels += ["", "## Volledigheid", "",
                   f"De RTR kent {totaal} versies van toepasbare regels voor dit bestuursorgaan; "
                   f"{gearchiveerd} daarvan staan in dit archief"
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
            if b is None:
                continue
            if str(v["sttrId"]) in in_vastlegging.get(b.map, set()):
                continue
            uit[(b.map, f"{begin:%Y%m%d}_gewijzigd")].append({
                **v, "sttrId": str(v["sttrId"]), "ref": ref, "activiteit": r["activiteit"], "soort": r["label"],
                "omschrijving": omschrijving(r["activiteit"]),
                "vervangt": str(versies[i - 1]["sttrId"]) if i else None,
                "vervangen_door": str(versies[i + 1]["sttrId"]) if i + 1 < len(versies) else None})
        if versies and versies[-1].get("einddatum"):
            weg = nl_datum(versies[-1]["einddatum"]) + timedelta(days=1)
            b = config.besluit_voor(weg)
            if weg <= tot and b is not None:
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
    p.add_argument("config", nargs="?", default=os.path.join(basis, "data", "archief_CVDR707248.json"),
                   help="JSON met het bestuursorgaan, de regeling en de besluiten "
                        "(standaard: data/archief_CVDR707248.json)")
    p.add_argument("--uit", default=os.path.join(basis, "archief"), help="doelmap (standaard: archief/)")
    p.add_argument("--stappen", default=",".join(STAPPEN),
                   help=f"komma-gescheiden, uit: {', '.join(STAPPEN)} (standaard: alle)")
    p.add_argument("--rate", type=float, default=2.0, help="verzoeken per seconde aan de RTR")
    p.add_argument("--tot", type=date.fromisoformat, default=date.today(),
                   help="versiegeschiedenis tot en met deze datum (jjjj-mm-dd, standaard: vandaag)")
    p.add_argument("--browser", default=None,
                   help="pad naar een Chromium/Chrome/Edge om de regeling af te drukken")
    p.add_argument("--cache", default=os.path.join(basis, "log", "sttr-cache"),
                   help="map voor eenmaal gedownloade STTR-bestanden")
    p.add_argument("--werkmap", default=os.path.join(basis, "log"),
                   help="map voor de versiegeschiedenis en het logboek, buiten het archief (standaard: log/)")
    args = p.parse_args(argv)

    stappen = [s.strip() for s in args.stappen.split(",") if s.strip()]
    onbekend = set(stappen) - set(STAPPEN)
    if onbekend:
        p.error(f"onbekende stap(pen): {', '.join(sorted(onbekend))}")
    os.makedirs(args.uit, exist_ok=True)
    os.makedirs(args.werkmap, exist_ok=True)
    naam = werknaam(args.config)
    # Earlier versions kept the history and the log in the archive: move them out.
    for oud, nieuw in (("versiehistorie.json", f"{naam}_versiehistorie.json"), ("archief.log", f"{naam}.log")):
        oud, nieuw = os.path.join(args.uit, oud), os.path.join(args.werkmap, nieuw)
        if os.path.isfile(oud) and os.path.abspath(oud) != os.path.abspath(nieuw):
            if os.path.exists(nieuw) and oud.endswith(".log"):
                with open(oud, "rb") as van, open(nieuw, "ab") as naar:
                    shutil.copyfileobj(van, naar)
                os.remove(oud)
            elif not os.path.exists(nieuw):
                os.replace(oud, nieuw)
    bestand = logging.FileHandler(os.path.join(args.werkmap, f"{naam}.log"), encoding="utf-8")
    bestand.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), bestand])

    try:
        return draai(args, stappen)
    except ConfigFout as exc:
        log.error("%s", exc)
        return 2
    except catalogus.RijkNietOndersteund as exc:
        log.error("%s", exc)
        return 3


def zoek_nieuw(pad: str, vandaag: date) -> bool:
    """The step zoeken: new publications of the regeling into the JSON file; True when it was written.

    When the sources cannot be reached, the run goes on with what the file has.
    """
    with open(pad, encoding="utf-8") as f:
        tekst = f.read()
    ruw = json.loads(tekst)
    try:
        veranderd = zoeken.zoek(ruw, vandaag, zoeken.Bron(haal, lees_metadata))
    except (RuntimeError, OSError, ET.ParseError) as exc:
        log.warning("Zoeken mislukt (%s); verder met wat in %s staat", exc, pad)
        return False
    for regel in veranderd:
        log.info("Gevonden: %s", regel)
    nieuw = zoeken.schrijf(ruw)
    if nieuw == tekst:
        log.info("Zoeken: niets nieuws")
        return False
    schrijf_atomair(pad, nieuw.encode("utf-8"))
    log.info("%s bijgewerkt", pad)
    return True


def draai(args: argparse.Namespace, stappen: list[str]) -> int:
    """The steps; 1 when the archive misses STTR versions."""
    config = lees_config(args.config)  # an unusable file stops the run before anything is fetched
    if "zoeken" in stappen and zoek_nieuw(args.config, date.today()):
        config = lees_config(args.config)
    client = ArchiefClient(BASE_URLS[config.omgeving], rate=args.rate, cache=args.cache)
    archief = Archief(config, args.uit, client, werkmap=args.werkmap)
    archief.voorbereiden()
    tot = args.tot
    if {"rtr", "wijzigingen"} & set(stappen):
        archief.vind_roots(tot)

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

"""
Find a regeling's new publications and add them to the archive's JSON file.

The step ``zoeken`` of ``code/archief.py``, before everything else:

1. Ask the SRU of the official publications for the bestuursorgaan's
   publications in its blad whose title has the regeling's first word, from
   60 days before the newest one the file knows.
2. Read the metadata of each one the file does not know yet:

   - **besluit**: ``OVERHEIDop.betreftRegeling`` is ``<CVDR>_<toestand>`` of
     this regeling.  Its besluitdatum is ``OVERHEIDop.datumOndertekening``.
   - **ontwerp**: ``OVERHEIDop.betreftProefversie`` is a version of the
     regeling's work (learned from a known publication), without a
     betreftRegeling.
   - **kennisgeving**: ``OVERHEIDop.mededelingOver`` points to a publication
     of this regeling.
   - anything else is left out, and logged.

3. Group them.  A besluit with the besluitdatum of a known one is a
   herpublicatie and joins it.  Otherwise it completes the besluit that has
   only an ontwerp (with several, the one whose title fits best: check it),
   or starts a new one.  A new ontwerp starts a new besluit.  A kennisgeving
   joins the publication it is about.
4. In werking: the toestand's page on lokaleregelgeving.overheid.nl says
   ``Geldend van dd-mm-jjjj``.  A toestand that is not there yet is tried
   again on the next run.  A besluit in werking without peildata gets one:
   today, or the day before the next besluit took effect.

What is not a STOP publication (a "Start inspraak" with externe bijlagen, a
notice of another kind) cannot be linked this way and is added by hand.
The file is written back in its own layout (``schrijf``).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Callable

log = logging.getLogger("rtr.zoeken")

SRU = "https://repository.overheid.nl/sru"
METADATA = "https://repository.overheid.nl/frbr/officielepublicaties/{blad}/{jaar}/{id}/1/metadata/metadata.xml"
CVDR_TOESTAND = "https://lokaleregelgeving.overheid.nl/{regeling}/{toestand}"
DCTERMS = "{http://purl.org/dc/terms/}"
MARGE = timedelta(days=60)
NIEUWE_NAAM = "wijzigingsbesluit"
SLEUTELS = ("besluitdatum", "naam", "in_werking", "zichtbaar_in_dso", "ontwerp", "definitief",
            "toestanden", "rtr_peildata", "opmerking")


class Bron:
    """Where the step reads: the SRU, a publication's metadata, a toestand's page.

    *haal* fetches a URL and raises ``urllib.error.HTTPError`` for a page that
    is not there; *lees_metadata* turns a metadata.xml into name -> contents.
    """

    def __init__(self, haal: Callable[[str], bytes],
                 lees_metadata: Callable[[bytes], dict[str, list[str]]]):
        self.haal = haal
        self.lees_metadata = lees_metadata
        self._metadata: dict[str, dict[str, list[str]]] = {}

    def zoek(self, query: str) -> list[str]:
        """The identifiers of every record the SRU finds for *query*."""
        ids, start = [], 1
        while True:
            url = SRU + "?" + urllib.parse.urlencode(
                {"query": query, "maximumRecords": 1000, "startRecord": start})
            wortel = ET.fromstring(self.haal(url))
            ids += [el.text.strip() for el in wortel.iter(DCTERMS + "identifier") if el.text]
            volgende = next((el.text for el in wortel.iter() if el.tag.endswith("}nextRecordPosition")), None)
            if not volgende:
                return list(dict.fromkeys(ids))
            start = int(volgende)

    def metadata(self, pub_id: str) -> dict[str, list[str]]:
        if pub_id not in self._metadata:
            blad, jaar, _ = pub_id.split("-", 2)
            self._metadata[pub_id] = self.lees_metadata(self.haal(METADATA.format(blad=blad, jaar=jaar, id=pub_id)))
        return self._metadata[pub_id]

    def geldend_van(self, cvdr: str, toestand: int) -> date | None:
        """The day the toestand took effect; None while CVDR does not have it."""
        try:
            pagina = self.haal(CVDR_TOESTAND.format(regeling=cvdr, toestand=toestand))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        m = re.search(r"Geldend van (\d{2})-(\d{2})-(\d{4})", pagina.decode("utf-8", "replace"))
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _volgorde(pub_id: str) -> tuple[int, int]:
    """``wsb-2026-14697`` -> (2026, 14697): newest last."""
    _, jaar, nummer = pub_id.split("-", 2)
    return int(jaar), int(nummer)


def _eerste(meta: dict[str, list[str]], naam: str) -> str:
    return (meta.get(naam) or [""])[0]


def _werk(expressie: str) -> str:
    """``/akn/nl/act/ws0636/2023/HDSRWSV/nld@2026-05-13;V7`` -> ``/akn/nl/act/ws0636/2023/HDSRWSV``."""
    return expressie.split("/nld@", 1)[0].split("@", 1)[0].rstrip("/")


def _publicatie_id(akn: str) -> str | None:
    """``/akn/nl/officialGazette/wsb/2026/14697/nld@2026-06-05`` -> ``wsb-2026-14697``."""
    m = re.search(r"/officialGazette/([a-z]+)/(\d{4})/(\d+)", akn)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _woorden(tekst: str) -> set[str]:
    return set(re.findall(r"\w+", tekst.casefold()))


def _kaarten(meta: dict[str, list[str]]) -> set[str]:
    """The works of the GIOs a publication sets (``/join/id/regdata/…/WG_X``, without the version)."""
    return {j.rsplit("/", 1)[0] for j in meta.get("OVERHEIDop.steltVast", [])}


def zoek(ruw: dict[str, Any], vandaag: date, bron: Bron) -> list[str]:
    """Add the regeling's new publications to *ruw* (the JSON file, parsed); returns what changed."""
    cvdr = ruw["regeling"]["cvdr"]
    besluiten = ruw["besluiten"]
    bekend: dict[str, tuple[dict[str, Any], str]] = {}  # id -> (besluit, "ontwerp" | "definitief")
    for b in besluiten:
        for fase in ("ontwerp", "definitief"):
            for d in b.get(fase, []):
                bekend[d["id"]] = (b, fase)
    veranderd: list[str] = []

    # 1. What to ask for, learned from the newest known publications.
    creator = blad = None
    werken: set[str] = set()
    for pub_id in sorted(bekend, key=_volgorde, reverse=True):
        meta = bron.metadata(pub_id)
        creator = creator or _eerste(meta, "DC.creator") or None
        blad = blad or _eerste(meta, "OVERHEIDop.publicationName") or None
        werken |= {_werk(p) for p in meta.get("OVERHEIDop.betreftProefversie", [])}
        if creator and blad and werken:
            break
    if not (creator and blad):
        log.warning("Zoeken: bestuursorgaan of blad niet gevonden in de metadata van %s", ", ".join(bekend))
        return veranderd
    datums = [date.fromisoformat(v) for b in besluiten for v in b.get("zichtbaar_in_dso", {}).values()]
    vanaf = max(datums) - MARGE
    eerste_woord = ruw["regeling"]["titel"].split()[0]
    query = (f'c.product-area==officielepublicaties AND w.publicatienaam=="{blad}" AND '
             f'dt.creator=="{creator}" AND dt.title any "{eerste_woord}" AND dt.date>={vanaf:%Y-%m-%d}')
    nieuw = [i for i in bron.zoek(query) if i not in bekend]
    log.info("Zoeken: %s vanaf %s, %d onbekend", blad, vanaf, len(nieuw))

    # 2. What each new one is.  A besluit of the regeling names its work too,
    # which recognises the ontwerpen when the known publications predate STOP.
    metas = {pub_id: bron.metadata(pub_id) for pub_id in sorted(nieuw, key=_volgorde)}
    for meta in metas.values():
        if any(r.split("_", 1)[0] == cvdr for r in meta.get("OVERHEIDop.betreftRegeling", [])):
            werken |= {_werk(p) for p in meta.get("OVERHEIDop.betreftProefversie", [])}
    gevonden = []
    for pub_id, meta in metas.items():
        doc = {"id": pub_id, "titel": _eerste(meta, "DC.title")}
        zichtbaar = _eerste(meta, "DCTERMS.available")
        regelingen = meta.get("OVERHEIDop.betreftRegeling", [])
        if regelingen:
            if any(r.split("_", 1)[0] == cvdr for r in regelingen):
                gevonden.append(("besluit", doc, zichtbaar, meta))
            else:
                log.info("Zoeken: %s gaat over %s, niet over %s", pub_id, ", ".join(regelingen), cvdr)
        elif any(_werk(p) in werken for p in meta.get("OVERHEIDop.betreftProefversie", [])):
            gevonden.append(("ontwerp", doc, zichtbaar, meta))
        elif meta.get("OVERHEIDop.mededelingOver"):
            gevonden.append(("kennisgeving", doc, zichtbaar, meta))
        else:
            log.info("Zoeken: %s (%s) verwijst niet naar de regeling; zo nodig met de hand toevoegen",
                     pub_id, doc["titel"])

    # 3. Where it goes: besluiten and ontwerpen first, then what is said about them.
    herpublicaties: list[dict[str, Any]] = []
    for soort, doc, zichtbaar, meta in sorted(gevonden, key=lambda g: (g[0] == "kennisgeving", g[2])):
        doc = {"id": doc["id"], "soort": soort, "titel": doc["titel"]}
        if soort == "ontwerp":
            b = {"naam": NIEUWE_NAAM, "zichtbaar_in_dso": {"ontwerp": zichtbaar}, "ontwerp": [doc]}
            besluiten.append(b)
            bekend[doc["id"]] = (b, "ontwerp")
            veranderd.append(f"{doc['id']}: ontwerp, nieuw besluit ({doc['titel']})")
        elif soort == "besluit":
            datum = _eerste(meta, "OVERHEIDop.datumOndertekening") or zichtbaar
            b = next((x for x in besluiten if x.get("besluitdatum") == datum), None)
            if b:
                b["definitief"].append(doc)
                herpublicaties.append(b)
                wat = "herpublicatie bij het besluit van " + datum
            else:
                open_ = [x for x in besluiten if x.get("ontwerp") and not x.get("definitief")
                         and x["zichtbaar_in_dso"]["ontwerp"] <= zichtbaar]
                if open_:
                    # The ontwerp that set the same kaarten, else the one whose title fits best.
                    kaarten = _kaarten(meta)

                    def score(x: dict[str, Any]) -> tuple[int, int]:
                        ontwerp = [d for d in x["ontwerp"] if d["soort"] == "ontwerp"] or x["ontwerp"]
                        gedeeld = set().union(*(_kaarten(bron.metadata(d["id"])) for d in ontwerp)) & kaarten
                        titels = _woorden(" ".join(d["titel"] for d in x["ontwerp"]))
                        return len(gedeeld), len(_woorden(doc["titel"]) & titels)
                    scores = {id(x): score(x) for x in open_}
                    b = max(open_, key=lambda x: scores[id(x)])
                    wat = f"besluit bij ontwerp {b['ontwerp'][0]['id']}"
                    beste = scores[id(b)]
                    if len(open_) > 1 and (beste[0] == 0 or list(scores.values()).count(beste) > 1):
                        wat += f" (uit {len(open_)} ontwerpen op de titel gekozen: controleer)"
                        log.warning("Zoeken: %s gekoppeld aan %s op de titel; controleer",
                                    doc["id"], b["ontwerp"][0]["id"])
                    elif len(open_) > 1:
                        wat += f" (uit {len(open_)} ontwerpen: {beste[0]} dezelfde kaarten)"
                else:
                    b = {"naam": NIEUWE_NAAM}
                    besluiten.append(b)
                    wat = "besluit zonder ontwerp"
                b["besluitdatum"] = datum
                b.setdefault("zichtbaar_in_dso", {})["besluit"] = zichtbaar
                b["definitief"] = [doc]
            bekend[doc["id"]] = (b, "definitief")
            veranderd.append(f"{doc['id']}: {wat} ({doc['titel']})")
        else:
            over = [_publicatie_id(m) for m in meta["OVERHEIDop.mededelingOver"]]
            doel = next((bekend[i] for i in over if i in bekend), None)
            if doel is None:
                log.info("Zoeken: %s is een mededeling over %s, geen publicatie van de regeling",
                         doc["id"], ", ".join(filter(None, over)))
                continue
            b, fase = doel
            b[fase].append(doc)
            bekend[doc["id"]] = doel
            veranderd.append(f"{doc['id']}: kennisgeving bij {', '.join(i for i in over if i in bekend)}")

    # 4. In werking, and a peildatum for the STTR.
    for b in besluiten:
        if b.get("definitief") and (not b.get("in_werking") or any(b is x for x in herpublicaties)):
            veranderd += _toestanden(b, cvdr, bron)
    gaand = sorted((b for b in besluiten if b.get("in_werking")), key=lambda b: b["in_werking"])
    for b, volgend in zip(gaand, gaand[1:] + [None]):
        if b.get("rtr_peildata"):
            continue
        peil = vandaag if volgend is None else min(
            vandaag, date.fromisoformat(volgend["in_werking"]) - timedelta(days=1))
        if peil >= date.fromisoformat(b["in_werking"]):
            b["rtr_peildata"] = [peil.isoformat()]
            veranderd.append(f"{b['besluitdatum']}: peildatum {peil}")
    for b in besluiten:
        _orden(b)
    # By date, as the file lists them: a besluit by its besluitdatum, an ontwerp by its day.
    besluiten.sort(key=lambda b: b.get("besluitdatum") or b["zichtbaar_in_dso"].get("ontwerp", ""))
    return veranderd


def _toestanden(b: dict[str, Any], cvdr: str, bron: Bron) -> list[str]:
    """The toestanden the besluit's publications set, and so its in-werking date.

    Not in werking yet: only once CVDR has every toestand (else the next run
    tries again).  In werking already (a herpublicatie came): the toestanden
    CVDR has are added, the others logged.
    """
    nummers = set()
    for d in b["definitief"]:
        if d["soort"] == "besluit":
            for r in bron.metadata(d["id"]).get("OVERHEIDop.betreftRegeling", []):
                regeling, _, toestand = r.partition("_")
                if regeling == cvdr and toestand.isdigit():
                    nummers.add(int(toestand))
    toestanden = {t["nummer"]: t for t in b.get("toestanden", [])}
    nieuw, ontbreekt = [], []
    for n in sorted(nummers - set(toestanden)):
        vanaf = bron.geldend_van(cvdr, n)
        if vanaf is None:
            ontbreekt.append(n)
        else:
            nieuw.append({"nummer": n, "geldig_vanaf": vanaf.isoformat()})
    if ontbreekt:
        log.log(logging.INFO if not b.get("in_werking") else logging.WARNING,
                "Zoeken: toestand %s van %s staat nog niet in CVDR; %s", ", ".join(map(str, ontbreekt)), cvdr,
                "de volgende keer opnieuw" if not b.get("in_werking") else "voeg die later met de hand toe")
        if not b.get("in_werking"):
            return []
    if not nieuw:
        return []
    b["toestanden"] = sorted([*toestanden.values(), *nieuw], key=lambda t: t["nummer"])
    eerder = b.get("in_werking")
    b["in_werking"] = min(t["geldig_vanaf"] for t in b["toestanden"])
    return [f"{b['besluitdatum']}: " + (f"in werking {b['in_werking']}, " if b["in_werking"] != eerder else "")
            + f"toestand {', '.join(str(t['nummer']) for t in nieuw)}"]


def _orden(b: dict[str, Any]) -> None:
    """The besluit's keys in the file's order; others after them."""
    eerst = {k: b[k] for k in SLEUTELS if k in b}
    rest = {k: v for k, v in b.items() if k not in eerst}
    b.clear()
    b.update(eerst)
    b.update(rest)


# ── the file's own layout ────────────────────────────────────────────


def schrijf(ruw: dict[str, Any]) -> str:
    """*ruw* as JSON in the layout of the archive's files.

    Two spaces per level, one publication per line (its opmerking on the
    next), ``zichtbaar_in_dso`` and ``rtr_peildata`` on the line of their
    key, ``toestanden`` too when there is one.
    """
    def inline(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    def blok(v: Any, inspringen: int) -> str:
        return json.dumps(v, ensure_ascii=False, indent=2).replace("\n", "\n" + " " * inspringen)

    def publicatie(d: dict[str, Any]) -> str:
        kort = {k: d[k] for k in ("id", "soort", "titel") if k in d}
        rest = {k: v for k, v in d.items() if k not in kort}
        tekst = inline(kort)
        if rest:
            tekst = tekst[:-1] + ",\n" + " " * 9 + inline(rest)[1:]
        return " " * 8 + tekst

    def lijst(items: list[Any], regel: Callable[[Any], str]) -> str:
        if not items:
            return "[]"
        return "[\n" + ",\n".join(regel(x) for x in items) + "\n      ]"

    def besluit(b: dict[str, Any]) -> str:
        regels = []
        for k, v in b.items():
            if k in ("ontwerp", "definitief"):
                waarde = lijst(v, publicatie)
            elif k == "toestanden" and len(v) != 1:
                waarde = lijst(v, lambda t: " " * 8 + inline(t))
            elif k in ("zichtbaar_in_dso", "rtr_peildata", "toestanden") or not isinstance(v, (dict, list)):
                waarde = inline(v)
            else:
                waarde = blok(v, 6)
            regels.append(f"      {inline(k)}: {waarde}")
        return "    {\n" + ",\n".join(regels) + "\n    }"

    delen = []
    for k, v in ruw.items():
        if k == "besluiten":
            waarde = "[\n" + ",\n".join(besluit(b) for b in v) + "\n  ]" if v else "[]"
        else:
            waarde = blok(v, 2)
        delen.append(f"  {inline(k)}: {waarde}")
    return "{\n" + ",\n".join(delen) + "\n}\n"

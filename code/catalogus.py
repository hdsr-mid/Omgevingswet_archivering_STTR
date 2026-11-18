"""Which government, which activities, and how to name their STTRs.

Ported from Knowledgebase-CLI (``sttr/catalogus.py``, branch
dev-v8-STTR-route2) and generalised from HDSR to any bestuursorgaan.

Every bestuursorgaan hangs its own activities under one national node per
kind of document.  Waterschappen and provincies are all listed under theirs,
so they can be found by name.  The omgevingsplan node only answers for the
OIN it is asked about — there are some 340 gemeenten — so a gemeente has to
be given by OIN.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from rtr_client import RtrClient, RtrError

log = logging.getLogger("rtr.catalogus")

# Ministerie van BZK: the owner of the national activity nodes.
NATIONAAL_OIN = "00000001003214345000"

# One national node per kind of bestuursorgaan; its children are the roots
# of each bestuursorgaan's own tree.
NATIONALE_KNOPEN = {
    "waterschap": "nl.imow-mnre1034.activiteit.ActInWaterschapsverordening",
    "provincie": "nl.imow-mnre1034.activiteit.ActiviteitInOmgevingsver",
    "gemeente": "nl.imow-mnre1034.activiteit.ActInOmgevingsplan",
}

# Words that say which kind of bestuursorgaan a name refers to.
_SOORT_WOORDEN = {
    "waterschap": {"waterschap", "hoogheemraadschap", "wetterskip"},
    "provincie": {"provincie"},
    "gemeente": {"gemeente"},
}

_MEERVOUD = {"waterschap": "waterschappen", "provincie": "provincies",
             "gemeente": "gemeenten"}

# Words that carry no identity when matching a name against an omschrijving.
_STOPWOORDEN = {
    "activiteit", "activiteiten", "gereguleerd", "in", "de", "het", "van",
    "en", "waterschapsverordening", "omgevingsverordening", "omgevingsplan",
} | set().union(*_SOORT_WOORDEN.values())

# The STTR flavours a functionele structuur can carry, keyed by the
# prefix of the regelbeheerobject identifier, with the label the status
# overview has always used for them.
STTR_TYPES = {
    "Conclusie": "Conclusie",
    "IndieningsvereistenMelding": "Melding",
    "IndieningsvereistenVergunning": "Aanvraag vergunning",
    "IndieningsvereistenInformatie": "Informatie",
    "Maatregelen": "Maatregelen",
}

# ``…T5`` twins mirror an activity for a scenario that publishes no STTR of
# its own.  Skipped by default so a run does not spend its budget confirming
# emptiness.
STANDAARD_OVERGESLAGEN_SUFFIX = ("T5",)


class CatalogusFout(RuntimeError):
    """The bestuursorgaan could not be found, or not unambiguously."""


class RijkNietOndersteund(CatalogusFout):
    """The national government: its regels are not in CVDR and its STTR not in a decentral tree."""


def niet_rijk(oin: str, roots: Iterable[str] = ()) -> None:
    """Refuse the national government: its OIN, or a root in a national (``.imow-mnre``) tree."""
    rijk = [r for r in roots if ".imow-mnre" in r]
    if oin == NATIONAAL_OIN or rijk:
        raise RijkNietOndersteund(
            f"OIN {oin}{' (' + ', '.join(rijk) + ')' if rijk else ''} is de rijksoverheid; "
            "alleen waterschappen, provincies en gemeenten worden ondersteund")


@dataclass(frozen=True)
class Bestuursorgaan:
    naam: str
    oin: str
    roots: tuple[str, ...]


@dataclass
class Activiteit:
    identifier: str
    omschrijving: str
    ouder: str = ""
    # STTR label ("Conclusie", "Melding", …) → functioneleStructuurRef
    regelbeheerobjecten: dict[str, str] = field(default_factory=dict)


def functionele_structuur_ref(oin: str, rbo_identifier: str) -> str:
    """The ref that ``/sttr/metadata`` is keyed on.

    Concatenation, not a lookup: the regelbeheerobject identifier is already
    ``<soort><activiteit>``, e.g. ``Conclusienl.imow-ws0636.activiteit.X``.
    """
    return (f"http://toepasbare-regels.omgevingswet.overheid.nl/{oin}"
            f"/id/concept/{rbo_identifier}")


def sttr_label(rbo: dict[str, Any], activiteit: str) -> str:
    """"Conclusie", "Melding", "Aanvraag vergunning" or "Informatie"."""
    identifier = rbo.get("identifier", "")
    soort = (identifier[:-len(activiteit)]
             if identifier.endswith(activiteit) else identifier)
    if soort in STTR_TYPES:
        return STTR_TYPES[soort]
    # A flavour this table does not know yet: fall back to what the RTR
    # itself calls it rather than dropping it.
    return ((rbo.get("toestemming") or {}).get("waarde")
            or rbo.get("typering") or soort)


# ── finding the bestuursorgaan ───────────────────────────────────────


def _tokens(tekst: str) -> set[str]:
    plat = unicodedata.normalize("NFKD", tekst or "")
    plat = "".join(c for c in plat if not unicodedata.combining(c))
    return set(re.findall(r"[a-z0-9]+", plat.casefold()))


def _kenmerkend(tekst: str) -> set[str]:
    return _tokens(tekst) - _STOPWOORDEN


def _soort(naam: str) -> str | None:
    woorden = _tokens(naam)
    for soort, kenmerk in _SOORT_WOORDEN.items():
        if woorden & kenmerk:
            return soort
    return None


def _roots_voor_oin(kinderen: Iterable[dict[str, Any]],
                    oin: str) -> tuple[str, ...]:
    return tuple(k["identifier"] for k in kinderen
                 if k.get("bestuursorgaanOin") == oin and k.get("identifier"))


def zoek_op_oin(client: RtrClient, oin: str, datum: str) -> Bestuursorgaan:
    """The bestuursorgaan behind *oin*, of any kind, gemeenten included; not the Rijk."""
    niet_rijk(oin)
    for soort, knoop in NATIONALE_KNOPEN.items():
        roots = _roots_voor_oin(client.relations(knoop, oin, datum), oin)
        if roots:
            niet_rijk(oin, roots)
            naam = client.details(roots[0], datum).get("bestuursOrgaanNaam")
            log.info("OIN %s: %s (%s), %d root(s)", oin, naam, soort,
                     len(roots))
            return Bestuursorgaan(naam or oin, oin, roots)
    raise CatalogusFout(
        f"Geen toepasbare activiteiten gevonden voor OIN {oin} op {datum}")


def zoek_op_naam(client: RtrClient, naam: str, datum: str) -> Bestuursorgaan:
    """A waterschap or provincie by name.

    First the cheap way, on the words that tell names apart in the root's
    omschrijving ("… waterschapsverordening De Stichtse Rijnlanden").  When
    that is not exactly one, each root's official bestuursOrgaanNaam is
    asked for — a few dozen requests, once.
    """
    soort = _soort(naam)
    if soort == "gemeente":
        raise CatalogusFout(
            "Gemeenten zijn in de RTR alleen op OIN te vinden: gebruik --oin "
            "(het OIN staat in het OIN-register van Logius)")
    soorten = [soort] if soort else ["waterschap", "provincie"]
    kinderen = [k for s in soorten
                for k in client.relations(NATIONALE_KNOPEN[s], NATIONAAL_OIN,
                                          datum)]

    gezocht = _kenmerkend(naam)
    kandidaten = {k["bestuursorgaanOin"] for k in kinderen
                  if gezocht and gezocht <= _kenmerkend(k.get("omschrijving"))}

    namen: dict[str, str] = {}
    if len(kandidaten) != 1:
        exact = _tokens(naam)
        for oin in sorted({k["bestuursorgaanOin"] for k in kinderen}):
            root = _roots_voor_oin(kinderen, oin)[0]
            namen[oin] = client.details(root, datum).get(
                "bestuursOrgaanNaam", "")
        kandidaten = {oin for oin, n in namen.items()
                      if _tokens(n) - _STOPWOORDEN == gezocht
                      or _tokens(n) == exact}
    if len(kandidaten) != 1:
        bekend = ", ".join(sorted(n for n in namen.values() if n))
        raise CatalogusFout(
            f"'{naam}' is {'niet' if not kandidaten else 'niet eenduidig'} "
            f"gevonden onder de {' en '.join(_MEERVOUD[s] for s in soorten)}. "
            f"Bekend: {bekend or '(geen)'}. Of gebruik --oin.")

    oin = kandidaten.pop()
    roots = _roots_voor_oin(kinderen, oin)
    gevonden = namen.get(oin) or client.details(roots[0], datum).get(
        "bestuursOrgaanNaam", naam)
    log.info("%s: OIN %s, %d root(s)", gevonden, oin, len(roots))
    return Bestuursorgaan(gevonden, oin, roots)


# ── the activity tree ────────────────────────────────────────────────


def _overslaan(identifier: str, suffixen: Iterable[str]) -> bool:
    return any(identifier.endswith(s) for s in suffixen)


def enumereer_activiteiten(
    client: RtrClient,
    bestuursorgaan: Bestuursorgaan,
    *,
    datum: str,
    overgeslagen_suffix: Iterable[str] = STANDAARD_OVERGESLAGEN_SUFFIX,
) -> list[Activiteit]:
    """Every activity of this bestuursorgaan, breadth-first from its roots.

    Only nodes of this bestuursorgaan (by OIN) are followed, so the walk
    stays inside its own tree and never wanders into the national one.  The
    walk asks each node for its children anyway, and that same answer says
    which regelbeheerobjecten the node carries — so the STTR flavours come
    for free.  Roots are included: they are activities in their own right.
    A root the RTR does not know on *datum* (HTTP 404: the tree started
    later, or was replaced) is skipped, as long as one of them is there.
    """
    oin = bestuursorgaan.oin
    suffixen = tuple(overgeslagen_suffix)
    gezien: set[str] = set()
    gevonden: dict[str, Activiteit] = {}
    ouders: dict[str, str] = {}
    wachtrij: deque[str] = deque(bestuursorgaan.roots)
    afwezig: list[str] = []

    while wachtrij:
        huidig = wachtrij.popleft()
        if huidig in gezien:
            continue
        gezien.add(huidig)
        try:
            knoop = client.relations_node(huidig, oin, datum)
        except RtrError as exc:
            if exc.status != 404 or huidig not in bestuursorgaan.roots:
                raise
            afwezig.append(huidig)
            log.info("Root %s bestaat niet op %s", huidig, datum)
            continue

        if not _overslaan(huidig, suffixen):
            rbos = {}
            for lijst in (knoop.get("regelBeheerObjecten") or {}).values():
                for rbo in lijst:
                    rbos[sttr_label(rbo, huidig)] = functionele_structuur_ref(
                        oin, rbo["identifier"])
            gevonden[huidig] = Activiteit(
                identifier=huidig,
                omschrijving=knoop.get("omschrijving", ""),
                # The RTR tree is a DAG; breadth-first order makes "first
                # parent wins" deterministic.
                ouder=ouders.get(huidig, ""),
                regelbeheerobjecten=rbos,
            )

        for kind in knoop.get("activiteiten", []):
            identifier = kind.get("identifier", "")
            if (kind.get("bestuursorgaanOin") != oin or not identifier
                    or identifier in gezien):
                continue
            ouders.setdefault(identifier, huidig)
            wachtrij.append(identifier)

    if bestuursorgaan.roots and len(afwezig) == len(set(bestuursorgaan.roots)):
        raise CatalogusFout(f"Geen van de roots van {bestuursorgaan.naam} bestaat op "
                            f"{datum}: {', '.join(afwezig)}")
    log.info("Activiteiten gevonden: %d (%d knopen bezocht)",
             len(gevonden), len(gezien))
    return sorted(gevonden.values(),
                  key=lambda a: (a.omschrijving.casefold(), a.identifier))

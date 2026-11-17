"""
RTR (Registratie Toepasbare Regels) archiving.

Reads everything from the public RTR viewer backend (see ``rtr_client``): no
API key and no PowerBI exports.  For one bestuursorgaan on one date it

- finds all its activities by walking its own activity tree,
- reads per activity: werkzaamheden, werkingsgebieden (with their names) and
  per STTR flavour the last change date,
- writes the Excel status overview with the werkingsgebieden matrix,
- optionally downloads every STTR (DMN XML) and writes a werkingsgebieden list.

Each activity is fetched exactly once and everything is keyed by its URN, so
a renamed activity or two activities sharing a name cannot lose or mix up
their werkingsgebieden.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import catalogus
from catalogus import Activiteit, Bestuursorgaan
from excel import ExcelHandler, generate_file_path
from rtr_client import BASE_URLS, RtrClient, RtrError

log = logging.getLogger("rtr")

# The status overview's "Wijziging …" columns, in this order; any other
# flavour the RTR turns up gets a column after these.
STTR_LABELS = ("Conclusie", "Melding", "Aanvraag vergunning", "Informatie", "Maatregelen")
AMBTSGEBIED = "Ambtsgebied"


@dataclass
class Gebied:
    identificatie: str
    noemer: str

    @property
    def label(self) -> str:
        """The column a gebied is shown under.

        The ambtsgebied is the whole territory rather than a werkingsgebied,
        so it gets one recognisable name whichever bestuursorgaan it is.
        """
        if ".ambtsgebied." in self.identificatie:
            return AMBTSGEBIED
        return (self.noemer or self.identificatie).strip()


@dataclass
class Sttr:
    sttr_id: str
    begindatum: str = ""
    laatste_wijziging: str = ""


@dataclass
class ActiviteitRecord:
    activiteit: Activiteit
    werkzaamheden: list[str] = field(default_factory=list)
    gebieden: list[Gebied] = field(default_factory=list)
    sttrs: dict[str, Sttr] = field(default_factory=dict)
    fout: str = ""

    @property
    def urn(self) -> str:
        return self.activiteit.identifier

    @property
    def naam(self) -> str:
        return self.activiteit.omschrijving or self.urn

    @property
    def urn_kort(self) -> str:
        return self.urn.split(".")[-1]


def iso_datum(datum: str) -> str:
    """dd-mm-jjjj → jjjj-mm-dd, the form the API documents."""
    return datetime.strptime(datum, "%d-%m-%Y").strftime("%Y-%m-%d")


class RTR:
    """Archive the toepasbare regels of one bestuursorgaan on one date."""

    def __init__(self, args, client: RtrClient | None = None):
        self.args = args
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.datum = iso_datum(args.date)
        self.client = client or RtrClient(BASE_URLS[args.env], rate=args.rate)
        self.bestuursorgaan = self._zoek_bestuursorgaan()

    def _zoek_bestuursorgaan(self) -> Bestuursorgaan:
        if self.args.oin:
            return catalogus.zoek_op_oin(self.client, self.args.oin, self.datum)
        return catalogus.zoek_op_naam(self.client, self.args.overheid, self.datum)

    # ── workflow ─────────────────────────────────────────────────────

    def archive_activities(self) -> list[ActiviteitRecord]:
        suffixen = () if self.args.include_t5 else catalogus.STANDAARD_OVERGESLAGEN_SUFFIX
        activiteiten = catalogus.enumereer_activiteiten(
            self.client, self.bestuursorgaan, datum=self.datum,
            overgeslagen_suffix=suffixen)

        records = []
        for nummer, activiteit in enumerate(activiteiten, 1):
            log.info("[%d/%d] %s", nummer, len(activiteiten), activiteit.omschrijving)
            records.append(self.lees_activiteit(activiteit))

        self.write_status(records)
        if self.args.sttr:
            self.archive_sttr_files(records)
        if self.args.location:
            self.write_werkingsgebieden_to_file(records)

        mislukt = [r for r in records if r.fout]
        log.info("Klaar: %d activiteiten, %d mislukt, %d verzoeken aan de RTR",
                 len(records), len(mislukt), self.client.requests_made)
        return records

    def lees_activiteit(self, activiteit: Activiteit) -> ActiviteitRecord:
        record = ActiviteitRecord(activiteit)
        try:
            details = self.client.details(activiteit.identifier, self.datum)
            record.werkzaamheden = list(details.get("werkzaamheden") or [])
            record.gebieden = [
                Gebied(loc.get("identificatie", ""), loc.get("noemer", ""))
                for loc in self.client.locaties(activiteit.identifier, self.datum)
                if loc.get("identificatie")]
            for label, ref in activiteit.regelbeheerobjecten.items():
                meta = self.client.sttr_metadata(ref, self.datum)
                if meta.get("sttrId"):
                    record.sttrs[label] = Sttr(
                        str(meta["sttrId"]), meta.get("begindatum", ""),
                        meta.get("laatsteWijzigingDatum", ""))
        except RtrError as exc:
            record.fout = str(exc)
            log.error("%s: %s", activiteit.identifier, exc)
        return record

    # ── outputs ──────────────────────────────────────────────────────

    def _pad(self, suffix: str, extension: str | None = None) -> str:
        return generate_file_path(self.args.date, self.bestuursorgaan.naam,
                                  self.args.env, suffix, self.base_dir, extension)

    @staticmethod
    def gebied_kolommen(records: list[ActiviteitRecord]) -> list[str]:
        """Matrix columns: Ambtsgebied first, then werkingsgebieden by name."""
        labels = {g.label for r in records for g in r.gebieden}
        return sorted(labels, key=lambda l: (l != AMBTSGEBIED, l.casefold()))

    def write_status(self, records: list[ActiviteitRecord]) -> str:
        kolommen = self.gebied_kolommen(records)
        soorten = list(STTR_LABELS) + sorted(
            {l for r in records for l in r.sttrs} - set(STTR_LABELS), key=str.casefold)
        pad = self._pad("status", "xlsx")
        excel = ExcelHandler(pad, kolommen, datetime.strptime(self.args.date, "%d-%m-%Y"),
                             change_labels=soorten)
        for row, record in enumerate(records, 2):
            labels = {g.label for g in record.gebieden}
            excel.write_activity_row(
                row,
                name=record.naam,
                urn=record.urn,
                werkzaamheden=record.werkzaamheden,
                changes=[record.sttrs[l].laatste_wijziging if l in record.sttrs else ""
                         for l in soorten],
                gebieden=[k in labels for k in kolommen],
                error=record.fout,
            )
        excel.close_workbook()
        log.info("Statusoverzicht: %s", pad)
        return pad

    def archive_sttr_files(self, records: list[ActiviteitRecord]) -> str:
        folder = self._pad("DMN-files")
        os.makedirs(folder, exist_ok=True)
        manifest = []
        for record in records:
            for label, sttr in record.sttrs.items():
                naam = f"STTR_{sttr.sttr_id}_{record.urn_kort}_{label.replace(' ', '_')}.xml"
                try:
                    inhoud = self.client.download_sttr(sttr.sttr_id)
                except RtrError as exc:
                    log.error("STTR %s (%s) niet opgehaald: %s", sttr.sttr_id, naam, exc)
                    continue
                # Bytes as published: an archive keeps the original.
                with open(os.path.join(folder, naam), "wb") as f:
                    f.write(inhoud)
                manifest.append({
                    "bestand": naam, "activiteit": record.urn,
                    "omschrijving": record.naam, "soort": label,
                    "sttrId": sttr.sttr_id, "begindatum": sttr.begindatum,
                    "laatsteWijzigingDatum": sttr.laatste_wijziging,
                })
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({
                "bestuursorgaan": self.bestuursorgaan.naam,
                "oin": self.bestuursorgaan.oin,
                "omgeving": self.args.env,
                "peildatum": self.args.date,
                "bron": self.client.base_url,
                "sttrs": manifest,
            }, f, ensure_ascii=False, indent=2)
        log.info("DMN-bestanden: %d in %s", len(manifest), folder)
        return folder

    @staticmethod
    def invert_werkingsgebied_mapping(records: list[ActiviteitRecord]):
        """gebied label → (identificaties, sorted activity names)."""
        ids: dict[str, set[str]] = defaultdict(set)
        activiteiten: dict[str, set[str]] = defaultdict(set)
        for record in records:
            for gebied in record.gebieden:
                ids[gebied.label].add(gebied.identificatie)
                activiteiten[gebied.label].add(record.naam)
        return {label: (sorted(ids[label]), sorted(activiteiten[label], key=str.casefold))
                for label in sorted(ids, key=lambda l: (l != AMBTSGEBIED, l.casefold()))}

    def write_werkingsgebieden_to_file(self, records: list[ActiviteitRecord]) -> str:
        pad = self._pad("werkingsgebieden", "txt")
        os.makedirs(os.path.dirname(pad), exist_ok=True)
        with open(pad, "w", encoding="utf-8") as f:
            for label, (identificaties, namen) in self.invert_werkingsgebied_mapping(records).items():
                f.write(f"{label} ({', '.join(identificaties)}):\n")
                for naam in namen:
                    f.write(f"  - {naam}\n")
                f.write("\n")
        log.info("Werkingsgebieden: %s", pad)
        return pad

"""Offline tests for the step zoeken: a fake stands in for the SRU, the metadata and CVDR.

    python -m unittest discover -s tests
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import date

BASIS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASIS, "code"))

import archief  # noqa: E402
import zoeken  # noqa: E402

WERK = "/akn/nl/act/ws9901/2023/WSV"
GIO = "/join/id/regdata/ws9901/2026/"
BO = "Hoogheemraadschap De Proefpolders"


def meta(titel, beschikbaar, **velden):
    uit = {"DC.title": [titel], "DCTERMS.available": [beschikbaar], "DC.creator": [BO],
           "OVERHEIDop.publicationName": ["Waterschapsblad"]}
    uit.update({f"OVERHEIDop.{k}": v if isinstance(v, list) else [v] for k, v in velden.items()})
    return uit


class FakeBron:
    def __init__(self, metadata, toestanden):
        self.meta, self.toestanden = metadata, toestanden
        self.queries = []

    def zoek(self, query):
        self.queries.append(query)
        return sorted(self.meta)

    def metadata(self, pub_id):
        return self.meta[pub_id]

    def geldend_van(self, cvdr, toestand):
        return self.toestanden.get(toestand)


def bestand():
    return {"bestuursorgaan": {"naam": BO, "oin": "99000000000000000001"},
            "regeling": {"cvdr": "CVDR1", "titel": "Waterschapsverordening De Proefpolders"},
            "besluiten": [{
                "besluitdatum": "2025-07-15", "naam": "wijziging", "in_werking": "2025-07-24",
                "zichtbaar_in_dso": {"besluit": "2025-07-24"},
                "definitief": [{"id": "wsb-2025-90002", "soort": "besluit", "titel": "Wijziging"}],
                "toestanden": [{"nummer": 5, "geldig_vanaf": "2025-07-24"}],
                "rtr_peildata": ["2025-08-01"]}]}


BEKEND = {"wsb-2025-90002": meta("Wijziging", "2025-07-24", betreftRegeling="CVDR1_5",
                                 betreftProefversie=WERK + "/nld@2025-07-01;V5")}
ONTWERP = {
    "wsb-2026-90010": meta("Ontwerp wijzigingsbesluit Waterschapsverordening", "2026-06-05",
                           betreftProefversie=WERK + "/nld@2026-05-13;V7", steltVast=[GIO + "WG_A/nld@2026-05-01;1"]),
    "wsb-2026-90011": meta("Kennisgeving ontwerp", "2026-06-08",
                           mededelingOver="/akn/nl/officialGazette/wsb/2026/90010/nld@2026-06-05"),
    "wsb-2026-90012": meta("Beleidsregels bij de Waterschapsverordening", "2026-06-08", betreftRegeling="CVDR2_3"),
    "wsb-2026-90013": meta("Start inspraak", "2026-06-08"),
    "wsb-2026-90014": meta("Mededeling", "2026-06-09", mededelingOver="/akn/nl/officialGazette/wsb/2026/1/nld@x"),
}
BESLUIT = {"wsb-2026-90020": meta("Wijzigingsbesluit Waterschapsverordening", "2026-09-29",
                                  betreftRegeling="CVDR1_6", datumOndertekening="2026-09-22",
                                  betreftProefversie=WERK + "/nld@2026-09-01;V7",
                                  steltVast=[GIO + "WG_A/nld@2026-08-31;2"])}


class Zoeken(unittest.TestCase):
    def zoek(self, ruw, metadata, toestanden=None, vandaag=date(2026, 11, 18)):
        bron = FakeBron(metadata, toestanden or {})
        return zoeken.zoek(ruw, vandaag, bron), bron

    def lees(self, ruw):
        """The file as archief reads it."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write(zoeken.schrijf(ruw))
        try:
            return archief.lees_config(f.name)
        finally:
            os.unlink(f.name)

    def test_ontwerp_en_kennisgeving(self):
        ruw = bestand()
        veranderd, bron = self.zoek(ruw, {**BEKEND, **ONTWERP})
        q = bron.queries[0]
        for deel in ('w.publicatienaam=="Waterschapsblad"', f'dt.creator=="{BO}"',
                     'dt.title any "Waterschapsverordening"', "dt.date>=2025-05-25"):
            self.assertIn(deel, q)
        self.assertEqual(ruw["besluiten"][1], {
            "naam": "wijzigingsbesluit", "zichtbaar_in_dso": {"ontwerp": "2026-06-05"},
            "ontwerp": [{"id": "wsb-2026-90010", "soort": "ontwerp", "titel": "Ontwerp wijzigingsbesluit Waterschapsverordening"},
                        {"id": "wsb-2026-90011", "soort": "kennisgeving", "titel": "Kennisgeving ontwerp"}]})
        self.assertEqual(len(veranderd), 2)  # the other regeling, the notice and the stray mededeling stay out
        c = self.lees(ruw)
        self.assertEqual(c.besluiten[1].map, "20260605_wijzigingsbesluit_wsb-2026-90010")

    def test_besluit_bij_ontwerp_en_in_werking(self):
        ruw = bestand()
        self.zoek(ruw, {**BEKEND, **ONTWERP})
        # The besluit is out, CVDR does not have its toestand yet.
        self.zoek(ruw, {**BEKEND, **ONTWERP, **BESLUIT})
        b = ruw["besluiten"][1]
        self.assertEqual((b["besluitdatum"], b["zichtbaar_in_dso"]),
                         ("2026-09-22", {"ontwerp": "2026-06-05", "besluit": "2026-09-29"}))
        self.assertEqual([d["id"] for d in b["definitief"]], ["wsb-2026-90020"])
        self.assertNotIn("in_werking", b)
        self.assertEqual(self.lees(ruw).besluiten[1].map, "20260922_wijzigingsbesluit_wsb-2026-90020")
        # Next run: CVDR has it; today is the peildatum, the earlier besluit keeps its own.
        veranderd, _ = self.zoek(ruw, {**BEKEND, **ONTWERP, **BESLUIT}, {6: date(2026, 10, 7)})
        self.assertEqual((b["in_werking"], b["toestanden"], b["rtr_peildata"]),
                         ("2026-10-07", [{"nummer": 6, "geldig_vanaf": "2026-10-07"}], ["2026-11-18"]))
        self.assertEqual(ruw["besluiten"][0]["rtr_peildata"], ["2025-08-01"])
        self.assertEqual(list(b), ["besluitdatum", "naam", "in_werking", "zichtbaar_in_dso", "ontwerp",
                                   "definitief", "toestanden", "rtr_peildata"])
        c = self.lees(ruw)
        self.assertEqual(c.besluiten[0].tot, date(2026, 10, 7))
        self.assertEqual(c.besluiten[1].peildata, [date(2026, 11, 18)])
        # Nothing new: nothing changes.
        tekst = zoeken.schrijf(ruw)
        veranderd, _ = self.zoek(ruw, {**BEKEND, **ONTWERP, **BESLUIT}, {6: date(2026, 10, 7)})
        self.assertEqual((veranderd, zoeken.schrijf(ruw)), ([], tekst))

    def test_peildatum_niet_voor_de_inwerkingtreding(self):
        ruw = bestand()
        self.zoek(ruw, {**BEKEND, **ONTWERP, **BESLUIT}, {6: date(2026, 10, 7)}, vandaag=date(2026, 10, 1))
        self.assertEqual(ruw["besluiten"][1]["in_werking"], "2026-10-07")
        self.assertNotIn("rtr_peildata", ruw["besluiten"][1])

    def test_peildatum_voor_het_volgende_besluit(self):
        ruw = bestand()
        del ruw["besluiten"][0]["rtr_peildata"]
        self.zoek(ruw, {**BEKEND, **BESLUIT}, {6: date(2026, 10, 7)})
        self.assertEqual(ruw["besluiten"][0]["rtr_peildata"], ["2026-10-06"])
        self.assertEqual(ruw["besluiten"][1]["naam"], "wijzigingsbesluit")  # no ontwerp: a new besluit
        self.assertNotIn("ontwerp", ruw["besluiten"][1])

    def test_herpublicatie(self):
        ruw = bestand()
        herpublicatie = {"wsb-2025-90003": meta("Wijziging (herpublicatie)", "2025-07-28", betreftRegeling="CVDR1_6",
                                                datumOndertekening="2025-07-15")}
        veranderd, _ = self.zoek(ruw, {**BEKEND, **herpublicatie}, {6: date(2025, 7, 29)})
        self.assertEqual(len(ruw["besluiten"]), 1)
        b = ruw["besluiten"][0]
        self.assertEqual([d["id"] for d in b["definitief"]], ["wsb-2025-90002", "wsb-2025-90003"])
        self.assertEqual(b["toestanden"], [{"nummer": 5, "geldig_vanaf": "2025-07-24"},
                                           {"nummer": 6, "geldig_vanaf": "2025-07-29"}])
        self.assertEqual(b["in_werking"], "2025-07-24")
        self.assertIn("2025-07-15: toestand 6", veranderd)

    def test_twee_ontwerpen_tegelijk(self):
        twee = {
            "wsb-2026-90030": meta("Ontwerp wijzigingsbesluit Waterschapsverordening", "2026-06-05",
                                   betreftProefversie=WERK + "/nld@2026-05-13;V7"),
            "wsb-2026-90031": meta("Ontwerp wijzigingsbesluit Waterschapsverordening 2024", "2026-06-05",
                                   betreftProefversie=WERK + "/nld@2026-05-13;V8",
                                   steltVast=[GIO + "WG_A/nld@2026-05-01;1", GIO + "WG_B/nld@2026-05-01;1"])}
        besluit = {"wsb-2026-90040": meta("Wijzigingsbesluit Waterschapsverordening hoofdstuk 2", "2026-09-29",
                                          betreftRegeling="CVDR1_6", datumOndertekening="2026-09-22",
                                          steltVast=[GIO + "WG_A/nld@2026-08-31;2", GIO + "WG_B/nld@2026-08-31;2"])}
        ruw = bestand()
        veranderd, _ = self.zoek(ruw, {**BEKEND, **twee, **besluit})
        # The same kaarten decide, not the order or the title.
        gekoppeld = next(b for b in ruw["besluiten"] if b.get("definitief") and b.get("ontwerp"))
        self.assertEqual(gekoppeld["ontwerp"][0]["id"], "wsb-2026-90031")
        self.assertIn("2 dezelfde kaarten", " ".join(veranderd))
        # Without kaarten it is the title, and a warning to check it.
        zonder = copy.deepcopy({**twee, **besluit})
        for m in zonder.values():
            m.pop("OVERHEIDop.steltVast", None)
        ruw = bestand()
        with self.assertLogs("rtr.zoeken", "WARNING") as logboek:
            self.zoek(ruw, {**BEKEND, **zonder})
        self.assertIn("controleer", logboek.output[0])

    def test_proefversie_geleerd_van_een_nieuw_besluit(self):
        ruw = bestand()
        oud = {"wsb-2025-90002": {k: v for k, v in BEKEND["wsb-2025-90002"].items()
                                  if k != "OVERHEIDop.betreftProefversie"}}  # from before STOP
        self.zoek(ruw, {**oud, **ONTWERP, **BESLUIT})
        b = ruw["besluiten"][1]
        self.assertEqual([d["id"] for d in b["ontwerp"]], ["wsb-2026-90010", "wsb-2026-90011"])
        self.assertEqual([d["id"] for d in b["definitief"]], ["wsb-2026-90020"])


class Bestand(unittest.TestCase):
    def test_hdsr_byte_voor_byte(self):
        pad = os.path.join(BASIS, "data", "archief_CVDR707248.json")
        with open(pad, encoding="utf-8", newline="") as f:
            tekst = f.read()
        self.assertEqual(zoeken.schrijf(json.loads(tekst)), tekst)

    def test_nieuwe_vormen(self):
        ruw = bestand()
        ruw["besluiten"].append({"naam": "wijzigingsbesluit", "zichtbaar_in_dso": {"ontwerp": "2026-06-05"},
                                 "ontwerp": [{"id": "wsb-2026-90010", "soort": "ontwerp", "titel": "Ontwerp é"}]})
        tekst = zoeken.schrijf(ruw)
        self.assertEqual(json.loads(tekst), ruw)
        self.assertIn('"ontwerp": [\n        {"id": "wsb-2026-90010", "soort": "ontwerp", "titel": "Ontwerp é"}\n      ]',
                      tekst)
        self.assertIn('"toestanden": [{"nummer": 5, "geldig_vanaf": "2025-07-24"}]', tekst)


class Stap(unittest.TestCase):
    """archief.zoek_nieuw: writes the file only when something was found, and goes on without the sources."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pad = os.path.join(tmp.name, "archief_CVDR1.json")
        with open(self.pad, "w", encoding="utf-8") as f:
            f.write(zoeken.schrijf(bestand()))
        bron = zoeken.Bron
        self.addCleanup(setattr, zoeken, "Bron", bron)

    def lees(self):
        with open(self.pad, encoding="utf-8") as f:
            return f.read()

    def test_schrijft_wat_gevonden_is(self):
        zoeken.Bron = lambda *_: FakeBron({**BEKEND, **ONTWERP}, {})
        with self.assertLogs("rtr.archief", "INFO"):
            self.assertTrue(archief.zoek_nieuw(self.pad, date(2026, 11, 18)))
        self.assertIn("wsb-2026-90010", self.lees())
        self.assertFalse(archief.zoek_nieuw(self.pad, date(2026, 11, 18)))

    def test_bron_onbereikbaar(self):
        voor = self.lees()

        class Kapot(FakeBron):
            def zoek(self, query):
                raise RuntimeError("SRU onbereikbaar")
        zoeken.Bron = lambda *_: Kapot(BEKEND, {})
        with self.assertLogs("rtr.archief", "WARNING"):
            self.assertFalse(archief.zoek_nieuw(self.pad, date(2026, 11, 18)))
        self.assertEqual(self.lees(), voor)


if __name__ == "__main__":
    unittest.main()

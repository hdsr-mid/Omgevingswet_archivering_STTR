"""Offline tests for the archive: where each STTR version is filed, and how
the version history is reconstructed.

    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import archief  # noqa: E402
from catalogus import Bestuursorgaan  # noqa: E402
from versiehistorie import Versiehistorie, maandelijks  # noqa: E402

BO = Bestuursorgaan("HDSR", "00000001802139097000", ("root",))


def config(besluiten):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"bestuursorgaan": {"naam": "HDSR", "oin": BO.oin, "root": "root"},
                   "regeling": {"cvdr": "CVDR1", "titel": "Verordening"},
                   "besluiten": besluiten}, f)
    try:
        return archief.lees_config(f.name)
    finally:
        os.unlink(f.name)


def besluit(besluitdatum, in_werking, peildata, bekendmaking):
    return {"besluitdatum": besluitdatum, "naam": "besluit", "in_werking": in_werking,
            "definitief": [{"id": bekendmaking, "soort": "besluit", "titel": "t"}],
            "rtr_peildata": peildata}


def versie(sttr_id, begin, eind=None):
    return {"sttrId": sttr_id, "begindatum": begin, "einddatum": eind,
            "laatsteWijzigingDatum": "x"}


class Config(unittest.TestCase):
    def test_mapnaam_en_perioden(self):
        c = config([besluit("2025-07-15", "2025-07-24", ["2025-08-08"], "wsb-2025-17997"),
                    besluit("2023-12-20", "2024-01-01", ["2024-01-15"], "wsb-2023-15782")])
        self.assertEqual([b.map for b in c.besluiten],
                         ["20231220_besluit_wsb-2023-15782", "20250715_besluit_wsb-2025-17997"])
        self.assertEqual(c.besluiten[0].tot, date(2025, 7, 24))
        self.assertIsNone(c.besluiten[1].tot)
        self.assertEqual(c.besluit_voor(date(2025, 7, 23)).map, "20231220_besluit_wsb-2023-15782")
        self.assertEqual(c.besluit_voor(date(2025, 7, 24)).map, "20250715_besluit_wsb-2025-17997")

    def test_peildatum_buiten_periode_is_fout(self):
        with self.assertRaisesRegex(ValueError, "buiten de periode"):
            config([besluit("2023-12-20", "2024-01-01", ["2025-08-08"], "wsb-1"),
                    besluit("2025-07-15", "2025-07-24", [], "wsb-2")])


class Verdeel(unittest.TestCase):
    """Every version is filed exactly under the besluit it took effect in."""

    def setUp(self):
        self.c = config([besluit("2023-12-20", "2024-01-01", ["2024-01-15"], "wsb-1"),
                         besluit("2024-12-17", "2025-01-01", ["2025-01-20"], "wsb-2")])
        self.b1, self.b2 = (b.map for b in self.c.besluiten)
        self.refs = {"ref-a": {"activiteit": "nl.x.activiteit.A", "label": "Conclusie", "versies": [
            versie("1", "01-01-2024", "04-01-2024"),   # first upload, gone before 15-01
            versie("2", "05-01-2024", "21-03-2024"),   # in the 15-01 snapshot
            versie("3", "22-03-2024", "02-01-2025"),   # later modification
            versie("4", "03-01-2025", None),           # next besluit, in its snapshot
        ]}, "ref-b": {"activiteit": "nl.x.activiteit.B", "label": "Melding", "versies": [
            versie("9", "05-01-2024", "30-06-2024"),   # in snapshot, then the object went
        ]}}
        self.snap = {self.b1: {"2", "9"}, self.b2: {"4"}}

    def test_alles_ergens(self):
        uit = archief.verdeel(self.c, self.refs, self.snap, date(2026, 11, 17))
        self.assertEqual(sorted(uit), [(self.b1, "20240101_gewijzigd"), (self.b1, "20240322_gewijzigd"),
                                       (self.b1, "20240701_gewijzigd")])
        self.assertEqual([v["sttrId"] for v in uit[(self.b1, "20240101_gewijzigd")]], ["1"])
        later = uit[(self.b1, "20240322_gewijzigd")][0]
        self.assertEqual((later["sttrId"], later["vervangt"], later["vervangen_door"]), ("3", "2", "4"))
        weg = uit[(self.b1, "20240701_gewijzigd")][0]
        self.assertEqual((weg["vervallen"], weg["laatste_versie"]), (True, "9"))
        gearchiveerd = {v["sttrId"] for items in uit.values() for v in items if not v.get("vervallen")}
        gearchiveerd |= set().union(*self.snap.values())
        self.assertEqual(gearchiveerd, {"1", "2", "3", "4", "9"})

    def test_getal_en_tekst_zijn_dezelfde_versie(self):
        # The API answers ids as numbers, manifests store text.
        refs = {ref: {**r, "versies": [{**v, "sttrId": int(v["sttrId"])} for v in r["versies"]]}
                for ref, r in self.refs.items()}
        uit = archief.verdeel(self.c, refs, self.snap, date(2026, 11, 17))
        self.assertEqual(sorted(uit), [(self.b1, "20240101_gewijzigd"), (self.b1, "20240322_gewijzigd"),
                                       (self.b1, "20240701_gewijzigd")])

    def test_versie_niet_in_eigen_vastlegging_krijgt_map(self):
        # The snapshot missed version 4 (e.g. its activity was not walked).
        uit = archief.verdeel(self.c, self.refs, {self.b1: {"2", "9"}}, date(2026, 11, 17))
        self.assertIn((self.b2, "20250103_gewijzigd"), uit)

    def test_later_gewijzigd_ook_als_volgende_vastlegging_hem_heeft(self):
        # Version 3 sits in besluit 2's snapshot too; under besluit 1 it is still
        # a later modification and gets its own dated folder.
        snap = {self.b1: {"2", "9"}, self.b2: {"3", "4"}}
        uit = archief.verdeel(self.c, self.refs, snap, date(2026, 11, 17))
        self.assertIn((self.b1, "20240322_gewijzigd"), uit)


class Verdwenen(unittest.TestCase):
    def test_activiteit_en_los_regelbeheerobject(self):
        h = {"walks": ["2024-01-01", "2024-02-01", "2024-03-01"],
             "activiteiten": {
                 "weg": {"omschrijving": {"2024-01-01": "grondwater onttrekken"}, "gezien_op": ["2024-01-01"]},
                 "blijft": {"omschrijving": {"2024-03-01": "duiker"},
                            "gezien_op": ["2024-01-01", "2024-02-01", "2024-03-01"]}},
             "refs": {
                 "r-weg": {"activiteit": "weg", "label": "Conclusie", "gezien_op": ["2024-01-01"]},
                 "r-los": {"activiteit": "blijft", "label": "Informatie", "gezien_op": ["2024-01-01"]},
                 "r-blijft": {"activiteit": "blijft", "label": "Conclusie",
                              "gezien_op": ["2024-01-01", "2024-02-01", "2024-03-01"]}}}
        uit = archief.verdwenen(h)
        self.assertEqual([(v["soort"], v["id"], v["eerst_weg"]) for v in uit],
                         [("activiteit", "weg", "2024-02-01"),
                          ("regelbeheerobject", "r-los", "2024-02-01")])


class FakeMetadata:
    """Answers /sttr/metadata from a list of (id, begin, eind) versions."""

    def __init__(self, versies, laatste_begin):
        self.versies = versies
        self.laatste_begin = laatste_begin
        self.calls = 0

    def sttr_metadata(self, ref, datum):
        self.calls += 1
        d = date.fromisoformat(datum)
        for sid, b, e in self.versies:
            if b <= d and (e is None or d <= e):
                return {"sttrId": sid, "begindatum": f"{b:%d-%m-%Y}",
                        "einddatum": e and f"{e:%d-%m-%Y}",
                        "laatsteWijzigingDatum": "x", "toekomstigeSttr": True,
                        "toekomstigeSttrBegindatum": f"{self.laatste_begin:%d-%m-%Y}"}
        later = [b for _, b, _ in self.versies if b > d]
        return {"toekomstigeSttr": bool(later),
                "toekomstigeSttrBegindatum": f"{self.laatste_begin:%d-%m-%Y}" if later else None}


class Keten(unittest.TestCase):
    def test_volgt_einddatum_en_springt_niet_naar_laatste(self):
        vs = [(1, date(2024, 1, 1), date(2024, 1, 4)), (2, date(2024, 1, 5), date(2025, 1, 16)),
              (3, date(2025, 1, 17), date(2025, 2, 12)),
              # a real gap of two weeks, then the latest version
              (4, date(2025, 3, 1), None)]
        fake = FakeMetadata(vs, laatste_begin=date(2025, 3, 1))
        gevonden = list(Versiehistorie._keten(fake, "r", date(2024, 1, 1), date(2026, 11, 17)))
        self.assertEqual([v["sttrId"] for v in gevonden], ["1", "2", "3", "4"])

    def test_nieuw_object_begint_na_vorige_ronde(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Versiehistorie(os.path.join(tmp, "h.json"), BO, "fake")
            h.data["walks"] = ["2024-01-01", "2025-06-01", "2025-07-01"]
            h.data["refs"]["r"] = {"activiteit": "a", "label": "Conclusie",
                                   "gezien_op": ["2025-07-01"], "versies": []}
            fake = FakeMetadata([(7, date(2025, 6, 20), None)], laatste_begin=date(2025, 6, 20))
            self.assertEqual(h.volg_ketens(fake, date(2024, 1, 1), date(2026, 11, 17)), 1)
            # Started at the 2025-06-01 walk, not at 2024-01-01: a few weekly steps.
            self.assertLessEqual(fake.calls, 5)

    def test_dubbele_versies_bij_laden_samengevoegd(self):
        with tempfile.TemporaryDirectory() as tmp:
            pad = os.path.join(tmp, "h.json")
            with open(pad, "w", encoding="utf-8") as f:
                json.dump({"walks": [], "activiteiten": {}, "refs": {"r": {
                    "activiteit": "a", "label": "Conclusie", "gezien_op": [],
                    "versies": [versie(70340, "05-01-2024"), versie("70340", "05-01-2024")]}}}, f)
            h = Versiehistorie(pad, BO, "fake")
            self.assertEqual([v["sttrId"] for v in h.data["refs"]["r"]["versies"]], ["70340"])
            self.assertFalse(h.voeg_toe("r", "a", "Conclusie", versie(70340, "05-01-2024")))

    def test_maandelijks(self):
        self.assertEqual(maandelijks(date(2025, 11, 17), date(2026, 2, 1)),
                         [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)])


if __name__ == "__main__":
    unittest.main()

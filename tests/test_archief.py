"""Offline tests for the archive: where each STTR version is filed, and how
the version history is reconstructed.

    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import archief  # noqa: E402
import catalogus  # noqa: E402
from catalogus import Bestuursorgaan  # noqa: E402
from versiehistorie import Versiehistorie, maandelijks  # noqa: E402

BO = Bestuursorgaan("Waterschap Voorbeeld", "99000000000000000001", ("root",))


def config(besluiten, bo=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"bestuursorgaan": bo or {"naam": BO.naam, "oin": BO.oin, "root": "root"},
                   "regeling": {"cvdr": "CVDR1", "titel": "Verordening"},
                   "besluiten": besluiten}, f)
    try:
        return archief.lees_config(f.name)
    finally:
        os.unlink(f.name)


def besluit(besluitdatum, in_werking, peildata, bekendmaking):
    return {"besluitdatum": besluitdatum, "naam": "besluit", "in_werking": in_werking,
            "definitief": [{"id": bekendmaking, "soort": "besluit", "titel": "t"}],
            "rtr_peildata": peildata, "zichtbaar_in_dso": {"besluit": besluitdatum}}


@contextlib.contextmanager
def stil():
    """Undo the logging set up by a main(): its handlers would print in every later test."""
    root = logging.getLogger()
    voor, niveau = list(root.handlers), root.level
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        for h in [h for h in root.handlers if h not in voor]:
            root.removeHandler(h)
            h.close()
        root.setLevel(niveau)


def versie(sttr_id, begin, eind=None):
    return {"sttrId": sttr_id, "begindatum": begin, "einddatum": eind,
            "laatsteWijzigingDatum": "x"}


class Config(unittest.TestCase):
    def test_mapnaam_en_perioden(self):
        c = config([besluit("2025-07-15", "2025-07-24", ["2025-08-08"], "wsb-2025-90002"),
                    besluit("2023-12-20", "2024-01-01", ["2024-01-15"], "wsb-2023-90001")])
        self.assertEqual([b.map for b in c.besluiten],
                         ["20231220_besluit_wsb-2023-90001", "20250715_besluit_wsb-2025-90002"])
        self.assertEqual(c.besluiten[0].tot, date(2025, 7, 24))
        self.assertIsNone(c.besluiten[1].tot)
        self.assertEqual(c.besluit_voor(date(2025, 7, 23)).map, "20231220_besluit_wsb-2023-90001")
        self.assertEqual(c.besluit_voor(date(2025, 7, 24)).map, "20250715_besluit_wsb-2025-90002")

    def test_mappen_heten_naar_de_dag_zichtbaar_in_dso(self):
        b = besluit("2023-12-20", "2024-01-01", [], "wsb-1")
        b["ontwerp"] = [{"id": "wsb-0", "soort": "ontwerp", "titel": "o"}]
        with self.assertRaisesRegex(ValueError, "zichtbaar_in_dso.ontwerp ontbreekt"):
            config([b])
        b["zichtbaar_in_dso"] = {"ontwerp": "2023-08-29", "besluit": "2023-12-21"}
        self.assertEqual([config([b]).besluiten[0].submap(f) for f in ("Ontwerp", "Definitief")],
                         ["Ontwerp_20230829", "Definitief_20231221"])
        del b["zichtbaar_in_dso"]["besluit"]
        with self.assertRaisesRegex(ValueError, "zichtbaar_in_dso.besluit ontbreekt"):
            config([b])

    def test_oude_mappen_krijgen_hun_datum(self):
        # Without a date (first layout) or with another one (an earlier choice of date).
        b = besluit("2023-12-20", "2024-01-01", [], "wsb-1")
        b["zichtbaar_in_dso"] = {"ontwerp": "2023-08-29", "besluit": "2023-12-21"}
        b["ontwerp"] = [{"id": "wsb-0", "soort": "ontwerp", "titel": "o"}]
        c = config([b])
        with tempfile.TemporaryDirectory() as tmp:
            a = archief.Archief(c, tmp, client=None)
            os.makedirs(a.map(c.besluiten[0], "Ontwerp_20230829", "bijlagen"))
            os.makedirs(a.map(c.besluiten[0], "Definitief"))
            with open(a.map(c.besluiten[0], "bijlagen.json"), "w", encoding="utf-8") as f:
                json.dump({"publicaties": {"wsb-0": {"bijlagen": [
                    {"bestand": "Ontwerp_20230829/bijlagen/x.pdf"},
                    {"bestand": "Ontwerp_20230829/bijlagen/y.pdf",
                     "identiek_aan": "Ontwerp_20230829/bijlagen/x.pdf"},
                    {"bestand": None}]}}}, f)
            a.hernoem_mappen()
            self.assertEqual(sorted(os.listdir(a.map(c.besluiten[0]))),
                             ["Definitief_20231221", "Ontwerp_20230829", "bijlagen.json"])
            with open(a.map(c.besluiten[0], "bijlagen.json"), encoding="utf-8") as f:
                x = json.load(f)["publicaties"]["wsb-0"]["bijlagen"]
            self.assertEqual([x[0]["bestand"], x[1]["identiek_aan"], x[2]["bestand"]],
                             ["Ontwerp_20230829/bijlagen/x.pdf", "Ontwerp_20230829/bijlagen/x.pdf", None])

    def test_blad_per_bestuurslaag(self):
        self.assertEqual(archief.blad("wsb-2025-90002"), "Waterschapsblad")
        self.assertEqual(archief.blad("prb-2025-90003"), "Provinciaal blad")
        self.assertEqual(archief.blad("gmb-2025-90004"), "Gemeenteblad")
        for pub_id in ("bgr-2025-90006", "wsb2025"):
            with self.assertRaises(archief.ConfigFout):
                archief.blad(pub_id)
        for pub_id in ("stcrt-2025-90005", "stb-2025-90006"):
            with self.assertRaises(catalogus.RijkNietOndersteund):
                archief.blad(pub_id)

    def test_onbekend_blad_is_fout(self):
        b = besluit("2025-07-15", "2025-07-24", [], "wsb-2025-90002")
        b["ontwerp"] = [{"id": "xyz-2025-90001", "soort": "ontwerp", "titel": "t"}]
        b["zichtbaar_in_dso"]["ontwerp"] = "2025-05-14"
        with self.assertRaisesRegex(ValueError, "xyz-2025-90001: onbekend blad 'xyz'"):
            config([b])

    def test_peildatum_buiten_periode_is_fout(self):
        with self.assertRaisesRegex(ValueError, "buiten de periode"):
            config([besluit("2023-12-20", "2024-01-01", ["2025-08-08"], "wsb-1"),
                    besluit("2025-07-15", "2025-07-24", [], "wsb-2")])


class Exitcodes(unittest.TestCase):
    """2: the config cannot be used; 3: the national government, which is not supported."""

    def schrijf(self, inhoud):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pad = os.path.join(tmp.name, "archief_CVDR1.json")
        with open(pad, "w", encoding="utf-8") as f:
            f.write(inhoud if isinstance(inhoud, str) else json.dumps(inhoud))
        return pad

    def goed(self, **wijzig):
        ruw = {"bestuursorgaan": {"naam": BO.naam, "oin": BO.oin, "root": "nl.imow-ws9901.activiteit.x"},
               "regeling": {"cvdr": "CVDR1", "titel": "Verordening"},
               "besluiten": [besluit("2025-07-15", "2025-07-24", [], "wsb-2025-90002")]}
        for sleutel, waarde in wijzig.items():
            deel, _, veld = sleutel.partition("__")
            if veld:
                ruw[deel][veld] = waarde
            else:
                ruw[deel] = waarde
        return ruw

    def test_onbruikbare_config(self):
        kapot_besluit = besluit("2025-07-15", "15-07-2025", [], "wsb-2025-90002")
        zonder_naam = besluit("2025-07-15", "2025-07-24", [], "wsb-2025-90002")
        del zonder_naam["naam"]
        for naam, inhoud, melding in (
                ("geen JSON", "{", "geen geldige JSON"),
                ("sleutel ontbreekt", self.goed(besluiten=[zonder_naam]), "'naam' ontbreekt"),
                ("datum", self.goed(besluiten=[kapot_besluit]), "Invalid isoformat"),
                ("geen cvdr", self.goed(regeling={"titel": "t"}), "CVDR-nummer"),
                ("omgeving", self.goed(omgeving="test"), "omgeving moet"),
                ("blad", self.goed(besluiten=[besluit("2025-07-15", "2025-07-24", [], "abc-2025-1")]),
                 "onbekend blad")):
            with self.subTest(naam), self.assertRaisesRegex(archief.ConfigFout, melding):
                archief.lees_config(self.schrijf(inhoud))
        with self.assertRaisesRegex(archief.ConfigFout, "No such file"):
            archief.lees_config("bestaat/niet.json")

    def test_rijk(self):
        for naam, ruw in (
                ("staatscourant", self.goed(besluiten=[besluit("2025-07-15", "2025-07-24", [], "stcrt-2025-1")])),
                ("bwb", self.goed(regeling={"bwb": "BWBR0047052", "titel": "Omgevingsbesluit"})),
                ("bwb als cvdr", self.goed(regeling={"cvdr": "BWBR0047052", "titel": "t"})),
                ("nationaal oin", self.goed(bestuursorgaan__oin=catalogus.NATIONAAL_OIN)),
                ("nationale root", self.goed(bestuursorgaan__root="nl.imow-mnre1034.activiteit.x"))):
            with self.subTest(naam), self.assertRaises(catalogus.RijkNietOndersteund):
                archief.lees_config(self.schrijf(ruw))

    def draai(self, ruw):
        with tempfile.TemporaryDirectory() as uit, tempfile.TemporaryDirectory() as werk, stil():
            return archief.main([self.schrijf(ruw), "--uit", uit, "--werkmap", werk,
                                 "--cache", os.path.join(werk, "cache"), "--stappen", "index"])

    def test_main_geeft_de_code(self):
        self.assertEqual(self.draai("{"), 2)
        self.assertEqual(self.draai(self.goed(regeling={"bwb": "BWBR0047052", "titel": "t"})), 3)
        with self.assertRaises(SystemExit) as fout, stil():  # argparse: an unusable option is 2 too
            archief.main(["x.json", "--tot", "18-11-2026"])
        self.assertEqual(fout.exception.code, 2)


class Roots(unittest.TestCase):
    """The activity tree's roots: from the config, or from the OIN."""

    class FakeCatalogus:
        """Answers the national node for a waterschap whose tree was replaced on 2025-07-24."""

        def __init__(self, roots_op):
            self.roots_op, self.gevraagd = roots_op, []

        def relations(self, knoop, oin, datum):
            self.gevraagd.append(datum)
            if knoop != catalogus.NATIONALE_KNOPEN["waterschap"]:
                return []
            return [{"identifier": r, "bestuursorgaanOin": oin} for r in self.roots_op.get(datum, [])]

        def details(self, identifier, datum):
            return {"bestuursOrgaanNaam": BO.naam}

    def maak(self, bo, roots_op):
        c = config([besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001"),
                    besluit("2025-07-15", "2025-07-24", [], "wsb-2025-90002")],
                   bo=dict(naam=BO.naam, oin=BO.oin, **bo))
        client = self.FakeCatalogus(roots_op)
        return archief.Archief(c, "nergens", client), client

    def test_root_roots_of_geen(self):
        for bo, verwacht in (({"root": "r1"}, ("r1",)), ({"roots": ["r1", "r2"]}, ("r1", "r2")),
                             ({"root": "r1", "roots": ["r2", "r1"]}, ("r1", "r2")),
                             ({"roots": "r1"}, ("r1",)), ({}, ())):
            with self.subTest(bo):
                c = config([besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")],
                           bo=dict(naam=BO.naam, oin=BO.oin, **bo))
                self.assertEqual(c.bestuursorgaan.roots, verwacht)

    def test_uit_het_oin_op_elke_inwerkingtreding(self):
        a, client = self.maak({}, {"2024-01-01": ["r1"], "2025-07-24": ["r1", "r2"], "2026-11-18": ["r2"]})
        a.vind_roots(date(2026, 11, 18))
        self.assertEqual(a.config.bestuursorgaan.roots, ("r1", "r2"))
        self.assertEqual(set(client.gevraagd), {"2024-01-01", "2025-07-24", "2026-11-18"})

    def test_datum_zonder_boom_wordt_overgeslagen(self):
        a, _ = self.maak({}, {"2026-11-18": ["r2"]})
        a.vind_roots(date(2026, 11, 18))
        self.assertEqual(a.config.bestuursorgaan.roots, ("r2",))

    def test_niet_na_tot(self):
        a, client = self.maak({}, {"2024-01-01": ["r1"], "2025-07-24": ["r2"]})
        a.vind_roots(date(2025, 1, 1))
        self.assertEqual(a.config.bestuursorgaan.roots, ("r1",))
        self.assertNotIn("2025-07-24", client.gevraagd)

    def test_geen_boom_is_fout(self):
        a, _ = self.maak({}, {})
        with self.assertRaisesRegex(archief.ConfigFout, "zet 'root'"):
            a.vind_roots(date(2026, 11, 18))

    def test_root_in_config_vraagt_niets(self):
        a, client = self.maak({"root": "r1"}, {"2026-11-18": ["r2"]})
        a.vind_roots(date(2026, 11, 18))
        self.assertEqual(a.config.bestuursorgaan.roots, ("r1",))
        self.assertEqual(client.gevraagd, [])


def alleen_ontwerp(zichtbaar, ontwerp_id, naam="wijzigingsbesluit"):
    return {"naam": naam, "zichtbaar_in_dso": {"ontwerp": zichtbaar},
            "ontwerp": [{"id": ontwerp_id, "soort": "ontwerp", "titel": "Ontwerp"}]}


def maak_bestand(pad, inhoud=b"%PDF-1.4"):
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    with open(pad, "wb") as f:
        f.write(inhoud)


class Onvolledig(unittest.TestCase):
    """A besluit with only an ontwerp, or not yet in werking."""

    def test_alleen_ontwerp(self):
        c = config([besluit("2025-07-15", "2025-07-24", ["2025-08-01"], "wsb-2025-90002"),
                    alleen_ontwerp("2026-06-05", "wsb-2026-90010")])
        self.assertEqual([b.map for b in c.besluiten],
                         ["20250715_besluit_wsb-2025-90002", "20260605_wijzigingsbesluit_wsb-2026-90010"])
        self.assertIsNone(c.besluiten[0].tot)  # an ontwerp does not end a period
        self.assertEqual(c.besluit_voor(date(2026, 12, 1)), c.besluiten[0])

    def test_besluit_nog_niet_in_werking(self):
        b = besluit("2026-09-22", "2026-10-07", [], "wsb-2026-90020")
        del b["in_werking"]
        c = config([besluit("2025-07-15", "2025-07-24", [], "wsb-2025-90002"), b])
        self.assertEqual(c.besluiten[1].map, "20260922_besluit_wsb-2026-90020")
        self.assertIsNone(c.besluiten[0].tot)
        self.assertFalse(c.besluiten[1].bevat(date(2026, 12, 1)))
        self.assertEqual(c.in_werking, c.besluiten[:1])

    def test_geen_besluit_in_werking(self):
        c = config([alleen_ontwerp("2026-06-05", "wsb-2026-90010")])
        self.assertIsNone(c.besluit_voor(date(2026, 12, 1)))
        self.assertEqual(archief.verdeel(c, Verdeel.refs(), {}, date(2026, 12, 1)), {})

    def test_onvolledig_is_fout(self):
        zonder_besluit = dict(alleen_ontwerp("2026-06-05", "wsb-2026-90010"), in_werking="2026-10-07")
        peil = dict(alleen_ontwerp("2026-06-05", "wsb-2026-90010"), rtr_peildata=["2026-10-10"])
        zonder_datum = besluit("2026-09-22", "2026-10-07", [], "wsb-2026-90020")
        del zonder_datum["besluitdatum"]
        leeg = {"naam": "x"}
        for naam, b, melding in (("in werking zonder besluit", zonder_besluit, "in_werking zonder"),
                                 ("peildata zonder in werking", peil, "in_werking ontbreekt"),
                                 ("besluit zonder datum", zonder_datum, "besluitdatum ontbreekt"),
                                 ("niets", leeg, "geen ontwerp en geen definitief")):
            with self.subTest(naam), self.assertRaisesRegex(archief.ConfigFout, melding):
                config([b])


class Compleet(unittest.TestCase):
    """What is complete is not fetched again; an ontwerp folder follows its besluit."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.uit = tmp.name
        b = besluit("2025-07-15", "2025-07-24", ["2025-08-01"], "wsb-2025-90002")
        b["zichtbaar_in_dso"] = {"ontwerp": "2025-05-14", "besluit": "2025-07-24"}
        b["ontwerp"] = [{"id": "wsb-2025-90001", "soort": "ontwerp", "titel": "o"}]
        b["toestanden"] = [{"nummer": 5, "geldig_vanaf": "2025-07-24"}]
        self.c = config([b])
        self.b = self.c.besluiten[0]
        self.a = archief.Archief(self.c, self.uit, client=None)
        m = lambda *d: os.path.join(self.uit, self.b.map, *d)  # noqa: E731
        maak_bestand(m("Ontwerp_20250514", "wsb-2025-90001_ontwerp.pdf"))
        maak_bestand(m("Definitief_20250724", "wsb-2025-90002_besluit.pdf"))
        maak_bestand(m("Definitief_20250724", "regeling_toestand-5_geldend-vanaf-20250724.pdf"))
        maak_bestand(m("RTR", "20250801_volledig", "STTR", "manifest.json"), b'{"sttrs": []}')
        maak_bestand(m("kaarten", "WG_1.gml"), b"<gml/>")
        maak_bestand(m("bijlagen.json"), json.dumps({"publicaties": {
            "wsb-2025-90001": {"bijlagen": []},
            "wsb-2025-90002": {"bijlagen": [{"soort": "kaart", "bestand": "kaarten/WG_1.gml", "bytes": 6}]}}}
        ).encode())
        self.m = m

    def test_compleet_en_wat_ontbreekt(self):
        self.assertTrue(self.a.compleet(self.b))
        for rel in (("Ontwerp_20250514", "wsb-2025-90001_ontwerp.pdf"),
                    ("Definitief_20250724", "regeling_toestand-5_geldend-vanaf-20250724.pdf"),
                    ("RTR", "20250801_volledig", "STTR", "manifest.json"), ("kaarten", "WG_1.gml"),
                    ("bijlagen.json",)):
            with self.subTest(rel):
                with open(self.m(*rel), "rb") as f:
                    bewaard = f.read()
                os.remove(self.m(*rel))
                self.assertFalse(self.a.compleet(self.b))
                maak_bestand(self.m(*rel), bewaard)
        maak_bestand(self.m("kaarten", "WG_1.gml"), b"<gml>anders</gml>")  # another size
        self.assertFalse(self.a.compleet(self.b))

    def test_compleet_wordt_niet_opnieuw_gehaald(self):
        def niet(*_, **__):
            raise AssertionError("niets ophalen voor een compleet besluit")
        originelen = archief.haal, archief.publicatie_metadata
        self.addCleanup(lambda: (setattr(archief, "haal", originelen[0]),
                                 setattr(archief, "publicatie_metadata", originelen[1])))
        archief.haal = archief.publicatie_metadata = niet
        self.a.voorbereiden()
        self.assertEqual(self.a.af, {self.b.map})
        self.a.publicaties()
        self.a.bijlagen()
        self.a.regeling(None)
        self.a.rtr()
        self.assertEqual(self.a.gewijzigd, set())

    def test_ontwerpmap_krijgt_de_naam_van_het_besluit(self):
        vroeger = config([alleen_ontwerp("2026-06-05", "wsb-2026-90010")]).besluiten[0]
        maak_bestand(os.path.join(self.uit, vroeger.map, "Ontwerp_20260605", "wsb-2026-90010_ontwerp.pdf"))
        b = dict(alleen_ontwerp("2026-06-05", "wsb-2026-90010", "wijziging-h2"), besluitdatum="2026-09-22",
                 definitief=[{"id": "wsb-2026-90020", "soort": "besluit", "titel": "b"}])
        b["zichtbaar_in_dso"]["besluit"] = "2026-09-29"
        a = archief.Archief(config([b]), self.uit, client=None)
        a.voorbereiden()
        nu = a.config.besluiten[0].map
        self.assertEqual(nu, "20260922_wijziging-h2_wsb-2026-90020")
        self.assertTrue(os.path.exists(os.path.join(self.uit, nu, "Ontwerp_20260605", "wsb-2026-90010_ontwerp.pdf")))
        self.assertFalse(os.path.exists(os.path.join(self.uit, vroeger.map)))
        self.assertIn(nu, a.gewijzigd)
        # The folder of another besluit is never taken, even when the id fits.
        self.assertTrue(os.path.exists(self.m("Ontwerp_20250514")))


class WijzigingenAanvullen(unittest.TestCase):
    """A complete besluit only gets new <date>_gewijzigd folders; what is there stays."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.uit = tmp.name
        self.c = config([besluit("2023-12-20", "2024-01-01", ["2024-01-15"], "wsb-1"),
                         besluit("2024-12-17", "2025-01-01", ["2025-01-20"], "wsb-2")])
        self.b1, self.b2 = (b.map for b in self.c.besluiten)
        client = SimpleNamespace(base_url="fake", download_sttr=lambda i: f"<dmn id='{i}'/>".encode())
        self.a = archief.Archief(self.c, self.uit, client)
        self.refs = Verdeel.refs()
        self.a.historie = lambda tot: SimpleNamespace(data={"refs": self.refs}, omschrijving=lambda x: x)
        for besluitmap, dag, ids in ((self.b1, "20240115", ["2", "9"]), (self.b2, "20250120", ["4"])):
            maak_bestand(os.path.join(self.uit, besluitmap, "RTR", f"{dag}_volledig", "STTR", "manifest.json"),
                         json.dumps({"sttrs": [{"sttrId": i} for i in ids]}).encode())
        # From an earlier run: one folder in each besluit that is no longer planned.
        self.oud = {b: os.path.join(self.uit, b, "RTR", dag, "STTR", "manifest.json")
                    for b, dag in ((self.b1, "20240201_gewijzigd"), (self.b2, "20250105_gewijzigd"))}
        for pad in self.oud.values():
            maak_bestand(pad, b'{"sttrs": [{"sttrId": "5"}]}')

    def mappen(self, besluitmap):
        return sorted(os.listdir(os.path.join(self.uit, besluitmap, "RTR")))

    def test_compleet_alleen_aanvullen(self):
        self.a.af = {self.b2}
        self.refs["ref-a"]["versies"][-1]["einddatum"] = "28-02-2025"
        self.refs["ref-a"]["versies"].append(versie("6", "01-03-2025"))  # later, open period of b2
        self.a.wijzigingen(date(2026, 11, 17))
        # Incomplete b1: the step owns its folders, so the stale one went.
        self.assertEqual(self.mappen(self.b1), ["20240101_gewijzigd", "20240115_volledig",
                                                "20240322_gewijzigd", "20240701_gewijzigd"])
        # Complete b2: the stale folder stays as it was, the new one is added.
        self.assertEqual(self.mappen(self.b2), ["20250105_gewijzigd", "20250120_volledig", "20250301_gewijzigd"])
        with open(self.oud[self.b2], "rb") as f:
            self.assertEqual(f.read(), b'{"sttrs": [{"sttrId": "5"}]}')
        self.assertEqual(self.a.gewijzigd, {self.b1, self.b2})
        # The same history again changes nothing.
        self.a.gewijzigd = set()
        self.a.wijzigingen(date(2026, 11, 17))
        self.assertEqual(self.a.gewijzigd, set())

    def test_bestaande_map_van_compleet_besluit_blijft(self):
        self.a.af = {self.b2}
        bestaand = os.path.join(self.uit, self.b2, "RTR", "20250301_gewijzigd", "STTR", "manifest.json")
        maak_bestand(bestaand, b'{"sttrs": []}')
        self.refs["ref-a"]["versies"][-1]["einddatum"] = "28-02-2025"
        self.refs["ref-a"]["versies"].append(versie("6", "01-03-2025"))
        with self.assertLogs("rtr.archief", "WARNING") as logboek:
            self.a.wijzigingen(date(2026, 11, 17))
        self.assertIn("niet toegevoegd: 6", logboek.output[0])
        with open(bestaand, "rb") as f:
            self.assertEqual(f.read(), b'{"sttrs": []}')


class Verdeel(unittest.TestCase):
    """Every version is filed exactly under the besluit it took effect in."""

    @staticmethod
    def refs():
        return {"ref-a": {"activiteit": "nl.x.activiteit.A", "label": "Conclusie", "versies": [
            versie("1", "01-01-2024", "04-01-2024"),   # first upload, gone before 15-01
            versie("2", "05-01-2024", "21-03-2024"),   # in the 15-01 snapshot
            versie("3", "22-03-2024", "02-01-2025"),   # later modification
            versie("4", "03-01-2025", None),           # next besluit, in its snapshot
        ]}, "ref-b": {"activiteit": "nl.x.activiteit.B", "label": "Melding", "versies": [
            versie("9", "05-01-2024", "30-06-2024"),   # in snapshot, then the object went
        ]}}

    def setUp(self):
        self.c = config([besluit("2023-12-20", "2024-01-01", ["2024-01-15"], "wsb-1"),
                         besluit("2024-12-17", "2025-01-01", ["2025-01-20"], "wsb-2")])
        self.b1, self.b2 = (b.map for b in self.c.besluiten)
        self.refs = self.refs()
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


GML = (b'<?xml version="1.0"?><geo:GeoInformatieObjectVaststelling '
       b'xmlns:geo="https://standaarden.overheid.nl/stop/imop/geo/"><geo:vastgesteldeVersie>'
       b'<geo:GeoInformatieObjectVersie><geo:FRBRWork>/join/id/regdata/ws9901/2023/WG_Oost</geo:FRBRWork>'
       b'<geo:FRBRExpression>/join/id/regdata/ws9901/2023/WG_Oost/nld@2024-12-02;1004</geo:FRBRExpression>'
       b'</geo:GeoInformatieObjectVersie></geo:vastgesteldeVersie></geo:GeoInformatieObjectVaststelling>')


class Bijlagen(unittest.TestCase):
    def test_bestandsnaam_van_informatieobject(self):
        self.assertEqual(archief.io_bestandsnaam("/join/id/regdata/ws9901/2023/WG_Oost/nld@2024-12-02;1004"),
                         "WG_Oost_2024-12-02_1004")
        self.assertEqual(archief.io_bestandsnaam("/join/id/regdata/ws9901/2023/WG_08/nld@2023-12-01"),
                         "WG_08_2023-12-01")
        self.assertEqual(archief.io_bestandsnaam("/join/id/regdata/ws9901/2023/WG_Oost/nld@2023-06-02;ontwerp"),
                         "WG_Oost_2023-06-02_ontwerp")

    def test_metadata_meerdere_waarden_en_genest(self):
        m = archief.lees_metadata(
            b'<metadata_gegevens><metadata name="OVERHEIDop.steltVast" content="/join/a"/>'
            b'<metadata name="OVERHEIDop.steltVast" content="/join/b"/>'
            b'<metadata name="OVERHEIDop.gebiedsmarkering" content="Waterschap">'
            b'<metadata name="OVERHEIDop.waterschapsnaam" content="Voorbeeld"/></metadata>'
            b'<metadata name="OVERHEIDop.externeBijlage" content="Legger|exb-2023-1"/></metadata_gegevens>')
        self.assertEqual(m["OVERHEIDop.steltVast"], ["/join/a", "/join/b"])
        self.assertEqual(m["OVERHEIDop.waterschapsnaam"], ["Voorbeeld"])
        self.assertEqual(m["OVERHEIDop.externeBijlage"], ["Legger|exb-2023-1"])

    def test_gml_noemt_zijn_versie(self):
        self.assertEqual(archief.frbr_expressie(GML),
                         "/join/id/regdata/ws9901/2023/WG_Oost/nld@2024-12-02;1004")


def gml(join):
    return (b'<?xml version="1.0"?><geo:GeoInformatieObjectVaststelling '
            b'xmlns:geo="https://standaarden.overheid.nl/stop/imop/geo/"><geo:vastgesteldeVersie>'
            b'<geo:GeoInformatieObjectVersie><geo:FRBRExpression>' + join.encode() +
            b'</geo:FRBRExpression></geo:GeoInformatieObjectVersie></geo:vastgesteldeVersie>'
            b'</geo:GeoInformatieObjectVaststelling>')


WG = "/join/id/regdata/ws9901/2023/"
TITELS = {"WG_Oost": "Oost", "WG_GA": "Groot afvoerdebiet", "WG_Grote_Afvoer": "Groot afvoerdebiet",
          "WG_Alleen_Ontwerp": "Vervallen gebied"}


class BijlagenOpslaan(unittest.TestCase):
    """The bijlagen step, with the network replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        b = besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")
        b["zichtbaar_in_dso"] = {"ontwerp": "2023-08-29", "besluit": "2023-12-21"}
        b["ontwerp"] = [{"id": "wsb-2023-90011", "soort": "ontwerp", "titel": "o"},
                        {"id": "wsb-2023-90012", "soort": "kennisgeving", "titel": "k1"},
                        {"id": "wsb-2023-90013", "soort": "kennisgeving", "titel": "k2"}]
        self.c = config([b])
        self.a = archief.Archief(self.c, self.tmp.name, client=None)
        self.besluitmap = os.path.join(self.tmp.name, self.c.besluiten[0].map)
        self.metadata = {
            "wsb-2023-90011": {"OVERHEIDop.steltVast": [
                WG + "WG_Oost/nld@2023-06-02;ontwerp",          # same GIO in the besluit
                WG + "WG_Grote_Afvoer/nld@2023-06-02;ontwerp",   # renamed WG_GA, same title
                WG + "WG_Alleen_Ontwerp/nld@2023-06-02;ontwerp"]},  # not in the besluit
            "wsb-2023-90012": {"OVERHEIDop.externeBijlage": ["Legger|exb-1"]},
            "wsb-2023-90013": {"OVERHEIDop.externeBijlage": ["Legger|exb-2"]},
            "wsb-2023-90001": {"OVERHEIDop.steltVast": [WG + "WG_Oost/nld@2023-12-01",
                                                        WG + "WG_GA/nld@2023-12-01"],
                               "DCTERMS.available": ["2023-12-21"]}}
        self.verzoeken = []
        originelen = (archief.publicatie_metadata, archief.informatieobject, archief.haal_met_url)

        def herstel():
            archief.publicatie_metadata, archief.informatieobject, archief.haal_met_url = originelen
        self.addCleanup(herstel)
        archief.publicatie_metadata = lambda pub: self.metadata[pub]

        def informatieobject(join):
            self.verzoeken.append(join)
            return {"soort": "kaart", "id": join, "dc": "dc-1", "titel": TITELS[join.split("/")[-2]],
                    "url": "https://r" + join + ".gml", "bron": "https://z/dc-1"}

        def haal_met_url(url, **_):
            self.verzoeken.append(url)
            if url.endswith(".gml"):
                return gml(url[len("https://r"):-len(".gml")]), url
            return b"%PDF-1.4 dezelfde legger", url.replace("currentItem", "1/bijlage/x.pdf")
        archief.informatieobject, archief.haal_met_url = informatieobject, haal_met_url

    def lees(self):
        with open(os.path.join(self.besluitmap, "bijlagen.json"), encoding="utf-8") as f:
            return json.load(f)["publicaties"]

    def test_kaarten_alleen_in_versie_van_besluit(self):
        self.a.bijlagen()
        self.assertEqual(sorted(os.listdir(os.path.join(self.besluitmap, "kaarten"))),
                         ["ONTWERP_WG_Alleen_Ontwerp_2023-06-02_ontwerp.gml",
                          "WG_GA_2023-12-01.gml", "WG_Oost_2023-12-01.gml"])
        ontwerp = {x["id"].split("/")[-2]: x for x in self.lees()["wsb-2023-90011"]["bijlagen"]}
        self.assertEqual(ontwerp["WG_Oost"]["vastgesteld_als"], WG + "WG_Oost/nld@2023-12-01")
        self.assertEqual(ontwerp["WG_Grote_Afvoer"]["vastgesteld_als"], WG + "WG_GA/nld@2023-12-01")
        self.assertIsNone(ontwerp["WG_Oost"]["bestand"])
        # what the besluit sets is not downloaded for the ontwerp
        self.assertNotIn("https://r" + WG + "WG_Oost/nld@2023-06-02;ontwerp.gml", self.verzoeken)
        self.assertFalse(os.path.exists(os.path.join(self.besluitmap, "Ontwerp_20230829", "kaarten")))

    def test_gelijke_bijlage_een_keer_opgeslagen(self):
        self.a.bijlagen()
        p = self.lees()
        eerste, tweede = p["wsb-2023-90012"]["bijlagen"][0], p["wsb-2023-90013"]["bijlagen"][0]
        self.assertEqual(eerste["bestand"], "Ontwerp_20230829/bijlagen/exb-1_Legger.pdf")
        self.assertEqual(tweede["identiek_aan"], "Ontwerp_20230829/bijlagen/exb-1_Legger.pdf")
        self.assertEqual(os.listdir(os.path.join(self.besluitmap, "Ontwerp_20230829", "bijlagen")), ["exb-1_Legger.pdf"])
        self.assertEqual(p["wsb-2023-90001"]["gepubliceerd"], "2023-12-21")

    def test_tweede_run_haalt_niets_opnieuw(self):
        self.a.bijlagen()
        self.verzoeken.clear()
        self.a.bijlagen()
        self.assertEqual(self.verzoeken, [])

    def test_kaart_van_eerdere_indeling_wordt_verplaatst(self):
        self.a.bijlagen()
        # As an earlier version kept it: under Definitief_…/kaarten, and bijlagen.json saying so.
        oud = os.path.join(self.besluitmap, "Definitief_20231221", "kaarten", "WG_Oost_2023-12-01.gml")
        os.makedirs(os.path.dirname(oud))
        os.replace(os.path.join(self.besluitmap, "kaarten", "WG_Oost_2023-12-01.gml"), oud)
        pad = os.path.join(self.besluitmap, "bijlagen.json")
        with open(pad, encoding="utf-8") as f:
            data = json.load(f)
        for x in data["publicaties"]["wsb-2023-90001"]["bijlagen"]:
            if x["id"].endswith("WG_Oost/nld@2023-12-01"):
                x["bestand"] = "Definitief_20231221/kaarten/WG_Oost_2023-12-01.gml"
        with open(pad, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.verzoeken.clear()
        self.a.bijlagen()
        self.assertEqual(self.verzoeken, [])
        self.assertTrue(os.path.exists(os.path.join(self.besluitmap, "kaarten", "WG_Oost_2023-12-01.gml")))
        self.assertFalse(os.path.exists(os.path.join(self.besluitmap, "Definitief_20231221", "kaarten")))

    def test_gml_met_andere_versie_wordt_geweigerd(self):
        archief.haal_met_url = lambda url, **_: (gml(WG + "WG_Oost/nld@2025-01-01;9"), url)
        with self.assertRaisesRegex(RuntimeError, "niet /join"):
            self.a.bijlagen()


class AlleenInhoud(unittest.TestCase):
    def test_werkbestanden_niet_in_archief(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as uit, tempfile.TemporaryDirectory() as werk:
            c = config([besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")])
            c.pad = os.path.join(werk, "archief_CVDR1.json")
            a = archief.Archief(c, uit, SimpleNamespace(base_url="fake"), werkmap=werk)
            os.makedirs(a.map(c.besluiten[0]))
            for naam in ("archief.log", "versiehistorie.json", "archief_CVDR1.json"):
                open(os.path.join(uit, naam), "w").close()
            a.index()
            self.assertEqual(sorted(os.listdir(uit)),
                             sorted(["README.md", "index.csv", c.besluiten[0].map]))
            with open(os.path.join(uit, "index.csv"), encoding="utf-8") as f:
                self.assertNotIn("versiehistorie", f.read())
            self.assertEqual(a.historiepad, os.path.join(werk, "archief_CVDR1_versiehistorie.json"))

    def test_index_op_leeg_archief(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as uit:
            c = config([besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")])
            a = archief.Archief(c, uit, SimpleNamespace(base_url="fake"))
            a.index()
            with open(a.map(c.besluiten[0], "README.txt"), encoding="utf-8") as f:
                self.assertIn("!\t\tDefinitief_20231220/wsb-2023-90001_besluit.pdf: niet gedownload", f.read())


class IndexAanvullen(unittest.TestCase):
    """index writes README.txt, README.md and index.csv only when something changed."""

    def setUp(self):
        uit, werk = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(uit.cleanup)
        self.addCleanup(werk.cleanup)
        self.uit, self.werk = uit.name, werk.name
        self.c = config([besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")])
        self.c.pad = os.path.join(self.werk, "archief_CVDR1.json")
        self.b = self.c.besluiten[0]
        self.a = archief.Archief(self.c, self.uit, SimpleNamespace(base_url="fake"), werkmap=self.werk)
        self.a.index()  # the first time: everything is written
        self.readme = self.a.map(self.b, "README.txt")

    def lees(self, pad):
        with open(pad, "rb") as f:
            return f.read()

    def schrijf(self, pad, inhoud):
        with open(pad, "wb") as f:
            f.write(inhoud)

    def test_niets_gewijzigd_niets_geschreven(self):
        self.a.af, self.a.gewijzigd = {self.b.map}, set()
        for naam in ("README.md", "index.csv"):
            self.schrijf(os.path.join(self.uit, naam), b"van een eerdere run")
        self.schrijf(self.readme, b"van een eerdere run\r\n!\t\tMatch.kb\r\n")
        self.a.index()
        for naam in ("README.md", "index.csv"):
            self.assertEqual(self.lees(os.path.join(self.uit, naam)), b"van een eerdere run")
        self.assertEqual(self.lees(self.readme), b"van een eerdere run\r\n!\t\tMatch.kb\r\n")
        self.assertFalse(os.path.exists(os.path.join(self.werk, "vorige-readme")))

    def test_handmatig_blijft_en_vorige_wordt_bewaard(self):
        handmatig = b"Handmatig\r\n\t\tMatch.kb\r\n!\t\taanleverpakket.zip nog toevoegen\r\n"
        oud = self.lees(self.readme) + b"\r\n" + handmatig
        self.schrijf(self.readme, oud)
        self.a.af, self.a.gewijzigd = {self.b.map}, {self.b.map}  # its folder got something new
        self.schrijf(os.path.join(self.uit, "README.md"), b"oud")
        self.a.index()
        nieuw = self.lees(self.readme)
        self.assertTrue(nieuw.endswith(b"\r\n\r\n" + handmatig), nieuw[-200:])
        self.assertEqual(nieuw.count(b"Handmatig"), 1)
        # The '!' lines written by hand count too: besluit, RTR/ and aanleverpakket.
        self.assertIn(b"Nakijken: 3 regel(s).", nieuw)
        self.assertIn(b"In werking 01-01-2024\r\n", nieuw)  # no toestand known yet
        vorige = os.listdir(os.path.join(self.werk, "vorige-readme"))
        self.assertEqual(len(vorige), 1)
        self.assertTrue(vorige[0].startswith(self.b.map + "_"))
        self.assertEqual(self.lees(os.path.join(self.werk, "vorige-readme", vorige[0])), oud)
        self.assertNotEqual(self.lees(os.path.join(self.uit, "README.md")), b"oud")
        # Written again, the README.txt stays the same.
        self.a.gewijzigd = {self.b.map}
        self.a.index()
        self.assertEqual(self.lees(self.readme), nieuw)

    def test_onvolledig_alleen_als_er_iets_anders_staat(self):
        self.a.af, self.a.gewijzigd = set(), set()
        anders = self.lees(self.readme).replace(f"{date.today():%d-%m-%Y}".encode(), b"01-01-2026")
        self.schrijf(self.readme, anders)
        self.a.index()  # only the day differs: left alone
        self.assertEqual(self.lees(self.readme), anders)
        maak_bestand(self.a.map(self.b, "Definitief_20231220", "wsb-2023-90001_besluit.pdf"))
        self.a.index()  # now the besluit is there: README.txt says so
        self.assertIn(b"\t\tDefinitief_20231220/wsb-2023-90001_besluit.pdf", self.lees(self.readme))


class ReadmeTxt(unittest.TestCase):
    def maak(self, besluiten, bestanden):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = config(besluiten)
        a = archief.Archief(c, tmp.name, client=None)
        for b in c.besluiten:
            for rel in bestanden:
                pad = os.path.join(tmp.name, b.map, *rel.split("/"))
                os.makedirs(os.path.dirname(pad), exist_ok=True)
                open(pad, "wb").close()
        return a, c

    def tekst(self, a, b):
        with open(a.map(b, "README.txt"), encoding="utf-8") as f:
            return f.read()

    def test_wijziging(self):
        b = besluit("2024-12-17", "2025-01-01", [], "wsb-2024-90002")
        b["zichtbaar_in_dso"] = {"ontwerp": "2024-10-17", "besluit": "2024-12-20"}
        b["ontwerp"] = [{"id": "wsb-2024-90021", "soort": "ontwerp", "titel": "Ontwerp"},
                        {"id": "wsb-2024-90022", "soort": "kennisgeving", "titel": "Kennisgeving"}]
        b["toestanden"] = [{"nummer": 3, "geldig_vanaf": "2025-01-01"}]
        a, c = self.maak([b], ["Ontwerp_20241017/wsb-2024-90021_ontwerp.pdf",
                               "Ontwerp_20241017/wsb-2024-90022_kennisgeving.pdf",
                               "Ontwerp_20241017/aanleverpakket.zip", "Definitief_20241220/wsb-2024-90002_besluit.pdf",
                               "Definitief_20241220/regeling_toestand-3_geldend-vanaf-20250101.pdf", "LEESMIJ.md",
                               "losse_notitie.txt"])
        uitroep = a._readme_txt(c.besluiten[0], {}, [])
        t = self.tekst(a, c.besluiten[0])
        regels = t.split("\n")
        self.assertIn("\t\tLink: https://zoek.officielebekendmakingen.nl/wsb-2024-90021.html", regels)
        # Files put in by hand are listed under overige bestanden.
        self.assertIn("\t\tOntwerp_20241017/aanleverpakket.zip", regels)
        self.assertIn("\t\tlosse_notitie.txt", regels)
        # Only what is really missing gets a '!': here the RTR snapshots.
        self.assertEqual([r for r in regels if r.startswith("!")], ["!\t\tRTR/: geen toepasbare regels vastgelegd"])
        self.assertEqual(uitroep, 1)
        self.assertIn(f"Nakijken: {uitroep} regel(s).", t)
        # No organisation's own files or document numbers are expected.
        for woord in ("Match", "renvooi", "mutaties", "DM-nummer", "DM "):
            self.assertNotIn(woord, t)
        self.assertNotIn("DM", regels)  # the section
        self.assertFalse(os.path.exists(a.map(c.besluiten[0], "LEESMIJ.md")))
        # Not published, so not mentioned: no inspraakrapport, no kaarten.
        self.assertNotIn("inspraakrapport", t)
        self.assertNotIn("kaarten", t)
        with open(a.map(c.besluiten[0], "README.txt"), "rb") as f:
            self.assertIn(b"\r\n", f.read())

    def test_inspraakrapport_in_eigen_sectie(self):
        b = besluit("2023-12-20", "2024-01-01", [], "wsb-2023-90001")
        b["zichtbaar_in_dso"] = {"ontwerp": "2023-08-29", "besluit": "2023-12-20"}
        b["ontwerp"] = [{"id": "wsb-2023-90011", "soort": "ontwerp", "titel": "Ontwerp"}]
        b["toestanden"] = [{"nummer": 1, "geldig_vanaf": "2024-01-01"}]
        a, c = self.maak([b], ["Definitief_20231220/bijlagen/Inspraakrapport_2026-05-11_1.pdf"])
        a._readme_txt(c.besluiten[0], {}, [])
        t = self.tekst(a, c.besluiten[0])
        self.assertIn("\tinspraakrapport\n\t\tDefinitief_20231220/bijlagen/Inspraakrapport_2026-05-11_1.pdf", t)

    def test_alleen_ontwerp(self):
        a, c = self.maak([alleen_ontwerp("2026-06-05", "wsb-2026-90010")], [])
        a._readme_txt(c.besluiten[0], {}, [])
        t = self.tekst(a, c.besluiten[0])
        self.assertIn("Ontwerp van 05-06-2026, bekendmaking wsb-2026-90010; nog geen besluit\n", t)
        self.assertNotIn("In werking", t)
        self.assertEqual([r for r in t.split("\n") if r.startswith("!")],
                         ["!\t\tOntwerp_20260605/wsb-2026-90010_ontwerp.pdf: niet gedownload (Ontwerp)"])

    def test_besluit_nog_niet_in_werking(self):
        b = besluit("2026-09-22", "2026-10-07", [], "wsb-2026-90020")
        del b["in_werking"]
        a, c = self.maak([b], ["Definitief_20260922/wsb-2026-90020_besluit.pdf"])
        a._readme_txt(c.besluiten[0], {}, [])
        t = self.tekst(a, c.besluiten[0])
        self.assertIn("In werking: nog niet bekend\n", t)
        self.assertNotIn("!", t.split("Toelichting")[0].replace("Een '!'", ""))

    def test_blad_van_de_publicatie(self):
        for voorvoegsel, naam in (("wsb", "Waterschapsblad"), ("prb", "Provinciaal blad"),
                                  ("gmb", "Gemeenteblad")):
            with self.subTest(voorvoegsel):
                pub = f"{voorvoegsel}-2025-90007"
                b = besluit("2025-03-04", "2025-03-06", [], pub)
                a, c = self.maak([b], [f"Definitief_20250304/{pub}_besluit.pdf"])
                with open(a.map(c.besluiten[0], "bijlagen.json"), "w", encoding="utf-8") as f:
                    json.dump({"publicaties": {pub: {"gepubliceerd": "2025-03-05", "bijlagen": []}}}, f)
                a._readme_txt(c.besluiten[0], {}, [])
                t = self.tekst(a, c.besluiten[0])
                self.assertIn(f"\t\tt, {naam} 05-03-2025\n", t)
                self.assertIn(f"Publicaties komen uit het {naam} (zoek.officielebekendmakingen.nl)",
                              " ".join(t.split()))
                for ander in {"Waterschapsblad", "Provinciaal blad", "Gemeenteblad"} - {naam}:
                    self.assertNotIn(ander, t)


if __name__ == "__main__":
    unittest.main()

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

BO = Bestuursorgaan("Waterschap Voorbeeld", "99000000000000000001", ("root",))


def config(besluiten):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"bestuursorgaan": {"naam": BO.naam, "oin": BO.oin, "root": "root"},
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


if __name__ == "__main__":
    unittest.main()

"""Offline tests: a fake RTR stands in for the network.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code"))

import catalogus  # noqa: E402
import rtr  # noqa: E402
from rtr_client import RtrError  # noqa: E402

OIN = "00000001802139097000"
ANDER_OIN = "00000001853261179000"
ROOT = "nl.imow-ws0636.activiteit.ActInWaterschapsVerordWs"
A = "nl.imow-ws0636.activiteit."


def node(identifier, omschrijving, oin=OIN, **extra):
    return {"identifier": identifier, "omschrijving": omschrijving,
            "bestuursorgaanOin": oin, **extra}


def rbos(activiteit, *soorten):
    out = {}
    for soort in soorten:
        typering = "Conclusie" if soort == "Conclusie" else (
            "Maatregelen" if soort == "Maatregelen" else "Indieningsvereisten")
        out.setdefault(typering, []).append(
            {"identifier": soort + activiteit, "typering": typering})
    return out


class FakeRtr:
    """Answers like /rtr-frontend/api for a tiny HDSR-like tree."""

    def __init__(self):
        self.requests_made = 0
        self.base_url = "fake://rtr"
        self.national = {
            catalogus.NATIONALE_KNOPEN["waterschap"]: [
                node(ROOT, "Activiteit gereguleerd in de waterschapsverordening De Stichtse Rijnlanden"),
                node("nl.imow-ws0663.activiteit.0663", "Activiteit gereguleerd in de waterschapsverordening Vechtstromen", ANDER_OIN),
                node("nl.imow-ws0650.activiteit.0650", "Activiteit gereguleerd in de waterschapsverordening", "00000003321640840000"),
            ],
            catalogus.NATIONALE_KNOPEN["provincie"]: [
                node("nl.imow-pv28.activiteit.x", "Activiteit gereguleerd in de Zuid-Hollandse Omgevingsverordening", "00000001002306608000"),
            ],
            catalogus.NATIONALE_KNOPEN["gemeente"]: [],
        }
        self.namen = {ROOT: "Hoogheemraadschap De Stichtse Rijnlanden",
                      "nl.imow-ws0663.activiteit.0663": "waterschap Vechtstromen",
                      "nl.imow-ws0650.activiteit.0650": "waterschap Zuiderzeeland",
                      "nl.imow-pv28.activiteit.x": "provincie Zuid-Holland"}
        self.tree = {
            ROOT: {"omschrijving": "Activiteit gereguleerd in de waterschapsverordening De Stichtse Rijnlanden",
                   "activiteiten": [node(A + "Duiker", "duiker aanleggen"),
                                    node(A + "Lozen", "Lozen"),
                                    node("nl.imow-mnre1034.activiteit.Rijk", "rijksactiviteit", catalogus.NATIONAAL_OIN)]},
            A + "Duiker": {"omschrijving": "duiker aanleggen",
                           "regelBeheerObjecten": rbos(A + "Duiker", "Conclusie", "IndieningsvereistenVergunning"),
                           "activiteiten": [node(A + "DuikerT5", "duiker aanleggen T5"),
                                            node(A + "Maatwerk1", "Maatwerkvoorschrift aanvragen")]},
            A + "Lozen": {"omschrijving": "Lozen",
                          "regelBeheerObjecten": rbos(A + "Lozen", "Conclusie", "Maatregelen"),
                          # reachable twice: the tree is a DAG
                          "activiteiten": [node(A + "Maatwerk1", "Maatwerkvoorschrift aanvragen"),
                                           node(A + "Maatwerk2", "Maatwerkvoorschrift aanvragen")]},
            A + "DuikerT5": {"omschrijving": "duiker aanleggen T5"},
            A + "Maatwerk1": {"omschrijving": "Maatwerkvoorschrift aanvragen"},
            A + "Maatwerk2": {"omschrijving": "Maatwerkvoorschrift aanvragen"},
        }
        self.locs = {
            ROOT: [("nl.imow-ws0636.ambtsgebied.HDSR", "Hoogheemraadschap De Stichtse Rijnlanden")],
            A + "Duiker": [("nl.imow-ws0636.gebied.1", "Vaarweg"), ("nl.imow-ws0636.gebied.2", "Beschermingszone A")],
            A + "Lozen": [("nl.imow-ws0636.ambtsgebied.HDSR", "Hoogheemraadschap De Stichtse Rijnlanden")],
            A + "Maatwerk1": [("nl.imow-ws0636.gebied.1", "Vaarweg")],
            A + "Maatwerk2": [("nl.imow-ws0636.gebied.3", "Zomerkade")],
        }
        self.sttr = {catalogus.functionele_structuur_ref(OIN, "Conclusie" + A + "Duiker"): 11,
                     catalogus.functionele_structuur_ref(OIN, "Maatregelen" + A + "Lozen"): 12}
        self.kapot = set()

    def _tel(self):
        self.requests_made += 1

    def relations(self, identifier, oin, datum):
        self._tel()
        if identifier == catalogus.NATIONALE_KNOPEN["gemeente"]:
            return [node("nl.imow-gm0344.activiteit.x", "omgevingsplan Utrecht", oin)] if oin == "GEM" else []
        return self.national[identifier]

    def relations_node(self, identifier, oin, datum):
        self._tel()
        return {"identifier": identifier, **self.tree[identifier]}

    def details(self, identifier, datum):
        self._tel()
        if identifier in self.kapot:
            raise RtrError("HTTP 500")
        if identifier in self.namen:
            return {"bestuursOrgaanNaam": self.namen[identifier]}
        if identifier == "nl.imow-gm0344.activiteit.x":
            return {"bestuursOrgaanNaam": "gemeente Utrecht"}
        return {"werkzaamheden": ["Dam plaatsen"] if identifier.endswith("Duiker") else []}

    def locaties(self, identifier, datum):
        self._tel()
        return [{"identificatie": i, "noemer": n} for i, n in self.locs.get(identifier, [])]

    def sttr_metadata(self, ref, datum):
        self._tel()
        if ref in self.sttr:
            return {"sttrId": self.sttr[ref], "begindatum": "01-01-2024",
                    "laatsteWijzigingDatum": "06-10-2026 10:05:01"}
        return {"toekomstigeSttr": False}

    def download_sttr(self, sttr_id):
        self._tel()
        return f"<dmn id='{sttr_id}'/>".encode()


def args(**kw):
    base = dict(overheid="Hoogheemraadschap De Stichtse Rijnlanden", oin=None, env="prod",
                date="17-11-2026", sttr=False, location=False, rate=2.0, include_t5=False)
    base.update(kw)
    return SimpleNamespace(**base)


class ZoekBestuursorgaan(unittest.TestCase):
    def setUp(self):
        self.rtr = FakeRtr()

    def test_naam_via_omschrijving(self):
        b = catalogus.zoek_op_naam(self.rtr, "Hoogheemraadschap De Stichtse Rijnlanden", "2026-11-17")
        self.assertEqual((b.oin, b.roots), (OIN, (ROOT,)))

    def test_naam_zonder_naam_in_omschrijving_valt_terug_op_details(self):
        b = catalogus.zoek_op_naam(self.rtr, "Waterschap Zuiderzeeland", "2026-11-17")
        self.assertEqual(b.oin, "00000003321640840000")

    def test_provincie_met_afwijkende_omschrijving(self):
        b = catalogus.zoek_op_naam(self.rtr, "Provincie Zuid-Holland", "2026-11-17")
        self.assertEqual(b.naam, "provincie Zuid-Holland")

    def test_gemeente_vraagt_oin(self):
        with self.assertRaisesRegex(catalogus.CatalogusFout, "--oin"):
            catalogus.zoek_op_naam(self.rtr, "Gemeente Utrecht", "2026-11-17")
        self.assertEqual(catalogus.zoek_op_oin(self.rtr, "GEM", "2026-11-17").naam, "gemeente Utrecht")

    def test_onbekend(self):
        with self.assertRaisesRegex(catalogus.CatalogusFout, "niet gevonden"):
            catalogus.zoek_op_naam(self.rtr, "Waterschap Bestaatniet", "2026-11-17")


class Boom(unittest.TestCase):
    def test_walk(self):
        rtr_ = FakeRtr()
        b = catalogus.Bestuursorgaan("HDSR", OIN, (ROOT,))
        acts = {a.identifier: a for a in catalogus.enumereer_activiteiten(rtr_, b, datum="2026-11-17")}
        self.assertIn(ROOT, acts)                                   # root is an activity too
        self.assertNotIn("nl.imow-mnre1034.activiteit.Rijk", acts)  # other OIN not followed
        self.assertNotIn(A + "DuikerT5", acts)                      # T5 skipped by default
        self.assertEqual(acts[A + "Maatwerk1"].ouder, A + "Duiker")  # first parent wins
        self.assertEqual(list(acts[A + "Duiker"].regelbeheerobjecten), ["Conclusie", "Aanvraag vergunning"])
        self.assertEqual(list(acts[A + "Lozen"].regelbeheerobjecten), ["Conclusie", "Maatregelen"])
        with_t5 = catalogus.enumereer_activiteiten(rtr_, b, datum="2026-11-17", overgeslagen_suffix=())
        self.assertIn(A + "DuikerT5", {a.identifier for a in with_t5})


class Matrix(unittest.TestCase):
    """The werkingsgebieden matrix, which used to lose rows on a name join."""

    def run_archive(self, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeRtr()
        archiver = rtr.RTR(args(**kw), client=self.fake)
        archiver.base_dir = self.tmp.name
        written = {}

        class Recorder:
            def __init__(self, path, columns, ref, change_labels):
                written["columns"] = columns
                written["change_labels"] = change_labels
                written["rows"] = {}

            def write_activity_row(self, row, **kw):
                written["rows"][kw["urn"]] = kw

            def close_workbook(self):
                pass

        self.recorder = written
        orig = rtr.ExcelHandler
        rtr.ExcelHandler = Recorder
        self.addCleanup(setattr, rtr, "ExcelHandler", orig)
        self.records = archiver.archive_activities()
        return archiver

    def row(self, urn):
        cols = self.recorder["columns"]
        flags = self.recorder["rows"][urn]["gebieden"]
        return {c for c, f in zip(cols, flags) if f}

    def test_kolommen_zijn_namen_ambtsgebied_eerst(self):
        self.run_archive()
        self.assertEqual(self.recorder["columns"], ["Ambtsgebied", "Beschermingszone A", "Vaarweg", "Zomerkade"])

    def test_elke_activiteit_eigen_gebieden_op_urn(self):
        self.run_archive()
        self.assertEqual(self.row(A + "Duiker"), {"Beschermingszone A", "Vaarweg"})
        self.assertEqual(self.row(A + "Lozen"), {"Ambtsgebied"})  # capitalised name
        # Two activities, one name: not merged.
        self.assertEqual(self.row(A + "Maatwerk1"), {"Vaarweg"})
        self.assertEqual(self.row(A + "Maatwerk2"), {"Zomerkade"})

    def test_wijzigingen_per_soort(self):
        self.run_archive()
        self.assertEqual(self.recorder["rows"][A + "Duiker"]["changes"],
                         ["06-10-2026 10:05:01", "", "", "", ""])
        self.assertEqual(self.recorder["rows"][A + "Lozen"]["changes"],
                         ["", "", "", "", "06-10-2026 10:05:01"])

    def test_onbekende_soort_krijgt_eigen_kolom(self):
        orig = FakeRtr.__init__

        def init(s):
            orig(s)
            s.tree[A + "Maatwerk2"]["regelBeheerObjecten"] = {"Indieningsvereisten": [{
                "identifier": "IndieningsvereistenMaatwerkvoorschrift" + A + "Maatwerk2",
                "typering": "Indieningsvereisten",
                "toestemming": {"code": "Maatwerkvoorschrift", "waarde": "Aanvraag maatwerkvoorschrift"}}]}
            s.sttr[catalogus.functionele_structuur_ref(
                OIN, "IndieningsvereistenMaatwerkvoorschrift" + A + "Maatwerk2")] = 13
        FakeRtr.__init__ = init
        self.addCleanup(setattr, FakeRtr, "__init__", orig)
        self.run_archive()
        self.assertEqual(self.recorder["change_labels"][-1], "Aanvraag maatwerkvoorschrift")
        self.assertEqual(self.recorder["rows"][A + "Maatwerk2"]["changes"][-1], "06-10-2026 10:05:01")

    def test_elke_activiteit_een_keer_opgehaald(self):
        self.run_archive()
        # Name lookup: the national node + the root's details.  Then per
        # activity relations_node + details + locaties, + metadata per RBO;
        # the T5 twin is walked (for its children) but not read.
        n_acts = len(self.records)
        n_rbos = sum(len(r.activiteit.regelbeheerobjecten) for r in self.records)
        self.assertEqual(self.fake.requests_made, 2 + 3 * n_acts + n_rbos + 1)

    def test_fout_bij_een_activiteit_stopt_de_rest_niet(self):
        self.fake_kapot = A + "Maatwerk2"
        orig = FakeRtr.__init__

        def init(s):
            orig(s)
            s.kapot.add(A + "Maatwerk2")
        FakeRtr.__init__ = init
        self.addCleanup(setattr, FakeRtr, "__init__", orig)
        self.run_archive()
        self.assertIn("HTTP 500", self.recorder["rows"][A + "Maatwerk2"]["error"])
        self.assertEqual(self.row(A + "Maatwerk1"), {"Vaarweg"})

    def test_sttr_en_locatie_bestanden(self):
        import json
        archiver = self.run_archive(sttr=True, location=True)
        folder = archiver._pad("DMN-files")
        files = sorted(os.listdir(folder))
        self.assertEqual(files, ["STTR_11_Duiker_Conclusie.xml", "STTR_12_Lozen_Maatregelen.xml", "manifest.json"])
        manifest = json.load(open(os.path.join(folder, "manifest.json"), encoding="utf-8"))
        self.assertEqual(len(manifest["sttrs"]), 2)
        tekst = open(archiver._pad("werkingsgebieden", "txt"), encoding="utf-8").read()
        self.assertIn("Vaarweg (nl.imow-ws0636.gebied.1):\n  - duiker aanleggen\n  - Maatwerkvoorschrift aanvragen\n", tekst)


class Excel(unittest.TestCase):
    def test_echte_werkmap(self):
        from datetime import datetime
        from excel import ExcelHandler
        with tempfile.TemporaryDirectory() as tmp:
            pad = os.path.join(tmp, "log", "x.xlsx")
            e = ExcelHandler(pad, ["Ambtsgebied", "Vaarweg"], datetime(2026, 11, 17))
            e.write_activity_row(2, name="a", urn="u", werkzaamheden=["w"],
                                 changes=["06-10-2026 10:05:01", "", "", "", ""],
                                 gebieden=[True, False])
            e.close_workbook()
            with zipfile.ZipFile(pad) as z:
                sheet = z.read("xl/worksheets/sheet1.xml").decode()
                styles = z.read("xl/styles.xml").decode()
            self.assertIn('textRotation="90"', styles)
            self.assertIn("538DD5", styles)
            self.assertIn('r="I1"', sheet)  # 4 base + 5 change columns: A..I
            self.assertIn('r="K1"', sheet)  # then the 2 matrix columns: J, K


if __name__ == "__main__":
    unittest.main()

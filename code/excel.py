"""
Excel status overview for RTR archiving.

One row per activity: its werkzaamheden, the last change date of each STTR
(coloured by how recent it is relative to the peildatum) and a matrix with a
blue cell for every werkingsgebied the activity applies in.
"""

from __future__ import annotations

import os
from datetime import datetime

import xlsxwriter

BASE_HEADERS = ["Activiteit", "Urn", "Aantal", "Werkzaamheden"]
BASE_WIDTHS = [60, 45, 7, 50]
CHANGE_WIDTH = 20
DEFAULT_CHANGE_LABELS = ["Conclusie", "Melding", "Aanvraag vergunning", "Informatie", "Maatregelen"]
DATE_FORMAT = "%d-%m-%Y %H:%M:%S"
BLUE = "#538DD5"
MATRIX_COLUMN_WIDTH = 3.5


class ExcelHandler:
    """Writes the status overview of one archiving run."""

    def __init__(self, workbook_path: str, gebied_columns: list[str],
                 reference_date: datetime, change_labels: list[str] | None = None,
                 change_header: str = "Wijziging"):
        """
        Args:
            workbook_path: where the .xlsx goes
            gebied_columns: werkingsgebied names, one matrix column each
            reference_date: the peildatum; change dates are coloured by how
                long before this date they are
            change_labels: STTR flavours, one "<change_header> …" column each
            change_header: "Wijziging" (last change) or "Geldig vanaf"
        """
        self.change_labels = list(change_labels or DEFAULT_CHANGE_LABELS)
        self.headers = BASE_HEADERS + [f"{change_header} {l}" for l in self.change_labels]
        os.makedirs(os.path.dirname(workbook_path), exist_ok=True)
        self.workbook_path = workbook_path
        self.gebied_columns = gebied_columns
        self.reference_date = reference_date
        self.workbook = xlsxwriter.Workbook(workbook_path)
        self.worksheet = self.workbook.add_worksheet("Status")
        self._formats: dict[tuple, object] = {}
        self.setup_worksheet()

    def fmt(self, color: str = "white", **extra):
        """A cached cell format; xlsxwriter limits how many a workbook has."""
        key = (color, tuple(sorted(extra.items())))
        if key not in self._formats:
            self._formats[key] = self.workbook.add_format({
                "bg_color": color, "align": "left", "valign": "top",
                "border": 1, **extra})
        return self._formats[key]

    def setup_worksheet(self):
        """Headers, column widths, rotated matrix headers and frozen panes."""
        header = self.fmt("#DDDDDD", bold=True, text_wrap=True, valign="bottom")
        self.worksheet.write_row(0, 0, self.headers, header)
        for col, width in enumerate(BASE_WIDTHS):
            self.worksheet.set_column(col, col, width)
        self.worksheet.set_column(len(BASE_WIDTHS), len(self.headers) - 1, CHANGE_WIDTH)

        first = len(self.headers)
        if self.gebied_columns:
            rotated = self.fmt("#DDDDDD", bold=True, rotation=90, valign="bottom",
                               font_size=9)
            self.worksheet.write_row(0, first, self.gebied_columns, rotated)
            self.worksheet.set_column(first, first + len(self.gebied_columns) - 1,
                                      MATRIX_COLUMN_WIDTH)
            # Excel caps a row at 409 points; at 9 points a character takes
            # about 5, which fits names of up to ~75 characters.
            longest = max(len(c) for c in self.gebied_columns)
            self.worksheet.set_row(0, min(409, 15 + longest * 5))
        self.worksheet.freeze_panes(1, 1)
        self.worksheet.autofilter(0, 0, 0, len(self.headers) - 1)

    def write_activity_row(self, row: int, *, name: str, urn: str,
                           werkzaamheden: list[str], changes: list[str],
                           gebieden: list[bool], error: str = ""):
        """Write one activity on (1-based) *row*; *changes* follow change_labels."""
        r = row - 1
        plain = self.fmt()
        self.worksheet.write(r, 0, name, plain)
        self.worksheet.write(r, 1, urn, plain)
        self.worksheet.write_number(r, 2, len(werkzaamheden), plain)
        # "; " because werkzaamheden names contain commas themselves.
        self.worksheet.write(r, 3, error and f"FOUT: {error}" or "; ".join(werkzaamheden),
                             self.fmt("#FFC7CE") if error else plain)
        for offset, change in enumerate(changes):
            self.write_change(r, len(BASE_HEADERS) + offset, change)
        first = len(self.headers)
        for offset, present in enumerate(gebieden):
            self.worksheet.write_blank(r, first + offset, None,
                                       self.fmt(BLUE) if present else plain)

    def write_change(self, r: int, col: int, content: str):
        """A date, green by how recent; anything else plain."""
        for form in (DATE_FORMAT, "%d-%m-%Y"):
            try:
                date = datetime.strptime(str(content), form)
                break
            except ValueError:
                continue
        else:
            self.worksheet.write(r, col, content, self.fmt())
            return
        days = (self.reference_date - date).days
        self.worksheet.write(r, col, content, self.fmt(self.set_green_intensity(days)))

    @staticmethod
    def set_green_intensity(days_diff: int) -> str:
        """More recent changes get a more intense green."""
        if days_diff < 1:
            return "#00FF00"
        if days_diff < 8:
            return "#32CD32"
        if days_diff < 30:
            return "#98FB98"
        if days_diff < 60:
            return "#90EE90"
        return "#F0FFF0"

    def close_workbook(self):
        self.workbook.close()


def generate_file_path(date_str, overheid, env, suffix, base_dir, extension=None):
    """log/<jjjjmmdd>_<overheid>_<omgeving>_STTR_<suffix>[.<extension>]

    Without an extension the path is meant as a folder.
    """
    formatted_date = datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y%m%d")
    environment = "productie-omgeving" if env == "prod" else "pre-omgeving"
    base_name = f"{formatted_date}_{overheid.replace(' ', '_')}_{environment}_STTR_{suffix}"
    full_name = f"{base_name}.{extension}" if extension else base_name
    return os.path.join(base_dir, "log", full_name)

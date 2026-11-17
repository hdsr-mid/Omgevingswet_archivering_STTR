"""
Command-line arguments for the RTR archiving application.
"""

import argparse
from datetime import datetime


def _datum(waarde: str) -> str:
    try:
        datetime.strptime(waarde, "%d-%m-%Y")
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{waarde}' is geen datum dd-mm-jjjj")
    return waarde


def _rate(waarde: str) -> float:
    rate = float(waarde)
    if rate <= 0:
        raise argparse.ArgumentTypeError("--rate moet groter dan 0 zijn")
    return rate


class ArgumentParser:
    """Handles parsing of command-line arguments for RTR archiving."""

    @staticmethod
    def parse_command_line_arguments(argv=None):
        """
        Returns:
            argparse.Namespace with
                overheid (str): name of the bestuursorgaan (underscores → spaces)
                oin (str|None): OIN of the bestuursorgaan; overrides --overheid
                env (str): 'prod' or 'pre'
                date (str): peildatum dd-mm-jjjj
                sttr (bool): download the STTR files (DMN XML)
                location (bool): write the werkingsgebieden per activity
                rate (float): requests per second to the RTR
                include_t5 (bool): also archive the …T5 twin activities
        """
        parser = argparse.ArgumentParser(
            description="Archiveert de toepasbare regels (RTR) van een "
                        "bestuursorgaan op een peildatum. Geen API-key nodig.")
        parser.add_argument("--overheid", type=str,
                            default="Hoogheemraadschap_De_Stichtse_Rijnlanden",
                            help="Naam van het waterschap of de provincie, "
                                 "underscores voor spaties (standaard: "
                                 "Hoogheemraadschap_De_Stichtse_Rijnlanden).")
        parser.add_argument("--oin", type=str, default=None,
                            help="OIN van het bestuursorgaan; nodig voor "
                                 "gemeenten, gaat voor --overheid.")
        parser.add_argument("--env", type=str, default="prod", choices=["prod", "pre"],
                            help="Omgeving: prod (standaard) of pre.")
        parser.add_argument("--date", type=_datum,
                            default=datetime.now().strftime("%d-%m-%Y"),
                            help="Peildatum dd-mm-jjjj (standaard: vandaag).")
        parser.add_argument("--sttr", action="store_true",
                            help="Download de STTR-bestanden (DMN XML).")
        parser.add_argument("--location", action="store_true",
                            help="Schrijf de werkingsgebieden per activiteit naar .txt.")
        parser.add_argument("--rate", type=_rate, default=2.0,
                            help="Verzoeken per seconde aan de RTR (standaard: 2).")
        parser.add_argument("--include-t5", action="store_true",
                            help="Ook de …T5-varianten meenemen (publiceren "
                                 "normaal geen STTR).")
        args = parser.parse_args(argv)
        args.overheid = args.overheid.replace("_", " ")
        return args

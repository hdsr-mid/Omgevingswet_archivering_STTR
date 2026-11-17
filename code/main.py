"""
Main entry point for archiving the toepasbare regels (RTR) of a bestuursorgaan.
"""

import logging
import sys

from catalogus import CatalogusFout
from commands import ArgumentParser
from rtr import RTR
from rtr_client import RtrError


def main(argv=None) -> int:
    """Run one archiving pass; exit code 1 when it could not be done."""
    for stream in (sys.stdout, sys.stderr):
        # Keep Dutch names printable on a cp1252 Windows console.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")
    args = ArgumentParser.parse_command_line_arguments(argv)
    try:
        records = RTR(args).archive_activities()
    except (CatalogusFout, RtrError) as exc:
        logging.error("%s", exc)
        return 1
    return 1 if records and all(r.fout for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())

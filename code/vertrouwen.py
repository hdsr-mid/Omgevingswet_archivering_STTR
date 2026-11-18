"""HTTPS to a server whose root Windows trusts and Python has not seen yet.

Python on Windows trusts the root certificates *present* in the Windows
certificate store, read as each connection starts.  Windows keeps that store
small: it fetches a root from Microsoft's trusted-root list the first time
one of its own programs meets a chain that needs it.  Python never asks, so
a root nobody on the machine has needed yet fails verification in Python
while every browser on the same machine opens the site without complaint.

That is how the verordening went missing on 2026-10-12.  The CVDR
repository's chain ends at *certSIGN ROOT CA G2*, the workstation had never
fetched that root, and the fetch failed with ``CERTIFICATE_VERIFY_FAILED``
("self-signed certificate in certificate chain") — so the corpus recorded no
toestand and no result showed an article or a lid.  A server that never
runs a browser would stay like that for good.

The remedy is Windows' own: have a Windows program open the same URL, which
makes Windows verify the chain and fetch the root if — and only if —
Microsoft trusts it, then try once more.  Nothing here lowers verification.
A root Windows does not trust stays untrusted, the retry fails as the first
attempt did, and the error then says what to do (:func:`uitleg`).

Standard library only.  Ported unchanged from Knowledgebase-CLI
(``sttr/vertrouwen.py``, branch dev-v8-STTR-route2).
"""

from __future__ import annotations

import base64
import logging
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
from typing import Any, Callable

log = logging.getLogger("rtr.vertrouwen")

# OpenSSL's verify codes for a chain that ends at a root this machine does
# not hold: 19, a self-signed certificate in the chain (the server sent the
# root along); 20, no local issuer certificate (it did not).
_ONBEKENDE_ROOT = frozenset({19, 20})

# Trust is a property of the host, so Windows is handed the origin alone —
# never a path or a query string — and only in this shape: nothing in it
# can close the quoted string it is put in.
_VEILIGE_HOST = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?$")

# Each host is handed to Windows once per process: a root that could not be
# fetched the first time will not be fetched by asking again.
_gevraagd: set[str] = set()


def onbekende_root(fout: BaseException) -> bool:
    """Did *fout* fail on a root certificate this machine does not hold?"""
    reden = getattr(fout, "reason", fout)
    return (isinstance(reden, ssl.SSLCertVerificationError)
            and reden.verify_code in _ONBEKENDE_ROOT)


def laat_windows_controleren(url: str, *, timeout: float = 60.0) -> bool:
    """Have Windows open *url*'s host once; True when Windows accepted it.

    Windows verifies the chain with its own certificate engine, and adds the
    root to its store when Microsoft trusts it.  A later connection from
    Python reads the store afresh and finds it.  Any HTTP answer counts, a
    404 included: an answer means the chain was accepted.
    """
    delen = urllib.parse.urlsplit(url)
    host = delen.netloc
    if sys.platform != "win32" or delen.scheme != "https" \
            or not _VEILIGE_HOST.match(host) or host in _gevraagd:
        return False
    _gevraagd.add(host)
    opdracht = (
        f"try {{ Invoke-WebRequest -Uri 'https://{host}/' -Method Head "
        f"-UseBasicParsing -TimeoutSec {int(timeout)} | Out-Null; exit 0 }} "
        "catch [System.Net.WebException] { "
        "if ($_.Exception.Response) { exit 0 } else { exit 1 } } "
        "catch { exit 1 }")
    try:
        uitslag = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(opdracht.encode("utf-16-le")).decode("ascii")],
            capture_output=True, timeout=timeout + 15)
    except (OSError, subprocess.TimeoutExpired) as fout:
        log.warning("Windows kon de certificaatketen van %s niet controleren: "
                    "%s", host, fout)
        return False
    if uitslag.returncode != 0:
        log.warning("Windows vertrouwt de certificaatketen van %s ook niet",
                    host)
        return False
    log.info("Windows heeft de certificaatketen van %s gecontroleerd; "
             "opnieuw geprobeerd", host)
    return True


def open_url(openen: Callable[..., Any], verzoek: Any, url: str,
             timeout: float) -> Any:
    """``openen(verzoek, timeout=…)``, once more if Windows fetched the root.

    *openen* is ``urllib.request.urlopen`` or an opener's ``open``; *url* is
    the address *verzoek* asks for, whose host Windows is asked to open.
    """
    try:
        return openen(verzoek, timeout=timeout)
    except urllib.error.URLError as fout:
        if not onbekende_root(fout) or not laat_windows_controleren(url):
            raise
    return openen(verzoek, timeout=timeout)


def uitleg(fout: BaseException) -> str:
    """What to do about *fout*, when it is the root this machine lacks."""
    if not onbekende_root(fout):
        return ""
    return (" — deze machine vertrouwt de root van de certificaatketen niet. "
            "Open het adres eenmaal in Edge, of laat het root-certificaat in "
            "het Windows-certificaatarchief installeren")

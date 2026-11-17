"""Client for the Registratie Toepasbare Regels.

Talks to the RTR viewer's own backend (``/rtr-frontend/api``) — the one the
public viewer at omgevingswet.overheid.nl uses.  It is anonymous and needs
no API key, and it serves exactly what any citizen sees in that viewer.

That also means it is *not* a published API with a support contract.  So we
crawl politely: one shared rate limit for every call, default 2/s, well under
anything that could be called abusive.

Ported from Knowledgebase-CLI (``sttr/rtr.py``, branch dev-v8-STTR-route2),
with the pre-production twin added.  Standard library only.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import vertrouwen

log = logging.getLogger("rtr.client")

PRODUCTIE = "https://omgevingswet.overheid.nl/rtr-frontend/api"
PRE = "https://pre.omgevingswet.overheid.nl/rtr-frontend/api"
BASE_URLS = {"prod": PRODUCTIE, "pre": PRE}

USER_AGENT = "rtr-archivering/2.0 (python-urllib)"

_STRUCTUUR = "/functionele-structuur/structuur"


class RtrError(RuntimeError):
    """The RTR could not be reached, or answered something unusable."""


class RtrClient:
    """Rate-limited, retrying reader for the RTR frontend API.

    ``rate`` is requests per second across *all* calls on this client, so
    the politeness budget cannot be sidestepped by using several methods.
    """

    def __init__(self, base_url: str = PRODUCTIE, *, rate: float = 2.0,
                 timeout: float = 30.0, retries: int = 3,
                 opener: Any = None) -> None:
        if rate <= 0:
            raise ValueError("rate moet groter dan 0 zijn")
        self.base_url = base_url.rstrip("/")
        self._interval = 1.0 / rate
        self.timeout = timeout
        self.retries = retries
        self._opener = opener or urllib.request.build_opener()
        self._next_allowed = 0.0
        self.requests_made = 0

    # ── plumbing ─────────────────────────────────────────────────────

    def _wait(self) -> None:
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        self._next_allowed = max(now, self._next_allowed) + self._interval

    def _get(self, path: str, params: dict[str, str] | None = None,
             *, accept: str = "application/json") -> bytes:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            url, headers={"Accept": accept, "User-Agent": USER_AGENT})

        last: Exception | None = None
        for poging in range(1, self.retries + 1):
            self._wait()
            self.requests_made += 1
            try:
                with vertrouwen.open_url(self._opener.open, request, url,
                                         self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                # 4xx is our mistake and will not improve by asking again.
                if 400 <= exc.code < 500:
                    raise RtrError(
                        f"RTR gaf HTTP {exc.code} voor {url}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if poging < self.retries:
                pauze = 2.0 ** poging
                log.warning("RTR-verzoek mislukt (poging %d/%d), opnieuw over "
                            "%.0fs: %s", poging, self.retries, pauze, last)
                time.sleep(pauze)
        raise RtrError(f"RTR onbereikbaar na {self.retries} pogingen: "
                       f"{url}{vertrouwen.uitleg(last) if last else ''}"
                       ) from last

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        raw = self._get(path, params)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RtrError(f"RTR gaf geen geldige JSON voor {path}") from exc

    # ── functionele structuur ────────────────────────────────────────

    def relations_node(self, identifier: str, oin: str,
                       datum: str) -> dict[str, Any]:
        """One activity with its direct children and regelbeheerobjecten.

        The children are under ``activiteiten``; the node's own
        ``regelBeheerObjecten`` say which STTR flavours it carries.
        """
        return self._get_json(f"{_STRUCTUUR}/{identifier}/relations",
                              {"oin": oin, "datum": datum})

    def relations(self, identifier: str, oin: str,
                  datum: str) -> list[dict[str, Any]]:
        """Direct children of one activity."""
        return self.relations_node(identifier, oin, datum).get(
            "activiteiten", [])

    def details(self, identifier: str, datum: str) -> dict[str, Any]:
        """Omschrijving, bestuursorgaan and werkzaamheden of one activity."""
        return self._get_json(f"{_STRUCTUUR}/{identifier}/details",
                              {"datum": datum})

    def locaties(self, identifier: str, datum: str) -> list[dict[str, Any]]:
        """Werkingsgebieden: ``identificatie`` **and** ``noemer``."""
        data = self._get_json(f"{_STRUCTUUR}/{identifier}/locaties",
                              {"datum": datum})
        return data.get("locaties", [])

    # ── STTR ─────────────────────────────────────────────────────────

    def sttr_metadata(self, functionele_structuur_ref: str,
                      datum: str) -> dict[str, Any]:
        """``{sttrId, begindatum, laatsteWijzigingDatum, toekomstigeSttr}``.

        An activity that has no STTR of this type still answers 200, with
        ``sttrId`` simply absent — that is how absence is detected.
        """
        return self._get_json(
            "/sttr/metadata",
            {"functioneleStructuurRef": functionele_structuur_ref,
             "datum": datum})

    def download_sttr(self, sttr_id: int | str) -> bytes:
        """The STTR XML as published."""
        return self._get("/sttr/download-sttr",
                         {"sttrContentId": str(sttr_id)},
                         accept="application/xml")

# Omgevingswet Archivering STTR

Een Python-tool voor het archiveren van de toepasbare regels uit de Registratie Toepasbare Regels (RTR) voor Nederlandse overheden.

## Overzicht

De tool leest de toepasbare regels van één bestuursorgaan op een peildatum en kan:

- Een Excel-overzicht genereren van alle activiteiten met hun werkzaamheden, de laatste wijziging per soort toepasbare regel en een matrix van de werkingsgebieden waarin elke activiteit geldt
- De STTR-bestanden (DMN XML) per activiteit downloaden en archiveren
- De werkingsgebieden met hun activiteiten exporteren

![Voorbeeld voor Hoogheemraadschap de Stichtse Rijnlanden](data/xlsx_example.PNG)

## Bron: geen API-key nodig

De gegevens komen uit de openbare achterkant van de RTR-viewer van het Omgevingsloket (`https://omgevingswet.overheid.nl/rtr-frontend/api`, en voor de pre-omgeving `https://pre.omgevingswet.overheid.nl/rtr-frontend/api`). Dat is precies wat iedere burger in het Omgevingsloket ziet. Er is geen API-key en er zijn geen PowerBI-exports nodig: de activiteiten worden gevonden door de activiteitenboom van het bestuursorgaan af te lopen, en de werkingsgebieden komen met hun naam rechtstreeks uit de RTR.

Dit is geen officieel gepubliceerde API met een ondersteuningsafspraak. De tool vraagt daarom rustig op: standaard 2 verzoeken per seconde (`--rate`). Een volledige run voor een waterschap duurt zo enkele minuten.

De RTR-client is overgenomen uit Knowledgebase-CLI (`sttr/rtr.py` en `sttr/catalogus.py`).

## Vereisten

- Python 3.9 of hoger
- `xlsxwriter`

## Setup

```bash
pip install -r requirements.txt
```

## Gebruik

```bash
python code/main.py
```

### Command-line opties

| Optie | Beschrijving | Standaard |
|-------|--------------|-----------|
| `--overheid` | Naam van het waterschap of de provincie (underscores voor spaties) | `Hoogheemraadschap_De_Stichtse_Rijnlanden` |
| `--oin` | OIN van het bestuursorgaan; nodig voor gemeenten, gaat voor `--overheid` | – |
| `--env` | Omgeving: `prod` of `pre` | `prod` |
| `--date` | Peildatum in formaat DD-MM-YYYY | Vandaag |
| `--sttr` | Download de STTR-bestanden (DMN XML) | Uit |
| `--location` | Exporteer de werkingsgebieden per activiteit | Uit |
| `--rate` | Verzoeken per seconde aan de RTR | `2` |
| `--include-t5` | Neem ook de `…T5`-varianten van activiteiten mee (die publiceren normaal geen STTR) | Uit |

### Welke overheden?

- **Waterschappen en provincies** worden op naam gevonden, bijvoorbeeld `--overheid Waterschap_Vechtstromen`, `--overheid Wetterskip_Fryslân` of `--overheid Provincie_Zuid-Holland`. Bij een onbekende naam toont de tool welke namen er wel zijn.
- **Gemeenten** geeft de RTR alleen op OIN prijs; gebruik `--oin`, bijvoorbeeld `--oin 00000001002220647000` (gemeente Utrecht). Het OIN staat in het OIN-register van Logius.
- **Het Rijk** wordt niet ondersteund: het OIN van BZK of een rijksactiviteit (`nl.imow-mnre…`) geeft exitcode 3.

### Voorbeelden

Standaard uitvoering voor Hoogheemraadschap De Stichtse Rijnlanden:
```bash
python code/main.py
```

Voor een ander waterschap:
```bash
python code/main.py --overheid Waterschap_Vechtstromen
```

Pre-omgeving op een specifieke datum:
```bash
python code/main.py --env pre --date 03-03-2026
```

Volledig archief inclusief STTR-bestanden en werkingsgebieden:
```bash
python code/main.py --date 12-04-2025 --sttr --location
```

## Output

Alles komt in `log/`, met de peildatum, de naam van het bestuursorgaan en de omgeving in de naam:

- `…_STTR_status.xlsx`: één rij per activiteit, met
  - de werkzaamheden;
  - per soort toepasbare regel (Conclusie, Melding, Aanvraag vergunning, Informatie, Maatregelen, en een extra kolom voor elke andere soort die de RTR teruggeeft, zoals Aanvraag maatwerkvoorschrift) de laatste wijziging, groener naarmate die dichter vóór de peildatum ligt;
  - een blauwe cel voor elk werkingsgebied waarin de activiteit geldt. De kolomkoppen zijn de namen van de werkingsgebieden; het ambtsgebied staat vooraan als `Ambtsgebied`.
- `…_STTR_DMN-files/` (met `--sttr`): `STTR_<sttrId>_<activiteit>_<soort>.xml`, byte voor byte zoals gepubliceerd, plus `manifest.json` met per bestand de activiteit, soort, begindatum en laatste wijziging.
- `…_STTR_werkingsgebieden.txt` (met `--location`): per werkingsgebied de identificatie(s) en de activiteiten die er gelden.

## Archief per besluit

`code/archief.py` legt een regeling per definitief besluit vast: de publicaties met hun kaarten en bijlagen, de hele regeling en élke versie van de toepasbare regels die eronder viel. Welke besluiten, publicaties en peildata dat zijn, staat in een JSON-bestand; voor de Waterschapsverordening van HDSR is dat [data/archief_CVDR707248.json](data/archief_CVDR707248.json).

```bash
pip install -r requirements-archief.txt
python code/archief.py --rate 4
```

Zonder JSON-bestand gebruikt de tool `data/archief_CVDR707248.json`; voor een andere regeling geef je het pad mee (`python code/archief.py data/archief_CVDR….json`).

Per definitief besluit één map, genoemd naar de besluitdatum:

```
archief/
├── README.md, index.csv (elk bestand met SHA-256)
└── 20250715_wijzigingsbesluit-h2-3-4_wsb-2025-17997/
    ├── README.txt    wat erin zit, en met een ! vooraan wat erbij hoort maar ontbreekt
    ├── bijlagen.json herkomst en SHA-256 van elke kaart en bijlage
    ├── Ontwerp_20250514/     ontwerpbesluit en kennisgevingen (PDF uit het Waterschapsblad)
    │   └── bijlagen/         informatieobjecten (PDF) en externe bijlagen
    ├── Definitief_20250724/  het besluit (PDF uit het Waterschapsblad), zijn bijlagen/
    │                         (zoals een inspraakrapport), en de hele regeling zoals die
    │                         vanaf de inwerkingtreding gold (PDF)
    ├── kaarten/      de werkingsgebieden (GIO's, GML) die het besluit vaststelt
    └── RTR/
        ├── 20250801_volledig/   alle toepasbare regels die op die dag golden
        ├── 20250808_volledig/   (status.xlsx, werkingsgebieden.txt, STTR/)
        └── 20250902_gewijzigd/  alleen wat op die dag inging (STTR/)
```

- **Volledig:** elke STTR-versie hoort bij het besluit in wiens periode (inwerkingtreding tot de volgende) hij inging. Staat hij in een volledige vastlegging van dat besluit, dan is hij daar gearchiveerd; anders krijgt hij een map `<datum>_gewijzigd` voor de dag waarop hij inging. Dat geldt voor latere wijzigingen en voor eerste uploads die binnen enkele dagen weer vervangen werden. `STTR/manifest.json` noemt per bestand welke versie het vervangt en waardoor het vervangen werd; vervallen regelbeheerobjecten staan erin zonder bestand. Het `README.md` van het archief controleert dat alle versies uit de versiegeschiedenis erin staan.
- **Buiten het archief:** de versiegeschiedenis (`log/<config>_versiehistorie.json`), het logboek (`log/<config>.log`) en de STTR-cache staan in `log/` (`--werkmap`, `--cache`): de tool heeft ze nodig om het archief aan te vullen, de lezer niet. Het JSON-bestand staat in de repository.
- **Versiegeschiedenis:** de RTR geeft per regelbeheerobject alleen de versie op een datum. De tool loopt de activiteitenboom maandelijks en op elke peildatum af en volgt per regelbeheerobject de keten van versies (`code/versiehistorie.py`).
- **Ontwerp en Definitief:** elk genoemd naar de dag waarop de publicatie zichtbaar werd in het DSO, in dezelfde vorm als de map van het besluit: `Ontwerp_<jjjjmmdd>` en `Definitief_<jjjjmmdd>`. Die dagen staan per besluit in het JSON-bestand (`zichtbaar_in_dso`: `ontwerp` en `besluit`); het is de bekendmakingsdatum van het ontwerp en van het besluit, en de stap `bijlagen` waarschuwt als die volgens de officiële metadata anders is.
- **Waterschap, provincie of gemeente:** het blad volgt uit het begin van de publicatie-id: `wsb-` Waterschapsblad, `prb-` Provinciaal blad, `gmb-` Gemeenteblad. Het `README.txt` noemt per publicatie het blad en de datum. Een ander begin (zoals `stcrt-`) is een fout in het JSON-bestand.
- **Activiteitenboom:** `bestuursorgaan.oin` is genoeg. Zonder `root` (of `roots`, een lijst) zoekt de tool de root(s) van de activiteitenboom bij het OIN op, op elke datum van inwerkingtreding en op `--tot`, en loopt ze allemaal af; dat gebeurt alleen voor de stappen `rtr` en `wijzigingen`. Een root die op een datum (nog) niet bestaat, wordt die dag overgeslagen.
- **Kaarten en bijlagen:** de metadata van elke publicatie noemt wat ze vaststelt (`steltVast`: werkingsgebieden als GIO en PDF-informatieobjecten zoals een inspraakrapport) en welke externe bijlagen ze meestuurt. De stap `bijlagen` haalt ze op: de GML is de authentieke versie van officielebekendmakingen.nl en moet zelf de gevraagde versie noemen. Kaarten staan in `kaarten/` naast `RTR/`, alleen in de versie van het besluit; een kaart van het ontwerp die het besluit niet vaststelt (niet onder dezelfde identificatie en niet onder dezelfde titel), heet `ONTWERP_…`. Een bijlage die gelijk is aan een eerdere in dezelfde map (een kennisgeving die twee keer is gepubliceerd) staat er één keer; `bijlagen.json` noemt hem dan `identiek_aan`.
- **README.txt:** per besluit wat er in de map zit, per onderdeel (ontwerp, kennisgeving, kaarten, inspraakrapport, besluit, hele regeling, RTR) en met een `!` vooraan wat erbij hoort maar niet in de map zit: een publicatie, kaart of bijlage die niet gedownload is, een toestand van de regeling die niet is afgedrukt. Wat niet gepubliceerd is (een inspraakrapport, kaarten bij een besluit dat geen werkingsgebieden wijzigt), wordt niet genoemd. Een bestand dat je zelf in de map zet, staat onder `overige bestanden`. `opmerking` bij een besluit of publicatie in het JSON-bestand komt in de README.txt.
- **Hele regeling:** de wijzigingsbesluiten bevatten alleen de wijzigingen. De hele regeling wordt per toestand afgedrukt van [lokaleregelgeving.overheid.nl](https://lokaleregelgeving.overheid.nl/CVDR707248), met de afdrukopmaak van die site, in A4 en met bron en afdrukdatum in de voettekst. Dat gebeurt met Playwright: op Windows met de geïnstalleerde Edge, elders met Chrome of met `python -m playwright install chromium-headless-shell`, of met `--browser <pad>`.
- **Statusoverzicht:** in het archief toont `status.xlsx` per soort de datum waarop de versie inging (`Geldig vanaf …`). De laatste wijzigingsdatum van een versie overschrijft de RTR zodra die versie vervangen wordt, dus voor een peildatum in het verleden zegt die niets.
- **Stappen:** `--stappen publicaties,bijlagen,regeling,rtr,wijzigingen,index` (standaard alle). Wat er al is, wordt overgeslagen; een nieuw besluit toevoegen aan het JSON-bestand en opnieuw draaien vult het archief aan.
- **Aanvullen:** elke run vult aan. Een besluit is compleet als het in werking is en alles er is: elke publicatie als PDF, `bijlagen.json` met elke kaart en bijlage, elke toestand van de regeling en een volledige vastlegging op elke peildatum. Een compleet besluit wordt niet opnieuw opgehaald. Wijzigingen in de toepasbare regels horen bij het besluit dat op die dag gold, tot het volgende besluit in werking treedt; ook een compleet besluit krijgt daarvoor nieuwe mappen `<datum>_gewijzigd`, maar wat er al in staat, blijft zoals het is.
- **Alleen een ontwerp:** een besluit mag alleen een ontwerp hebben (nog geen `besluitdatum`, `definitief` of `in_werking`). De map heet dan naar de dag waarop het ontwerp zichtbaar werd en de id van het ontwerp, en krijgt de naam van het besluit zodra dat in het JSON-bestand staat. Zonder `in_werking` (de toestand staat nog niet in CVDR) zijn er nog geen toepasbare regels voor het besluit.
- **Eigen regels in README.txt:** `README.txt`, `README.md` en `index.csv` worden alleen opnieuw geschreven als er iets veranderde. Zet eigen regels (zoals een Match-bestand of aanleverpakket) onderaan `README.txt`, onder een regel die begint met `Handmatig`: die blijven staan. Een `README.txt` die vervangen wordt, staat eerst nog in `log/vorige-readme/`.

### Exitcodes

| Code | `code/archief.py` | `code/main.py` |
|---|---|---|
| 0 | klaar | klaar |
| 1 | het archief mist versies van toepasbare regels (of een onverwachte fout) | bestuursorgaan niet gevonden, RTR onbereikbaar, of geen enkele activiteit gelukt |
| 2 | JSON-bestand of opties onbruikbaar: geen geldige JSON, een veld ontbreekt, een datum of blad klopt niet, geen CVDR-nummer, geen activiteitenboom bij het OIN | opties onbruikbaar |
| 3 | het Rijk: een publicatie in de Staatscourant of het Staatsblad (`stcrt-`, `stb-`), een regeling uit het BWB, het OIN van BZK of een rijksactiviteit (`nl.imow-mnre…`) | het OIN van BZK of een rijksactiviteit |

## Tests

```bash
python -m unittest discover -s tests
```

## Projectstructuur

```
.
├── code/
│   ├── main.py          # Hoofdprogramma
│   ├── commands.py      # Command-line opties
│   ├── rtr.py           # Archivering: activiteiten lezen, outputs schrijven
│   ├── rtr_client.py    # Client voor de RTR (geen API-key)
│   ├── catalogus.py     # Bestuursorgaan vinden, activiteitenboom aflopen
│   ├── excel.py         # Excel-statusoverzicht
│   ├── archief.py       # Archief per besluit (publicaties, regeling, alle STTR-versies)
│   ├── versiehistorie.py # Alle STTR-versies van een bestuursorgaan uit de RTR
│   └── vertrouwen.py    # HTTPS op Windows met een nog onbekend root-certificaat
├── tests/
├── data/
│   ├── archief_CVDR707248.json  # Besluiten Waterschapsverordening HDSR
│   └── xlsx_example.PNG
├── log/                 # Gegenereerde output (niet in git)
├── archief/             # Archief per besluit (niet in git)
├── requirements.txt
├── requirements-archief.txt  # Playwright, voor de PDF van de hele regeling
└── README.md
```

## Licentie

Dit project is gelicenseerd onder de MIT License - zie het [LICENSE](LICENSE) bestand voor details.

## Bijdragen

Bijdragen zijn welkom! Zie [CONTRIBUTING.md](CONTRIBUTING.md) voor richtlijnen.

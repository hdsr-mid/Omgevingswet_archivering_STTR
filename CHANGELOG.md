# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `code/archief.py`: archief per definitief besluit van een regeling, met ontwerp en kennisgevingen, het besluit, de hele regeling per toestand (PDF), volledige RTR-vastleggingen op gekozen peildata en een map per dag waarop toepasbare regels later nog wijzigden; controleert dat elke STTR-versie gearchiveerd is
- `code/versiehistorie.py`: reconstrueert alle STTR-versies van een bestuursorgaan uit de RTR
- `data/archief_CVDR707248.json`: de besluiten van de Waterschapsverordening HDSR met hun publicaties en peildata
- Statusoverzicht kan per soort de begindatum van de versie tonen (`Geldig vanaf …`); het archief gebruikt dat
- STTR-manifest vermeldt einddatum, functioneleStructuurRef en SHA-256
- Archief: stap `bijlagen` haalt per publicatie de kaarten (GIO's als GML), PDF-informatieobjecten (zoals het inspraakrapport) en externe bijlagen op, met herkomst in `bijlagen.json`; een gelijke bijlage wordt één keer opgeslagen. Kaarten staan in `kaarten/` naast `RTR/`, alleen in de versie van het besluit (`ONTWERP_…` voor een kaart die alleen het ontwerp had)
- Archief: `README.txt` per besluit vervangt `LEESMIJ.md`; noemt wat erin zit en met een `!` wat erbij hoort maar ontbreekt (een publicatie, kaart of bijlage die niet gedownload is, een toestand die niet is afgedrukt), met `opmerking` uit het JSON-bestand
- Archief: versiegeschiedenis en logboek staan in `log/` (`--werkmap`) in plaats van in het archief, en het archief krijgt geen kopie van het JSON-bestand meer; een bestaand archief wordt bij de volgende run opgeruimd
- Archief: `Ontwerp_<jjjjmmdd>` en `Definitief_<jjjjmmdd>`, genoemd naar de dag waarop de publicatie zichtbaar werd in het DSO (nieuw: `zichtbaar_in_dso` in het JSON-bestand); een bestaand archief wordt bij de volgende run hernoemd
- `data/archief_CVDR707248.json`: zichtbaar_in_dso per besluit
- Archief: ook voor provincies en gemeenten; het `README.txt` noemt het blad van elke publicatie (Waterschapsblad, Provinciaal blad of Gemeenteblad, uit het begin van de id), en een onbekend blad is een fout in het JSON-bestand
- Archief: `root` in het JSON-bestand is niet meer nodig (ook `roots`, een lijst); zonder root zoekt de tool de activiteitenboom bij het OIN op, op elke datum van inwerkingtreding en op `--tot`
- Activiteitenboom: een root die op de gevraagde datum niet bestaat (HTTP 404), wordt overgeslagen zolang een andere root er wel is
- Archief: `python code/archief.py` zonder argumenten gebruikt `data/archief_CVDR707248.json`
- Archief vult aan: een compleet besluit (alles opgehaald, in werking, een vastlegging op elke peildatum) wordt overgeslagen en krijgt alleen nieuwe mappen `<datum>_gewijzigd`; `README.txt`, `README.md` en `index.csv` worden alleen opnieuw geschreven als er iets veranderde, met behoud van alles onder een regel `Handmatig` en een kopie van de vorige `README.txt` in `log/vorige-readme/`
- Archief: stap `zoeken` (`code/zoeken.py`) vindt nieuwe publicaties van de regeling (ontwerp, besluit, herpublicatie, kennisgeving) via de SRU van officielebekendmakingen.nl en hun metadata, zoekt de inwerkingtreding op in CVDR, kiest een peildatum en schrijft het JSON-bestand terug in zijn eigen opmaak
- Archief: een besluit met alleen een ontwerp, of nog niet in werking; de map van het ontwerp krijgt de naam van het besluit zodra dat er is
- Exitcodes: 2 als het JSON-bestand of een optie onbruikbaar is (archief), 3 voor het Rijk (archief en `main.py`), dat niet wordt ondersteund; 1 blijft: versies ontbreken
- `data/archief_CVDR707248.json`: kennisgeving wsb-2025-12605 (inspraak op de ontwerp-wijziging van de werkingsgebieden) bij het besluit van 15-07-2025
- Archief: één volledige vastlegging per besluit (`rtr_peildata` met meer dan één datum is een fout in het JSON-bestand); wat daarna inging, krijgt een map `<datum>_gewijzigd` tot het volgende besluit in werking treedt. Versies die in de 7 dagen vóór de vastlegging kwamen en weer gingen (de aanloop) worden niet opgenomen; `README.md` van het archief noemt ze apart, en ze tellen niet als ontbrekend
- `data/archief_CVDR707248.json`: één peildatum per besluit (15-01-2024, 01-08-2025 en 28-05-2026 blijven)

## [2.0.0] - 2026-11-17

### Changed
- Gegevens komen uit de openbare RTR-viewer-API (`/rtr-frontend/api`): geen API-key meer nodig
- Activiteiten worden gevonden door de activiteitenboom van het bestuursorgaan af te lopen; de PowerBI-exports (A1, A3) zijn niet meer nodig en verwijderd
- Werkingsgebieden krijgen hun naam (noemer) rechtstreeks uit de RTR
- RTR-client en activiteitenboom overgenomen uit Knowledgebase-CLI; alleen nog `xlsxwriter` als dependency
- Wijzigingsdatums worden gekleurd ten opzichte van de peildatum (`--date`) in plaats van vandaag
- Werkingsgebiedenbestand heet nu `…_STTR_werkingsgebieden.txt` en vermeldt de identificaties
- DMN-bestanden worden byte voor byte opgeslagen, met een `manifest.json`

### Added
- Waterschappen en provincies op naam, en ieder bestuursorgaan (ook gemeenten) via `--oin`
- Pre-omgeving via `https://pre.omgevingswet.overheid.nl/rtr-frontend/api`
- Kolom `Wijziging Maatregelen` en download van Maatregelen-STTR's
- `--rate` (verzoeken per seconde) en `--include-t5`
- Offline tests (`python -m unittest discover -s tests`)

### Fixed
- Werkingsgebiedenmatrix: rijen worden op URN gekoppeld in plaats van op naam, zodat hernoemde activiteiten, activiteiten met hoofdletters en activiteiten met dezelfde naam hun blauwe cellen niet meer verliezen of mengen
- Kolomkoppen van de matrix zijn namen in plaats van identificaties, en staan gedraaid zodat ze leesbaar zijn
- Elke activiteit wordt één keer opgehaald in plaats van twee keer
- Een fout bij één activiteit stopt de run niet meer en staat in de rij
- Werkzaamheden worden gescheiden door `; ` (hun namen bevatten zelf komma's)

### Added
- Comprehensive README.md with improved documentation
- requirements.txt for easier dependency management
- CONTRIBUTING.md with contribution guidelines
- CHANGELOG.md for tracking version changes
- Project structure documentation
- Detailed usage examples with correct command syntax
- Prerequisites section in documentation

### Changed
- Improved README structure and formatting
- Clarified command-line options with table format
- Fixed command examples (corrected syntax errors)
- Enhanced setup instructions

### Fixed
- Typos in README.md
- Incorrect command syntax in examples (removed extra spaces in flags)

## [1.0.0] - 2024

### Added
- Initial release
- RTR data archiving functionality
- Excel report generation
- DMN logic archiving with `--sttr` flag
- Werkingsgebieden export with `--location` flag
- Support for multiple overheden (waterschappen)
- API integration with IPLO Omgevingswet API (API-key)
- Support for both production and pre-production environments

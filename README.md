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

`code/archief.py` legt een regeling per definitief besluit vast: de publicaties, de hele regeling en élke versie van de toepasbare regels die eronder viel. Welke besluiten, publicaties en peildata dat zijn, staat in een JSON-bestand; voor de Waterschapsverordening van HDSR is dat [data/archief_CVDR707248.json](data/archief_CVDR707248.json).

```bash
pip install -r requirements-archief.txt
python code/archief.py data/archief_CVDR707248.json --rate 4
```

Per definitief besluit één map, genoemd naar de besluitdatum:

```
archief/
├── README.md, index.csv (elk bestand met SHA-256), versiehistorie.json
└── 20250715_wijzigingsbesluit-h2-3-4_wsb-2025-17997/
    ├── LEESMIJ.md
    ├── Ontwerp/      ontwerpbesluit en kennisgevingen (PDF uit het Waterschapsblad)
    ├── Definitief/   het besluit (PDF uit het Waterschapsblad) en de hele regeling
    │                 zoals die vanaf de inwerkingtreding gold (PDF)
    └── RTR/
        ├── 20250801_volledig/   alle toepasbare regels die op die dag golden
        ├── 20250808_volledig/   (status.xlsx, werkingsgebieden.txt, STTR/)
        └── 20250902_gewijzigd/  alleen wat op die dag inging (STTR/)
```

- **Volledig:** elke STTR-versie hoort bij het besluit in wiens periode (inwerkingtreding tot de volgende) hij inging. Staat hij in een volledige vastlegging van dat besluit, dan is hij daar gearchiveerd; anders krijgt hij een map `<datum>_gewijzigd` voor de dag waarop hij inging. Dat geldt voor latere wijzigingen en voor eerste uploads die binnen enkele dagen weer vervangen werden. `STTR/manifest.json` noemt per bestand welke versie het vervangt en waardoor het vervangen werd; vervallen regelbeheerobjecten staan erin zonder bestand. Het `README.md` van het archief controleert dat alle versies uit `versiehistorie.json` erin staan.
- **Versiegeschiedenis:** de RTR geeft per regelbeheerobject alleen de versie op een datum. De tool loopt de activiteitenboom maandelijks en op elke peildatum af en volgt per regelbeheerobject de keten van versies (`code/versiehistorie.py`).
- **Hele regeling:** de wijzigingsbesluiten bevatten alleen de wijzigingen. De hele regeling wordt per toestand afgedrukt van [lokaleregelgeving.overheid.nl](https://lokaleregelgeving.overheid.nl/CVDR707248), met de afdrukopmaak van die site, in A4 en met bron en afdrukdatum in de voettekst. Dat gebeurt met Playwright: op Windows met de geïnstalleerde Edge, elders met Chrome of met `python -m playwright install chromium-headless-shell`, of met `--browser <pad>`.
- **Statusoverzicht:** in het archief toont `status.xlsx` per soort de datum waarop de versie inging (`Geldig vanaf …`). De laatste wijzigingsdatum van een versie overschrijft de RTR zodra die versie vervangen wordt, dus voor een peildatum in het verleden zegt die niets.
- **Stappen:** `--stappen publicaties,regeling,rtr,wijzigingen,index` (standaard alle). Wat er al is, wordt overgeslagen; een nieuw besluit toevoegen aan het JSON-bestand en opnieuw draaien vult het archief aan.

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

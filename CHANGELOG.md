# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

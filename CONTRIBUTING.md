# Bijdragen aan Omgevingswet Archivering STTR

Bedankt voor je interesse om bij te dragen aan dit project! Deze richtlijnen helpen je om effectief bij te dragen.

## Hoe kun je bijdragen?

### Bugs rapporteren

Als je een bug vindt, maak dan een issue aan met:
- Een duidelijke beschrijving van het probleem
- Stappen om het probleem te reproduceren
- Verwacht gedrag versus actueel gedrag
- Je Python-versie en operating system
- Relevante log-output of error messages

### Feature requests

Voor nieuwe functionaliteiten:
- Beschrijf de gewenste functionaliteit en waarom deze nuttig zou zijn
- Geef voorbeelden van hoe de functionaliteit gebruikt zou worden
- Overweeg of de functionaliteit past binnen de scope van het project

### Code bijdragen

1. **Fork het repository**
   ```bash
   git clone https://github.com/hdsr-mid/Omgevingswet_archivering_STTR.git
   cd Omgevingswet_archivering_STTR
   ```

2. **Maak een feature branch**
   ```bash
   git checkout -b feature/jouw-feature-naam
   ```

3. **Installeer dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Maak je wijzigingen**
   - Houd je code consistent met de bestaande code style
   - Voeg comments toe waar nuttig
   - Test je wijzigingen grondig

5. **Commit je wijzigingen**
   ```bash
   git add .
   git commit -m "Beschrijving van je wijziging"
   ```

6. **Push naar je fork**
   ```bash
   git push origin feature/jouw-feature-naam
   ```

7. **Maak een Pull Request**
   - Geef een duidelijke beschrijving van je wijzigingen
   - Referentie naar relevante issues
   - Beschrijf hoe je de wijzigingen getest hebt

## Code Style

- Gebruik betekenisvolle variabele- en functienamen
- Volg PEP 8 Python style guide waar mogelijk
- Voeg docstrings toe aan functies en klassen
- Houd functies klein en gefocust op één taak

## Testing

De tests draaien offline tegen een nagebootste RTR:

```bash
python -m unittest discover -s tests
```

Test daarnaast met de echte RTR:
- Test met verschillende overheden (`--overheid`, en een gemeente via `--oin`)
- Test met verschillende datums en met `--env pre`
- Test met verschillende combinaties van flags (`--sttr`, `--location`)
- Controleer of de gegenereerde Excel-bestanden correct zijn

## Commit Messages

- Gebruik duidelijke, beschrijvende commit messages
- Begin met een werkwoord in de tegenwoordige tijd (bijv. "Add", "Fix", "Update")
- Houd de eerste regel onder de 50 karakters
- Voeg extra details toe in de commit body indien nodig

Voorbeelden:
```
Add support for new waterschap
Fix date parsing for edge cases
Update README with clearer examples
```

## Vragen?

Als je vragen hebt over het bijdragen, open dan een issue of neem contact op met de maintainers.

## Licentie

Door bij te dragen aan dit project, ga je ermee akkoord dat je bijdragen worden gelicenseerd onder de MIT License.

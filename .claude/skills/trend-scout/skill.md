---
name: trend-scout
description: "Detecteer trending Polymarket markten en value bets voordat odds verschuiven"
---

# /trend-scout — Trend Researcher

Identificeert opkomende markten op Polymarket voordat odds verschuiven. Zoekt naar volume-spikes, sentiment shifts, en cross-referentie met externe data.

**Gebaseerd op:** agency-agents/product-trend-researcher (aangepast voor Polymarket prediction markets)

**Status: GEPARKEERD** — API endpoints niet gevalideerd. Pas activeren na validatie tegen Polymarket docs.

---

## Gebruik

```
/trend-scout                — Scan voor trending markten
/trend-scout sports         — Focus op sports markten
/trend-scout volume         — Markten met volume-spikes
/trend-scout wallets        — Waar zijn onze wallets actief?
```

---

## Stap 1: Data Verzamelen

### A. Polymarket trending markten

```bash
# Actieve markten met volume
ssh root@78.141.222.227 'curl -s "https://clob.polymarket.com/markets?active=true&limit=50" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in sorted(data, key=lambda x: float(x.get(\"volume\", 0)), reverse=True)[:20]:
    vol = float(m.get(\"volume\", 0))
    title = m.get(\"question\", \"\")[:60]
    end = m.get(\"end_date_iso\", \"\")[:10]
    print(f\"  \${vol:>10,.0f}  {end}  {title}\")
"'
```

### B. Wallet activiteit (waar zetten onze wallets op?)

```bash
# Recente signalen van de bot
ssh root@78.141.222.227 'journalctl -u bottie --since "6 hours ago" --no-pager 2>/dev/null | grep "SIGNAL:" | tail -20'
```

### C. Snel-resolvende markten identificeren

Focus op markten die:
- Binnen 7 dagen resolven (capital efficiency)
- Sports/events met duidelijke uitkomst
- Volume > $10K (liquiditeit)
- Niet "up or down" of crypto noise

---

## Stap 2: Weak Signal Detection

### Patronen om op te letten

1. **Volume spike** — Markt gaat van $1K naar $10K volume in 24u → iets is aan het gebeuren
2. **Wallet clustering** — Meerdere tracked wallets nemen positie in dezelfde markt → consensus signal
3. **Odds shift** — Prijs beweegt >10% in korte tijd → nieuw informatiesignaal
4. **Nieuw aanbod** — Markten die net gelanceerd zijn met snelgroeiend volume → early mover voordeel
5. **Kalender events** — Wedstrijden, verkiezingen, deadlines die binnenkort resolven

### Signaal sterkte beoordelen

| Indicator | Zwak | Matig | Sterk |
|-----------|------|-------|-------|
| Volume groei | <2x in 24u | 2-5x | >5x |
| Wallet overlap | 1 wallet | 2 wallets | 3+ wallets |
| Tijd tot resolutie | >7 dagen | 3-7 dagen | <3 dagen |
| Prijs range | <0.20 of >0.80 | 0.20-0.35 of 0.65-0.80 | 0.35-0.65 |

---

## Stap 3: Cross-Referentie

Bij sports markten:
- Check of de wedstrijd vandaag/morgen is (snel resolving)
- Zijn er blessures, schorsingen, weerberichten?
- Hoe presteren de teams recent?

Bij andere markten:
- Is er nieuws dat de markt beweegt?
- Is de markt liquide genoeg om in/uit te stappen?

---

## Stap 4: Rapporteer

```markdown
# Trend Scout — [datum]

## Trending Markten (top 5 signaal sterkte)

| Markt | Volume | Resolutie | Signaal | Wallets actief |
|-------|--------|-----------|---------|----------------|
| [titel] | $XXK | [datum] | [sterk/matig] | [namen] |

## Wallet Clustering
[Welke markten hebben meerdere wallets?]

## Aanbevelingen
1. [Markt]: [waarom interessant] — [actie suggestie]
2. ...

## Niet Handelen
[Markten die trending zijn maar waar we GEEN edge hebben]
[Reden per markt]
```

---

## Regels

| DO | DON'T |
|----|-------|
| Focus op snel-resolvende markten (<7 dagen) | Illiquide long-term markten |
| Cross-refereer met wallet activiteit | Blind op volume af gaan |
| Sports en events met duidelijke uitkomst | Crypto, politiek, meme markten |
| Signaal sterkte kwantificeren | "Dit ziet er interessant uit" |
| Rapporteer, laat user beslissen | Zelf trades plaatsen |

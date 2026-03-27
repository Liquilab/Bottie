"""Cannae Intelligence Engine — reverse engineering Cannae's trading strategy.

Modules:
  Descriptive (WAT doet Cannae):
    1. first_principles  — ROI decomposition, edge stability, risk metrics
    2. event_menu        — Game selectie vs beschikbaar aanbod
    3. entry_price       — Prijsdistributie, implied edge, dip buying
    4. sizing_model      — Sizing patronen (quartiles, per league/type)
    5. temporal          — Timing patronen (uur, dag, batches)
    7. odds_edge         — PM prijs vs bookmaker odds

  Strategisch (WAAROM en HOE — reverse engineering):
    9.  conviction_model  — YES/NO ratio, conviction vs ROI, draw plays
    10. game_selection    — Wat maakt een game aantrekkelijk? Selectiesignalen
    11. hedge_structure   — Hoe bouwt Cannae multi-leg games? Optimale structuur
    12. predictive_model  — Voorspel wat Cannae zou doen op een nieuw game
"""

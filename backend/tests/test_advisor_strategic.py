"""Strategic-advisor upgrade: per-segment growth (momentum, not just current totals) in
the analytics layer, and the ADVISOR_INSTRUCTIONS reasoning contract.

Run:  cd backend && python -m pytest tests/test_advisor_strategic.py -q
Offline — pure functions + string checks.
"""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app import nl2sql                      # noqa: E402
from app.agent import analytics             # noqa: E402


# ---- per-segment growth (the "which region is gaining momentum" angle) ----
def test_growth_by_segment_ranks_momentum_not_size():
    # provinsi × month × gmv: Jabar is biggest but FLAT; Banten is smaller but SURGING.
    # A raw total ranks Jabar #1; growth_by_segment must surface Banten as fastest-growing.
    rows = [
        ["Jawa Barat", 202605, 1000], ["Jawa Barat", 202606, 1010],   # +1%
        ["Banten",     202605, 200],  ["Banten",     202606, 400],    # +100%
        ["DKI",        202605, 500],  ["DKI",        202606, 450],     # -10%
    ]
    d = analytics.summarize(["provinsi", "month_id", "gmv"], rows)
    g = d["growth_by_segment"]
    assert g["dimension"] == "provinsi" and g["value_column"] == "gmv"
    assert g["fastest_growing"]["segment"] == "Banten" and g["fastest_growing"]["pct_change"] == 100
    assert g["fastest_declining"]["segment"] == "DKI" and g["fastest_declining"]["pct_change"] == -10


def test_growth_by_segment_surfaces_small_start_large_end_surger():
    # THE point of the feature: a segment small today but surging must NOT be dropped just
    # because its starting base was small — it ENDS large, so it is material. (regression for
    # the review bug where a first-value floor discarded exactly this signal)
    rows = [
        ["North", 202601, 10],   ["North", 202602, 2000],  # +19900%, ends large -> the momentum leader
        ["South", 202601, 5000], ["South", 202602, 6000],  # +20%, biggest but flat-ish
        ["East",  202601, 3000], ["East",  202602, 3100],  # +3.3%
    ]
    g = analytics.summarize(["region", "month_id", "gmv"], rows)["growth_by_segment"]
    assert g["fastest_growing"]["segment"] == "North"


def test_growth_by_segment_dominant_segment_does_not_mask_surgers():
    # one giant segment must not inflate the floor so that real surgers vanish (review bug:
    # max-pegged floor returned None / wrong leader here)
    rows = [
        ["Giant", 202605, 100000], ["Giant", 202606, 110000],  # +10%
        ["S1",    202605, 50],     ["S1",    202606, 500],      # +900%
        ["S2",    202605, 60],     ["S2",    202606, 700],      # +1067%
    ]
    g = analytics.summarize(["kota", "month_id", "gmv"], rows)["growth_by_segment"]
    assert g["fastest_growing"]["segment"] == "S2"


def test_growth_by_segment_drops_noise_tiny_at_both_ends():
    # a segment tiny at BOTH ends is noise and must not be crowned on a meaningless %
    rows = [
        ["Real",  202605, 300], ["Real",  202606, 900],    # +200%, material
        ["Noise", 202605, 1],   ["Noise", 202606, 5],      # ends at 5 -> immaterial
        ["Flat",  202605, 800], ["Flat",  202606, 800],
    ]
    g = analytics.summarize(["kota", "month_id", "gmv"], rows)["growth_by_segment"]
    assert g["fastest_growing"]["segment"] == "Real"


def test_growth_by_segment_sign_guard_no_false_decliner():
    # when every segment grows, there is NO fastest_declining (review bug: the slowest grower
    # was mislabeled 'declining')
    rows = [["A", 202601, 100], ["A", 202602, 110],        # +10%
            ["B", 202601, 100], ["B", 202602, 105]]        # +5%
    g = analytics.summarize(["region", "month_id", "gmv"], rows)["growth_by_segment"]
    assert g["fastest_growing"]["segment"] == "A"
    assert "fastest_declining" not in g                    # nobody actually declined
    # ...and symmetrically, when all decline there is no fastest_growing
    rows2 = [["A", 202601, 100], ["A", 202602, 90],        # -10%
             ["B", 202601, 100], ["B", 202602, 80]]        # -20%
    g2 = analytics.summarize(["region", "month_id", "gmv"], rows2)["growth_by_segment"]
    assert g2["fastest_declining"]["segment"] == "B"
    assert "fastest_growing" not in g2


def test_growth_by_segment_needs_period_and_dimension():
    # no period column -> no growth_by_segment (nothing to compare over time)
    d = analytics.summarize(["provinsi", "gmv"], [["Jabar", 100], ["Banten", 50]])
    assert "growth_by_segment" not in (d or {})


# ---- advisor reasoning contract ----
def test_advisor_prompt_pushes_grounded_creativity():
    t = nl2sql.ADVISOR_INSTRUCTIONS.lower()
    # the strategic angles the flat answer was missing
    assert "momentum" in t and "growth_by_segment" in t
    assert "gap" in t or "mismatch" in t
    assert "assumption" in t or "trade-off" in t
    # missing-data must become a next step, not a dead end
    assert "total_cbm" in t or "missing" in t
    # the hard rule survives the creativity upgrade
    assert "never invent" in t


# ---- number formatting (readability) ----
def test_large_values_lose_the_decimal_tail():
    assert analytics._r(2785176381.1759) == 2785176381      # no 4-decimal tail on a billion
    assert analytics._r(133539869.0031) == 133539869
    assert analytics._r(999.5) == pytest.approx(999.5)      # small values keep precision
    assert analytics._r(32.1277) == pytest.approx(32.1277)  # a percentage stays exact
    # a summed GMV total is rendered clean, not as ...,1759
    d = analytics.summarize(["marketplace", "gmv"],
                            [["Shopee", 1500000000.4213], ["TikTok", 1285176380.7546]])
    assert d["measures"][0]["total"] == 2785176381


def test_number_format_instructions_cover_currency_rounding_scale():
    t = nl2sql.NUMBER_FORMAT_INSTRUCTIONS.lower()
    assert "rp" in t                                        # currency prefix for monetary figures
    assert "miliar" in t and "juta" in t                    # localized readable scale
    assert "billion" in t                                   # english scale too
    assert "decimal" in t and "1000" in t                   # rounding rule
    assert "traceable" in t                                 # exact value must survive for the verifier


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

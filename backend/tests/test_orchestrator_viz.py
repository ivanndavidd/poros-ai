"""Dashboard/latency fixes: chart/artifact blocks are found in an answer, and docs are
only searched for document/how-to questions (not pure data questions).

Run:  cd backend && python -m pytest tests/test_orchestrator_viz.py -q

Offline. Motivated by a live case where "Dashboard penjualan bulan ini" returned text only
(a repair pass dropped the chart; since chat overhaul item 6 nothing rewrites an answer)
and ran slowly (search_docs pulled an unrelated onboarding-deck citation).
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.agent import orchestrator as orch          # noqa: E402

CHART = '```chart\n{"type":"bar","labels":["A"],"series":[{"name":"x","data":[1]}]}\n```'


def test_viz_blocks_extracts_chart_and_artifact():
    text = f'ringkasan\n{CHART}\nlagi\n```artifact\n<html></html>\n```'
    blocks = orch._viz_blocks(text)
    assert len(blocks) == 2
    assert "```chart" in blocks[0]
    assert "```artifact" in blocks[1]


def test_doc_cue_only_fires_for_document_questions():
    # pure data questions -> no docs
    assert not orch._DOC_CUE.search("Dashboard penjualan bulan ini")
    assert not orch._DOC_CUE.search("berapa total gmv shopee")
    # document / how-to questions -> docs
    assert orch._DOC_CUE.search("bagaimana cara supervisor membuat jadwal shift")
    assert orch._DOC_CUE.search("apa isi SOP retur barang?")
    assert orch._DOC_CUE.search("panduan penggunaan aplikasi")


def test_insight_instructions_localise_to_user_language():
    # Regression guard: the shared insight/recommendation spec must NOT hardcode Indonesian
    # (it was, which pushed English questions to Indonesian answers). It must tell the model
    # to match the user's language and offer the English label too.
    from app.nl2sql import INSIGHT_INSTRUCTIONS
    low = INSIGHT_INSTRUCTIONS.lower()
    assert "same language" in low
    assert "recommendation" in low                       # English label present
    assert "pola/tren" not in INSIGHT_INSTRUCTIONS        # old Indonesian-only descriptions gone

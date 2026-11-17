"""Offline tests for the generic inspect -> observe -> replan Deep Research loop."""
from types import SimpleNamespace

from app.agent import research_agent
from app.skills.registry import Ctx


def _ctx(policy=None):
    return Ctx(
        tenant_id="t_x",
        role="Analyst",
        role_policy=policy or {"full_access": True},
        question="Mengapa revenue turun dan apa tindakan terbaik?",
        answer_purpose="answer_complex",
        deep_analysis=True,
    )


def _datasource():
    return {
        "id": "ds_sales",
        "name": "Sales",
        "type": "postgres",
        "conn": {},
        "mdl": {
            "tables": {
                "sales_monthly": ["month_id", "region", "email", "revenue", "orders"],
                "inventory_daily": ["day", "warehouse", "stock"],
            },
            "allowed_tables": ["sales_monthly", "inventory_daily"],
            "pii_columns": ["email"],
            "descriptions": {
                "sales_monthly": "Monthly revenue and order performance by region.",
                "inventory_daily": "Daily stock by warehouse.",
            },
        },
    }


def _tool_result(calls, text=""):
    return SimpleNamespace(
        text=text,
        tool_calls=calls,
        model="fake-model",
        provider="fake",
        prompt_tokens=10,
        completion_tokens=5,
        cost=0.001,
    )


def _completion(text):
    return SimpleNamespace(
        text=text,
        model="fake-model",
        provider="fake",
        prompt_tokens=5,
        completion_tokens=3,
        cost=0.001,
    )


def _call(call_id, name, **arguments):
    return {"id": call_id, "name": name, "arguments": arguments}


def _bypass_phase_gate(monkeypatch):
    """Legacy loop tests isolate replanning/critic behavior from phase-gate policy."""
    monkeypatch.setattr(research_agent, "_phase_state", lambda *_a, **_k: {
        "phase": "ready_for_critic",
        "sql_ready": True,
        "finish_ready": True,
        "minimum_hypotheses": 0,
        "minimum_profiles": 0,
        "distinct_profiled_tables": [],
        "scoped_tables": [],
        "missing_before_sql": [],
        "missing_before_finish": [],
    })


def test_adaptive_loop_observes_results_and_critic_forces_replan(monkeypatch):
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result([_call("c1", "search_catalog", query="revenue trend driver")]),
        _tool_result([_call(
            "c2", "describe_table", datasource="ds_sales", tables=["sales_monthly"],
        )]),
        _tool_result([_call(
            "c3", "run_sql", datasource="ds_sales", purpose="ukur tren revenue",
            sql="SELECT month_id, SUM(revenue) AS revenue FROM sales_monthly GROUP BY month_id",
        )]),
        _tool_result([_call(
            "c4", "finish_research", reason="tren tersedia", remaining_gaps=[],
        )]),
        _tool_result([_call(
            "c5", "run_sql", datasource="ds_sales", purpose="uji driver wilayah",
            sql="SELECT region, SUM(revenue) AS revenue FROM sales_monthly GROUP BY region",
        )]),
        _tool_result([_call(
            "c6", "finish_research", reason="tren dan driver tersedia", remaining_gaps=[],
        )]),
    ]
    controller_states = []

    def fake_tools(_tid, _purpose, messages, _tools, **_kwargs):
        controller_states.append(messages[1]["content"])
        return script.pop(0)

    critic = iter([
        '{"complete": false, "reason": "baru ada tren", '
        '"gaps": ["belum ada driver"], "next_actions": ["breakdown per region"]}',
        '{"complete": true, "reason": "tren dan driver sudah diuji", '
        '"gaps": [], "next_actions": []}',
    ])
    monkeypatch.setattr(research_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(
        research_agent.llm, "complete", lambda *_a, **_k: _completion(next(critic)),
    )

    executed = []

    def fake_execute(_ctx, ds, sql, engine):
        executed.append((ds["id"], sql, engine))
        region_query = "region" in sql.lower()
        return {
            "ok": True,
            "columns": ["region", "revenue"] if region_query else ["month_id", "revenue"],
            "rows": [["Jawa", 80]] if region_query else [[202601, 100], [202602, 80]],
            "safe_sql": sql,
            "generated_sql": sql,
            "engine": engine,
            "dialect": "postgres",
            "datasource_id": ds["id"],
            "datasource_name": ds["name"],
            "datasource_type": ds["type"],
            "pii_columns": ["email"],
        }

    monkeypatch.setattr(research_agent, "execute_governed", fake_execute)
    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=10, max_sql=8, max_catalog=6, max_docs=0, max_web=0,
            max_critic_rounds=2, max_seconds=60,
        ),
    )

    assert len(executed) == 2
    assert out["research"]["completed"] is True
    assert out["research"]["stop_reason"] == "critic_approved"
    assert out["research"]["critic_rounds"] == 2
    assert out["coverage"]["missing"] == []
    # The second decision sees the first governed result, proving observe -> replan.
    assert "sql_1" in controller_states[3]
    assert "belum ada driver" in controller_states[4]


def test_research_state_masks_pii_before_next_model_turn(monkeypatch):
    script = [
        _tool_result([_call(
            "c1", "run_sql", datasource="ds_sales", purpose="cek transaksi",
            sql="SELECT email, revenue FROM sales_monthly",
        )]),
        _tool_result([_call(
            "c2", "finish_research", reason="cukup", remaining_gaps=[],
        )]),
    ]
    seen = []

    def fake_tools(_tid, _purpose, messages, _tools, **_kwargs):
        seen.append(messages[1]["content"])
        return script.pop(0)

    monkeypatch.setattr(research_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "ok", "gaps": [], "next_actions": []}',
        ),
    )
    monkeypatch.setattr(research_agent, "execute_governed", lambda _ctx, ds, sql, engine: {
        "ok": True,
        "columns": ["email", "revenue"],
        "rows": [["secret.person@example.com", 125]],
        "safe_sql": sql,
        "generated_sql": sql,
        "engine": engine,
        "dialect": "postgres",
        "datasource_id": ds["id"],
        "datasource_name": ds["name"],
        "datasource_type": ds["type"],
        "pii_columns": ["email"],
    })

    research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=6, max_sql=4, max_catalog=2, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )
    assert "secret.person@example.com" not in seen[1]
    assert "[PII:email]" in seen[1]


def test_duplicate_tool_action_is_suppressed(monkeypatch):
    duplicate = _call(
        "c1", "run_sql", datasource="ds_sales", purpose="baseline",
        sql="SELECT SUM(revenue) AS revenue FROM sales_monthly",
    )
    script = [
        _tool_result([duplicate]),
        _tool_result([{**duplicate, "id": "c2"}]),
        _tool_result([_call(
            "c3", "finish_research", reason="cukup", remaining_gaps=[],
        )]),
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools",
        lambda *_a, **_k: script.pop(0),
    )
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "ok", "gaps": [], "next_actions": []}',
        ),
    )
    calls = []

    def fake_execute(_ctx, ds, sql, engine):
        calls.append(sql)
        return {
            "ok": True, "columns": ["revenue"], "rows": [[100]],
            "safe_sql": sql, "generated_sql": sql, "engine": engine, "dialect": "postgres",
            "datasource_id": ds["id"], "datasource_name": ds["name"],
            "datasource_type": ds["type"], "pii_columns": [],
        }

    monkeypatch.setattr(research_agent, "execute_governed", fake_execute)
    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=6, max_sql=4, max_catalog=2, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )
    assert len(calls) == 1
    assert out["research"]["tool_counts"]["run_sql"] == 1
    assert any(item["status"] == "duplicate" for item in out["research"]["tool_trace"])


def test_empty_sql_is_not_evidence_and_research_retries(monkeypatch):
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result([_call(
            "c1", "run_sql", datasource="ds_sales", purpose="current-day baseline",
            sql=(
                "SELECT region, SUM(revenue) AS revenue FROM sales_monthly "
                "WHERE month_id = 20260728 GROUP BY region"
            ),
        )]),
        _tool_result([_call(
            "c2", "run_sql", datasource="ds_sales", purpose="available-history baseline",
            sql=(
                "SELECT month_id, region, SUM(revenue) AS revenue "
                "FROM sales_monthly GROUP BY month_id, region"
            ),
        )]),
        _tool_result([_call(
            "c3", "finish_research", reason="historical evidence found",
            remaining_gaps=[],
        )]),
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools",
        lambda *_a, **_k: script.pop(0),
    )
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "retry found evidence", '
            '"gaps": [], "next_actions": []}',
        ),
    )

    def fake_execute(_ctx, ds, sql, engine):
        empty = "20260728" in sql
        return {
            "ok": True,
            "columns": ["region", "revenue"],
            "rows": [] if empty else [["Jawa", 100]],
            "safe_sql": sql,
            "generated_sql": sql,
            "engine": engine,
            "dialect": "postgres",
            "datasource_id": ds["id"],
            "datasource_name": ds["name"],
            "datasource_type": ds["type"],
            "pii_columns": [],
        }

    monkeypatch.setattr(research_agent, "execute_governed", fake_execute)
    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=6, max_sql=4, max_catalog=2, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )

    assert len(out["queries"]) == 2
    assert len(out["results"]) == 1
    assert out["results"][0]["rows"] == [["Jawa", 100]]
    trace = out["research"]["tool_trace"]
    assert any(item["tool"] == "run_sql" and item["status"] == "empty" for item in trace)
    assert out["research"]["completed"] is True


def test_catalog_discovery_hides_role_unauthorized_tables():
    ctx = _ctx({"authorized_tables": ["sales_monthly"], "full_access": False})
    result = research_agent.catalog_search(
        ctx, [_datasource()], {"query": "revenue stock", "limit": 12},
    )
    tables = [
        item["table"]
        for datasource in result["datasources"]
        for item in datasource["matches"]
    ]
    assert "sales_monthly" in tables
    assert "inventory_daily" not in tables


def test_critic_cannot_approve_without_governed_sql(monkeypatch):
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "looks fine", "gaps": [], "next_actions": []}',
        ),
    )
    verdict = research_agent._critic(_ctx(), research_agent.Ledger())
    assert verdict["complete"] is False
    assert "no successful governed SQL evidence" in verdict["gaps"]


def test_budget_exhaustion_still_runs_web_and_final_critic(monkeypatch):
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result([_call(
            "s1", "run_sql", datasource="ds_sales", purpose="baseline",
            sql="SELECT SUM(revenue) AS revenue FROM sales_monthly",
        )]),
        *[
            _tool_result([_call(f"c{index}", "search_catalog", query=f"driver {index}")])
            for index in range(2, 7)
        ],
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools",
        lambda *_a, **_k: script.pop(0),
    )
    critic = iter([
        '{"complete": false, "reason": "perlu lanjut", "gaps": ["counter-check"], '
        '"next_actions": ["cari counter-check"]}',
        '{"complete": true, "reason": "cukup setelah final review", '
        '"gaps": [], "next_actions": []}',
    ])
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(next(critic)),
    )
    monkeypatch.setattr(research_agent, "execute_governed", lambda _ctx, ds, sql, engine: {
        "ok": True, "columns": ["revenue"], "rows": [[100]],
        "safe_sql": sql, "generated_sql": sql, "engine": engine, "dialect": "postgres",
        "datasource_id": ds["id"], "datasource_name": ds["name"],
        "datasource_type": ds["type"], "pii_columns": [],
    })
    monkeypatch.setattr(
        research_agent.web_research,
        "research",
        lambda *_a, **_k: {
            "ok": True, "provider": "openai", "model": "fake-web",
            "text": "External benchmark.", "queries": ["official benchmark"],
            "sources": [{
                "label": "Web: Official",
                "title": "Official",
                "url": "https://example.org/official",
            }],
            "usage": {
                "model": "fake-web", "prompt_tokens": 10,
                "completion_tokens": 4, "cost": 0.001,
            },
        },
    )

    out = research_agent.run_research(
        _ctx(), [_datasource()], enable_web=True,
        budget=research_agent.ResearchBudget(
            max_rounds=6, max_sql=2, max_catalog=8, max_docs=0, max_web=2,
            max_critic_rounds=2, max_seconds=60,
        ),
    )
    assert out["research"]["completed"] is True
    assert out["research"]["stop_reason"] == "critic_approved_after_budget"
    assert out["research"]["critic_rounds"] == 2
    assert out["research"]["tool_counts"]["web_search"] == 1
    assert out["web"]["sources"][0]["label"] == "Web: Official"


def test_exhausted_tools_are_removed_from_next_round():
    budget = research_agent.ResearchBudget(
        max_rounds=6, max_sql=2, max_catalog=2, max_docs=1, max_web=1,
        max_critic_rounds=1, max_seconds=60,
    )
    ledger = research_agent.Ledger()
    ledger.counts.update({
        "run_sql": 2,
        "search_catalog": 2,
        "live_discovery": budget.max_discovery,
        "profile_table": budget.max_profile,
        "verify_semantics": budget.max_semantic,
        "update_hypothesis": budget.max_hypotheses * 3,
        "describe_table": 1,
        "search_docs": 1,
        "web_search": 1,
    })
    names = {
        tool["function"]["name"]
        for tool in research_agent._tools(
            enable_docs=True, enable_web=True, budget=budget, ledger=ledger,
        )
    }
    assert names == {"finish_research"}


def test_approved_best_available_research_preserves_limitations(monkeypatch):
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result([_call(
            "s1", "run_sql", datasource="ds_sales", purpose="baseline",
            sql="SELECT SUM(revenue) AS revenue FROM sales_monthly",
        )]),
        _tool_result([_call(
            "f1", "finish_research", reason="best available evidence gathered",
            remaining_gaps=["warehouse capacity is not in the catalog"],
        )]),
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools", lambda *_a, **_k: script.pop(0),
    )
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "sufficient with caveat", '
            '"gaps": ["warehouse capacity is not in the catalog"], "next_actions": []}',
        ),
    )
    monkeypatch.setattr(research_agent, "execute_governed", lambda _ctx, ds, sql, engine: {
        "ok": True, "columns": ["revenue"], "rows": [[100]],
        "safe_sql": sql, "generated_sql": sql, "engine": engine, "dialect": "postgres",
        "datasource_id": ds["id"], "datasource_name": ds["name"],
        "datasource_type": ds["type"], "pii_columns": [],
    })

    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=6, max_sql=2, max_catalog=2, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )
    assert out["research"]["completed"] is True
    assert out["coverage"]["missing"] == []
    assert out["research"]["remaining_gaps"] == [
        "warehouse capacity is not in the catalog",
    ]


def test_live_layers_and_hypothesis_ledger_are_auditable(monkeypatch):
    datasource = _datasource()
    datasource["conn"] = {"host": "metadata.test"}
    script = [
        _tool_result([_call(
            "c0", "search_catalog", query="revenue decline by region",
        )]),
        _tool_result([_call(
            "h1", "update_hypothesis",
            statement="Penurunan revenue terutama berasal dari wilayah Jawa",
            status="testing", rationale="perlu breakdown wilayah",
            analytical_angle="regional contribution",
            scope={
                "datasource": "ds_sales",
                "tables": ["sales_monthly"],
                "metrics": ["revenue"],
                "dimensions": ["region"],
                "time_window": "available comparable months",
            },
            falsification_test=(
                "Reject if another region explains a larger share of the decline."
            ),
            decision_effect="Determines which region needs corrective action first.",
            assumptions=["revenue adalah metric resmi"],
        )]),
        _tool_result([_call(
            "p1", "profile_table", datasource="ds_sales",
            table="sales_monthly", sample_size=100,
        )]),
        _tool_result([_call(
            "m1", "verify_semantics", datasource="ds_sales",
            assertions=[{
                "concept": "official revenue",
                "table": "sales_monthly",
                "columns": ["revenue", "region"],
            }],
        )]),
        _tool_result([_call(
            "s1", "run_sql", datasource="ds_sales", hypothesis_id="H1",
            purpose="uji kontribusi revenue per wilayah",
            sql="SELECT region, SUM(revenue) AS revenue FROM sales_monthly GROUP BY region",
        )]),
        _tool_result([_call(
            "h2", "update_hypothesis", id="H1",
            statement="Penurunan revenue terutama berasal dari wilayah Jawa",
            status="supported", rationale="breakdown menunjukkan kontribusi terbesar",
            supporting_evidence=["sql_1"], assumptions=["revenue adalah metric resmi"],
            next_tests=[],
        )]),
        _tool_result([_call(
            "f1", "finish_research", reason="hipotesis diuji", remaining_gaps=[],
        )]),
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools",
        lambda *_args, **_kwargs: script.pop(0),
    )
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_args, **_kwargs: _completion(
            '{"complete": true, "reason": "cukup", "gaps": [], "next_actions": []}',
        ),
    )
    monkeypatch.setattr(
        research_agent.discovery,
        "discover_datasource",
        lambda ds, _mdl, **_kwargs: {
            "ok": True,
            "datasource_id": ds["id"],
            "datasource_name": ds["name"],
            "datasource_type": ds["type"],
            "authorized_relation_count": 1,
            "hidden_relation_count": 0,
            "relations": [{
                "table": "sales_monthly",
                "kind": "table",
                "columns": [{"name": "region"}, {"name": "revenue"}],
                "schema_drift": {},
            }],
        },
    )
    monkeypatch.setattr(
        research_agent.discovery,
        "profile_relation",
        lambda ds, _mdl, table, **_kwargs: {
            "ok": True, "datasource_id": ds["id"], "table": table,
            "sample_rows": 20, "estimated_rows": 1000,
            "primary_key": [], "candidate_keys_from_sample": [],
            "columns": [
                {"name": "region", "semantic_role": "dimension"},
                {"name": "revenue", "semantic_role": "measure"},
            ],
            "warnings": [], "limitations": ["sample based"],
        },
    )
    monkeypatch.setattr(
        research_agent.discovery,
        "verify_semantics",
        lambda _mdl, _assertions: {
            "ok": True,
            "checks": [{"status": "verified", "table": "sales_monthly"}],
            "summary": {"verified": 1, "partial": 0, "invalid": 0},
        },
    )
    monkeypatch.setattr(
        research_agent,
        "execute_governed",
        lambda _ctx, ds, sql, engine: {
            "ok": True, "columns": ["region", "revenue"], "rows": [["Jawa", 80]],
            "safe_sql": sql, "generated_sql": sql, "engine": engine, "dialect": "postgres",
            "datasource_id": ds["id"], "datasource_name": ds["name"],
            "datasource_type": ds["type"], "pii_columns": [],
        },
    )

    out = research_agent.run_research(
        _ctx(), [datasource],
        budget=research_agent.ResearchBudget(
            max_rounds=8, max_sql=4, max_catalog=4,
            max_discovery=3, max_profile=3, max_semantic=3, max_hypotheses=4,
            max_docs=0, max_web=0, max_critic_rounds=1, max_seconds=60,
        ),
    )

    hypothesis = out["research"]["hypotheses"][0]
    assert hypothesis["id"] == "H1"
    assert hypothesis["status"] == "supported"
    assert hypothesis["supporting_evidence"] == ["sql_1"]
    assert hypothesis["tested_evidence"] == ["sql_1"]
    assert out["research"]["layers"]["live_discovery"]["status"] == "used"
    assert out["research"]["layers"]["profiling"]["status"] == "used"
    assert out["research"]["layers"]["semantic_verification"]["status"] == "used"


def test_phase_gate_requires_inspection_and_competing_hypotheses_before_sql():
    ctx = _ctx()
    ctx.advise = True
    ledger = research_agent.Ledger()
    ledger.discoveries.append({
        "ok": True,
        "relation_index": ["sales_monthly", "inventory_daily"],
        "relations": [],
    })
    budget = research_agent.ResearchBudget(
        max_rounds=10, max_sql=4, max_catalog=4, max_discovery=2,
        max_profile=4, max_semantic=4, max_hypotheses=4,
        max_docs=0, max_web=0, max_critic_rounds=2, max_seconds=60,
    )

    names = {
        tool["function"]["name"]
        for tool in research_agent._tools(
            False, False, budget=budget, ledger=ledger, ctx=ctx,
        )
    }
    assert "run_sql" not in names
    assert "finish_research" not in names

    ledger.counts["search_catalog"] = 1
    ledger.profiles.extend([
        {"ok": True, "table": "sales_monthly"},
        {"ok": True, "table": "inventory_daily"},
    ])
    ledger.semantic_checks.append({"checks": [{"status": "verified"}]})
    ledger.hypotheses = {
        "H1": {
            "id": "H1", "statement": "Scale supports option A", "status": "testing",
            "analytical_angle": "scale",
            "scope": {"tables": ["sales_monthly"]},
            "falsification_test": "Reject if scale is below peers.",
            "decision_effect": "Changes the leading option.",
            "tested_evidence": [],
        },
        "H2": {
            "id": "H2", "statement": "Capacity risk weakens option A", "status": "testing",
            "analytical_angle": "capacity risk",
            "scope": {"tables": ["inventory_daily"]},
            "falsification_test": "Reject if utilization is healthy.",
            "decision_effect": "Changes scale-up into a no-build option.",
            "tested_evidence": [],
        },
    }
    names = {
        tool["function"]["name"]
        for tool in research_agent._tools(
            False, False, budget=budget, ledger=ledger, ctx=ctx,
        )
    }
    assert "run_sql" in names
    assert "finish_research" not in names

    ledger.sql_results.append({"evidence_id": "sql_1"})
    for hypothesis in ledger.hypotheses.values():
        hypothesis["status"] = "inconclusive"
        hypothesis["tested_evidence"] = ["sql_1"]
    names = {
        tool["function"]["name"]
        for tool in research_agent._tools(
            False, False, budget=budget, ledger=ledger, ctx=ctx,
        )
    }
    assert "finish_research" in names


def test_hypothesis_requires_scope_and_falsification():
    ledger = research_agent.Ledger()
    try:
        research_agent._hypothesis_payload(ledger, {
            "statement": "Revenue decline comes from one region",
            "status": "testing",
        })
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("unscoped hypothesis should be rejected")


def test_near_duplicate_sql_shape_is_suppressed_but_new_grain_is_allowed():
    ledger = research_agent.Ledger()
    first = (
        "SELECT region, SUM(revenue) AS revenue "
        "FROM sales_monthly WHERE month_id >= 202601 GROUP BY region"
    )
    ledger.sql_shapes.append({
        "datasource": "ds_sales",
        "evidence_id": "sql_1",
        "shape": research_agent._normalized_sql_shape(first, "postgres"),
    })
    duplicate = (
        "SELECT region AS area, SUM(revenue) AS total_revenue "
        "FROM sales_monthly WHERE month_id >= 202601 GROUP BY region"
    )
    different_grain = (
        "SELECT month_id, SUM(revenue) AS revenue "
        "FROM sales_monthly WHERE month_id >= 202601 GROUP BY month_id"
    )
    assert research_agent._near_duplicate_sql(
        ledger, "ds_sales", duplicate, "postgres",
    ) == "sql_1"
    assert research_agent._near_duplicate_sql(
        ledger, "ds_sales", different_grain, "postgres",
    ) is None


def test_critic_state_keeps_late_evidence_ids_visible():
    ledger = research_agent.Ledger()
    for index in range(1, 19):
        ledger.sql_results.append({
            "ok": True,
            "evidence_id": f"sql_{index}",
            "hypothesis_id": "H1",
            "research_purpose": "bounded result",
            "datasource_id": "ds_sales",
            "columns": ["region", "revenue"],
            "rows": [[f"region-{row}", row] for row in range(60)],
            "safe_sql": "SELECT region, SUM(revenue) FROM sales_monthly GROUP BY region",
            "dialect": "postgres",
            "pii_columns": [],
        })
    state = research_agent._critic_state(_ctx(), ledger)
    assert '"evidence_id": "sql_1"' in state
    assert '"evidence_id": "sql_18"' in state


def test_reasoning_narration_streams_during_research(monkeypatch):
    """Per-round model prose + hypothesis transitions flow out as reasoning_delta."""
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result(
            [_call(
                "c1", "run_sql", datasource="ds_sales", purpose="ukur tren revenue",
                sql="SELECT month_id, SUM(revenue) AS revenue FROM sales_monthly GROUP BY month_id",
            )],
            text="Saya menguji tren revenue bulanan dulu, kontak person@example.com bila perlu.",
        ),
        _tool_result([_call(
            "c2", "update_hypothesis", statement="Revenue turun karena tren musiman.",
            status="inconclusive", analytical_angle="momentum",
            scope={"datasource": "ds_sales", "tables": ["sales_monthly"]},
            falsification_test="tren naik", decision_effect="prioritas berubah",
        )]),
        _tool_result([_call(
            "c3", "finish_research", reason="cukup", remaining_gaps=[],
        )]),
    ]
    monkeypatch.setattr(
        research_agent.llm, "complete_tools",
        lambda *_a, **_k: script.pop(0),
    )
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "ok", "gaps": [], "next_actions": []}',
        ),
    )
    monkeypatch.setattr(research_agent, "execute_governed", lambda *_a, **_k: {
        "ok": True, "columns": ["month_id", "revenue"],
        "rows": [[202601, 100], [202602, 80]],
        "safe_sql": "SELECT 1", "generated_sql": "SELECT 1", "engine": "llm:fake",
        "dialect": "postgres", "datasource_id": "ds_sales",
        "datasource_name": "Sales", "datasource_type": "postgres",
        "pii_columns": ["email"],
    })

    events = []
    out = research_agent.run_research(
        _ctx(), [_datasource()], emit=events.append,
        budget=research_agent.ResearchBudget(
            max_rounds=8, max_sql=4, max_catalog=4, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )
    assert out["research"]["completed"] is True
    narration = [e for e in events if e.get("type") == "reasoning_delta"]
    joined = " ".join(e["text"] for e in narration)
    assert "tren revenue bulanan" in joined                 # round prose narrated
    assert "Hipotesis H" in joined                          # hypothesis transition narrated
    assert "person@example.com" not in joined               # PII stays redacted mid-stream


def test_prose_only_model_stops_early_instead_of_burning_the_round_budget(monkeypatch):
    """A model that answers in prose never satisfies the phase gate, so run_sql is never
    offered to it. The loop must give up after a few idle rounds instead of spending the
    whole budget re-rejecting the same reply (live staging: 24 rounds, zero tool calls)."""
    states = []

    def fake_tools(_tenant, _purpose, messages, _tools, **_kw):
        states.append(messages[-1]["content"])
        return _tool_result([], text="Berdasarkan analisis, sebaiknya pilih gudang A.")

    monkeypatch.setattr(research_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": false, "reason": "belum ada bukti", "gaps": [], "next_actions": []}',
        ),
    )

    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=24, max_sql=8, max_catalog=6, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60, max_idle_rounds=3,
        ),
    )

    assert out["research"]["stop_reason"] == "no_tool_action"
    assert out["research"]["rounds"] == 3
    assert len(states) == 3
    assert not out["results"] and not out["queries"]
    # the rejection is now explicit and names the tools that ARE callable this round,
    # instead of silently dropping the reply
    assert "requested no tool" in states[-1]
    assert "search_catalog" in states[-1]


def test_idle_counter_resets_when_the_model_acts_again(monkeypatch):
    """Only CONSECUTIVE idle rounds abort — a model that stalls then acts keeps going."""
    script = [
        _tool_result([]),
        _tool_result([]),
        _tool_result([_call("c1", "search_catalog", query="revenue driver")]),
        _tool_result([]),
        _tool_result([]),
        _tool_result([]),
    ]

    def fake_tools(*_a, **_k):
        return script.pop(0)

    monkeypatch.setattr(research_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": false, "reason": "belum ada bukti", "gaps": [], "next_actions": []}',
        ),
    )

    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=24, max_sql=8, max_catalog=6, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60, max_idle_rounds=3,
        ),
    )

    assert out["research"]["rounds"] == 6
    assert out["research"]["stop_reason"] == "no_tool_action"
    assert script == []


def test_invented_tool_names_count_as_idle_rounds_too(monkeypatch):
    """A model alternating prose and made-up tool names must not reset the idle counter —
    the live regression burned 24 rounds exactly this way (nothing recognized, nothing
    gathered, no stall detected)."""
    replies = [
        _tool_result([], text="Saya sedang menganalisis opsi ekspansi."),
        _tool_result([_call("c1", "tool_name", query="x")]),          # protocol example, verbatim
        _tool_result([], text="Melanjutkan analisis kualitatif."),
        _tool_result([_call("c2", "sql_query", sql="SELECT 1")]),     # invented name
    ]

    def fake_tools(*_a, **_k):
        return replies.pop(0) if replies else _tool_result([], text="masih prosa")

    monkeypatch.setattr(research_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": false, "reason": "belum ada bukti", "gaps": [], "next_actions": []}',
        ),
    )

    out = research_agent.run_research(
        _ctx(), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=24, max_sql=8, max_catalog=6, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60, max_idle_rounds=3,
        ),
    )

    assert out["research"]["stop_reason"] == "no_tool_action"
    assert out["research"]["rounds"] == 3          # prose, fake, prose -> no reset in between
    assert not out["results"] and not out["queries"]


def test_system_prompt_carries_no_per_turn_narration_order(monkeypatch):
    """Regression guard: the 'EVERY turn, one short sentence' narration bullet made
    gpt-5.6-luna (emulated transport) reply with the sentence INSTEAD of tool_calls,
    killing SQL + web for the whole turn. Narration must come from the tool protocol's
    optional "note" field, never from a system-prompt order."""
    ctx = _ctx()
    text = research_agent._system_prompt(
        ctx, [_datasource()], research_agent.ResearchBudget(), False, False,
    )
    lowered = text.lower()
    assert "every turn" not in lowered
    assert "one short sentence" not in lowered


ROW_FILTERED = {"full_access": True, "row_filters": {"sales_monthly": "region = 'Jakarta'"}}


def _gate_ledger():
    """Everything the phase gate asks for except a profile."""
    ledger = research_agent.Ledger()
    ledger.discoveries.append({
        "ok": True, "relation_index": ["sales_monthly", "inventory_daily"], "relations": [],
    })
    ledger.counts["search_catalog"] = 1
    ledger.semantic_checks.append({"checks": [{"status": "verified"}]})
    ledger.hypotheses = {
        "H1": {
            "id": "H1", "statement": "Revenue fell in one region", "status": "testing",
            "analytical_angle": "segmentation", "scope": {"tables": ["sales_monthly"]},
            "falsification_test": "Reject if every region fell alike.",
            "decision_effect": "Targets the fix.", "tested_evidence": [],
        },
    }
    return ledger


def _tool_names(ctx, ledger):
    budget = research_agent.ResearchBudget(
        max_rounds=10, max_sql=4, max_catalog=4, max_discovery=2, max_profile=4,
        max_semantic=4, max_hypotheses=4, max_docs=0, max_web=0,
        max_critic_rounds=2, max_seconds=60,
    )
    return {
        tool["function"]["name"]
        for tool in research_agent._tools(False, False, budget=budget, ledger=ledger, ctx=ctx)
    }


def test_row_filtered_role_is_never_offered_profile_table_and_the_gate_still_opens():
    """profile_table samples outside govern(), so RLAC never applies to it. A role with
    row filters must not get it, and the gate must not wait for a profile it can never
    take (that stalled Deep Research into its fallback)."""
    ledger = _gate_ledger()
    assert "run_sql" not in _tool_names(_ctx(), ledger)       # a full role still profiles first
    assert "profile_table" in _tool_names(_ctx(), ledger)

    names = _tool_names(_ctx(ROW_FILTERED), ledger)
    assert "profile_table" not in names
    assert "run_sql" in names

    early = research_agent.Ledger()
    early.discoveries = list(ledger.discoveries)
    assert "profile_table" not in _tool_names(_ctx(ROW_FILTERED), early)
    assert "profile_table" not in {
        tool["function"]["name"] for tool in research_agent._tools(False, False, ctx=_ctx(ROW_FILTERED))
    }


def test_row_filtered_role_cannot_profile_even_when_the_model_calls_it(monkeypatch):
    _bypass_phase_gate(monkeypatch)
    script = [
        _tool_result([_call("c1", "profile_table", datasource="ds_sales", table="sales_monthly")]),
        _tool_result([_call("c2", "finish_research", reason="cukup", remaining_gaps=[])]),
    ]
    monkeypatch.setattr(research_agent.llm, "complete_tools", lambda *_a, **_k: script.pop(0))
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "ok", "gaps": [], "next_actions": []}'),
    )
    sampled = []
    monkeypatch.setattr(research_agent.discovery, "profile_relation",
                        lambda *a, **k: sampled.append(a) or {"ok": True})

    out = research_agent.run_research(
        _ctx(ROW_FILTERED), [_datasource()], budget=research_agent.ResearchBudget(
            max_rounds=4, max_sql=2, max_catalog=2, max_docs=0, max_web=0,
            max_critic_rounds=1, max_seconds=60,
        ),
    )

    assert sampled == []
    trace = [item for item in out["research"]["tool_trace"] if item["tool"] == "profile_table"]
    assert trace and trace[0]["status"] == "error"


def test_row_filtered_role_is_not_marked_down_for_a_missing_profile(monkeypatch):
    monkeypatch.setattr(
        research_agent.llm, "complete",
        lambda *_a, **_k: _completion(
            '{"complete": true, "reason": "ok", "gaps": [], "next_actions": []}'),
    )
    ledger = research_agent.Ledger()
    ledger.discoveries.append({"ok": True, "relations": [{"name": "sales_monthly"}]})
    ledger.sql_results.append({"evidence_id": "sql_1"})
    gap = "no live table profile was used to validate grain and data shape"

    assert gap in research_agent._critic(_ctx(), ledger)["gaps"]
    assert gap not in research_agent._critic(_ctx(ROW_FILTERED), ledger)["gaps"]

    prompt = research_agent._system_prompt(
        _ctx(ROW_FILTERED), [_datasource()], research_agent.ResearchBudget(), False, False)
    assert "bounded non-PII profiling" not in prompt
    assert "profile material tables" not in prompt

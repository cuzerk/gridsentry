"""
StormLines — LangGraph orchestrator.

Resolves a user's (bbox, start, end) pick into the ground-truth data
pipeline (transmission lines -> EagleI outage timeline -> wind/precip), with
an LLM-driven decision node handling the judgment calls a plain coverage
check can't: mixed historical/future ranges, oversized areas, sparse-data
warnings, and a human-readable explanation for the UI.

Deliberately NOT agentic: the outage-CSV filtering, infra fetch, and
wind/precip fetch stay plain deterministic functions (same modules as
Phase 1). The LLM sits only at the resolve_data_source decision boundary,
where reasoning about ambiguous inputs actually pays for itself.

If no Anthropic credentials are configured (or the call fails for any
reason), resolve_data_source_node falls back to a deterministic explanation
built from the same coverage facts — the graph's routing never depends on
the LLM call succeeding.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "analysis"))

import fetch_power_lines  # noqa: E402
import process_outages  # noqa: E402
import precip_agent  # noqa: E402
import wind_agent  # noqa: E402

RESOLVER_MODEL = "claude-opus-4-8"
LARGE_AREA_DEG2 = 100.0   # bbox area beyond which the Overpass infra fetch gets slow
LARGE_RANGE_DAYS = 60     # date range beyond which the outage timeline gets very large

# Wind/precip are fetched over a much larger area than the picked bbox (lines
# and outages stay scoped to exactly what the user drew) so an approaching
# storm is visible moving into frame instead of appearing to originate at
# the edge of the selection.
WEATHER_WIDTH_FACTOR = 3.0   # east-west span multiplier
WEATHER_HEIGHT_FACTOR = 5.0  # north-south span multiplier


def expand_bbox(bbox: tuple, width_factor: float, height_factor: float) -> tuple:
    """Expand a (west, south, east, north) bbox around its center so the
    result is width_factor times as wide and height_factor times as tall."""
    west, south, east, north = bbox
    cx, cy = (west + east) / 2, (south + north) / 2
    half_w = (east - west) / 2 * width_factor
    half_h = (north - south) / 2 * height_factor
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


class OrchestratorState(TypedDict):
    bbox: tuple
    start: datetime
    end: datetime
    out_dir: Path
    coverage: Optional[dict]
    strategy: Optional[str]  # "ground_truth" | "reject"
    explanation: Optional[str]
    warnings: list
    routes: Optional[list]
    levels: Optional[dict]
    weather_bbox: Optional[tuple]
    weather_errors: list
    meta: Optional[dict]


# ═══════════════════════════════════════════════════════════════════════════
# NODE 1 — resolve_data_source (the one genuinely agentic decision point)
# ═══════════════════════════════════════════════════════════════════════════

def _facts_for_resolver(bbox: tuple, start_dt: datetime, end_dt: datetime, coverage: dict) -> dict:
    west, south, east, north = bbox
    area_deg2 = (east - west) * (north - south)
    span_days = (end_dt - start_dt).days
    return {
        "bbox": list(bbox),
        "area_sq_degrees": round(area_deg2, 2),
        "start": start_dt.date().isoformat(),
        "end": end_dt.date().isoformat(),
        "span_days": span_days,
        "historical_coverage_years": "2014-2023",
        "years_requested": coverage["years_requested"],
        "years_covered": coverage["years_covered"],
        "has_data": coverage["has_data"],
        "county_count": coverage["county_count"],
        "event_count": coverage["event_count"],
        "is_large_area": area_deg2 > LARGE_AREA_DEG2,
        "is_large_range": span_days > LARGE_RANGE_DAYS,
    }


def _deterministic_explanation(facts: dict) -> tuple:
    """Fallback used when the LLM call is unavailable or fails. Covers the
    same decision space with plain conditionals — a deterministic switch
    handles the common case fine; only the edge-case *phrasing* is worse
    without the LLM, not the routing correctness."""
    warnings = []
    if not facts["years_covered"]:
        explanation = (
            f"No historical EagleI outage coverage for {facts['start']} to {facts['end']}. "
            "Ground-truth data spans 2014-01-01 through 2023-12-31."
        )
        return "reject", explanation, warnings

    if set(facts["years_requested"]) - set(facts["years_covered"]):
        warnings.append("partial_historical_coverage")
    if facts["is_large_area"]:
        warnings.append("large_area_may_be_slow")
    if facts["is_large_range"]:
        warnings.append("large_date_range_may_be_slow")

    if not facts["has_data"]:
        warnings.append("sparse_or_empty_data")
        explanation = (
            f"This area/date range is within EagleI's historical coverage but has no "
            f"recorded outage events ({facts['county_count']} counties, 0 events) — "
            "an empty timeline here is accurate, not a bug."
        )
    else:
        explanation = (
            f"Showing real EagleI outage data for {facts['start']} to {facts['end']} "
            f"({facts['county_count']} counties, {facts['event_count']} outage events)."
        )
    return "ground_truth", explanation, warnings


def _llm_resolve(facts: dict) -> Optional[dict]:
    """Ask Claude to judge edge cases and write the UI-facing explanation.
    Returns None (triggering the deterministic fallback) if credentials
    aren't configured or the call fails for any reason."""
    try:
        import anthropic
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=RESOLVER_MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "strategy": {"type": "string", "enum": ["ground_truth", "reject"]},
                            "explanation": {"type": "string"},
                            "warnings": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["strategy", "explanation", "warnings"],
                        "additionalProperties": False,
                    },
                },
            },
            system=(
                "You resolve a user's picked US map area + date range into a data-source "
                "decision for a storm-outage visualization. The only real data source "
                "available is the EagleI historical outage dataset, covering 2014-01-01 "
                "through 2023-12-31 across the entire US. There is no live prediction "
                "capability yet — if the request falls even partially outside that window, "
                "the correct strategy is \"reject\", and the explanation should tell the "
                "user what portion (if any) of their range IS covered so they can narrow "
                "it, rather than just saying no. If the range is fully covered, strategy "
                "is \"ground_truth\" even when event_count is 0 (sparse real data is "
                "honest, not a failure). Note risk factors as short warning codes: "
                "\"sparse_or_empty_data\" (has_data is false), \"large_area_may_be_slow\" "
                "(is_large_area), \"large_date_range_may_be_slow\" (is_large_range), "
                "\"partial_historical_coverage\" (years_requested has years outside "
                "years_covered). Write the explanation as one or two plain sentences for "
                "an end user, not a developer."
            ),
            messages=[{"role": "user", "content": json.dumps(facts)}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        decision = json.loads(text)
        if decision.get("strategy") not in ("ground_truth", "reject"):
            return None
        return decision
    except Exception as exc:
        print(f"[orchestrator] LLM resolver unavailable, using deterministic fallback: {exc}")
        return None


def resolve_data_source_node(state: OrchestratorState) -> dict:
    coverage = process_outages.estimate_coverage(state["bbox"], state["start"], state["end"])
    facts = _facts_for_resolver(state["bbox"], state["start"], state["end"], coverage)

    decision = _llm_resolve(facts)
    if decision is None:
        strategy, explanation, warnings = _deterministic_explanation(facts)
    else:
        strategy = decision["strategy"]
        explanation = decision["explanation"]
        warnings = decision["warnings"]
        # Safety net: never let the LLM route to ground_truth when there is
        # literally no historical coverage for any requested year — the
        # coverage check is a fact, not a judgment call.
        if not facts["years_covered"]:
            strategy = "reject"

    return {"coverage": coverage, "strategy": strategy, "explanation": explanation, "warnings": warnings}


def _route_after_resolve(state: OrchestratorState) -> str:
    return "continue" if state["strategy"] == "ground_truth" else "stop"


# ═══════════════════════════════════════════════════════════════════════════
# NODES 2-4 — deterministic ground-truth pipeline (Phase 1 modules, unchanged)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_infra_node(state: OrchestratorState) -> dict:
    routes, levels = fetch_power_lines.fetch_lines_for_bbox(state["bbox"])
    return {"routes": routes, "levels": levels}


def fetch_ground_truth_node(state: OrchestratorState) -> dict:
    process_outages.build_outage_timeline(
        state["bbox"], state["start"], state["end"], state["routes"], state["out_dir"],
        storm_label=f"{state['start'].date()} to {state['end'].date()}",
    )
    return {}


def fetch_weather_node(state: OrchestratorState) -> dict:
    weather_bbox = expand_bbox(state["bbox"], WEATHER_WIDTH_FACTOR, WEATHER_HEIGHT_FACTOR)
    start_s = state["start"].strftime("%Y-%m-%d")
    end_s = state["end"].strftime("%Y-%m-%d")
    out_dir = state["out_dir"]
    errors = []
    try:
        wind_agent.run(weather_bbox, start_s, end_s, str(out_dir / "wind"))
    except Exception as exc:
        errors.append(f"wind: {exc}")
    try:
        precip_agent.run(weather_bbox, start_s, end_s, str(out_dir / "precip"))
    except Exception as exc:
        errors.append(f"precip: {exc}")
    return {"weather_bbox": weather_bbox, "weather_errors": errors}


def finalize_node(state: OrchestratorState) -> dict:
    meta = {
        "bbox": list(state["bbox"]),
        "start": state["start"].isoformat(),
        "end": state["end"].isoformat(),
        "source": state["strategy"],
        "explanation": state["explanation"],
        "warnings": state["warnings"],
        "coverage": state["coverage"],
        "area_hash": fetch_power_lines.area_hash(state["bbox"]),
        "weather_bbox": list(state["weather_bbox"]) if state.get("weather_bbox") else None,
        "weather_errors": state.get("weather_errors", []),
    }
    out_dir = state["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    return {"meta": meta}


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════

def build_orchestrator():
    g = StateGraph(OrchestratorState)

    g.add_node("resolve_data_source", resolve_data_source_node)
    g.add_node("fetch_infra", fetch_infra_node)
    g.add_node("fetch_ground_truth", fetch_ground_truth_node)
    g.add_node("fetch_weather", fetch_weather_node)
    g.add_node("finalize", finalize_node)

    g.set_entry_point("resolve_data_source")
    g.add_conditional_edges(
        "resolve_data_source", _route_after_resolve,
        {"continue": "fetch_infra", "stop": END},
    )
    g.add_edge("fetch_infra", "fetch_ground_truth")
    g.add_edge("fetch_ground_truth", "fetch_weather")
    g.add_edge("fetch_weather", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


def run_orchestrator(bbox: tuple, start_dt: datetime, end_dt: datetime, out_dir: Path) -> dict:
    """Entry point for backend/server.py. Returns {"status": "ready", "meta": {...}}
    or {"status": "failed", "error": ..., "coverage": ...}."""
    graph = build_orchestrator()
    result = graph.invoke({
        "bbox": bbox, "start": start_dt, "end": end_dt, "out_dir": out_dir,
        "coverage": None, "strategy": None, "explanation": None, "warnings": [],
        "routes": None, "levels": None, "weather_bbox": None, "weather_errors": [], "meta": None,
    })

    if result.get("strategy") != "ground_truth":
        return {
            "status": "failed",
            "error": result.get("explanation") or "No historical data available for this request.",
            "coverage": result.get("coverage"),
        }
    return {"status": "ready", "meta": result["meta"]}

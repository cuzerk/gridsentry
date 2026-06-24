"""
GridSentry — pipeline entry point.

Usage:
    cd backend
    python pipeline.py

Runs the three-agent LangGraph pipeline and writes
../frontend/data/prediction_timeline.json.
"""

from __future__ import annotations

import os
import sys
import time

# Resolve backend package from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import GridSentryState, LOCATIONS, build_pipeline

_HERE        = os.path.dirname(os.path.abspath(__file__))
_OUTPUT_PATH = os.path.join(_HERE, "..", "frontend", "data", "prediction_timeline.json")


def main() -> None:
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║          GridSentry — Predictive Maintenance         ║")
    print("║        Feb 22-24, 2026  |  New England Nor'easter    ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    pipeline = build_pipeline()

    initial_state: GridSentryState = {
        "weather_data": {},
        "outage_data":  {},
        "predictions":  [],
        "output_path":  os.path.abspath(_OUTPUT_PATH),
        "log":          [],
    }

    t0 = time.time()
    final_state = pipeline.invoke(initial_state)
    elapsed = time.time() - t0

    print("\n── Pipeline Log ─────────────────────────────────────────")
    for entry in final_state.get("log", []):
        print(f"  {entry}")

    print(f"\n── Done in {elapsed:.1f}s ───────────────────────────────")
    print(f"  Output → {os.path.abspath(_OUTPUT_PATH)}\n")
    print("  Next step:  cd ../frontend && streamlit run app.py\n")


if __name__ == "__main__":
    main()

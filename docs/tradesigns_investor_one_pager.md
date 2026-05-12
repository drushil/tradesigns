# TradeSigns — One-Page Business Summary

## What it is
TradeSigns is an AI-assisted paper trading platform that automates signal generation, risk-gated decisioning, execution simulation, and learning-driven optimization for US equities/ETFs. It combines deterministic risk controls with adaptive signal weighting and a live operations dashboard.

## Problem
Retail and small-team systematic traders struggle to operationalize strategy ideas into a repeatable, monitored, and explainable workflow. Most setups are fragmented across scripts, broker UIs, and spreadsheets.

## Solution
TradeSigns provides a full loop:
1. Compute and normalize intraday/macro signals per ticker.
2. Apply strict risk + expected value gates before execution.
3. Execute in Alpaca paper accounts with position/exit tracking.
4. Persist all telemetry in Supabase for analytics, auditability, and learning.
5. Surface decisions and outcomes in a multi-page Streamlit dashboard.

## Product capabilities
- Multi-page operator dashboard (signals, trades, performance, learning, logs).
- Risk profile engine with configurable aggressiveness and instrument constraints.
- Regime-aware learning engine using attribution + exponential weighting updates.
- Trade grading, replay diagnostics, and portfolio review workflows.
- Low-ops deployment model suitable for rapid experimentation.

## Business value
- **Speed:** Faster strategy iteration cycle from idea → test → evidence.
- **Discipline:** Hard risk and EV controls reduce impulsive/manual override behavior.
- **Transparency:** Every decision and outcome is persisted and reviewable.
- **Scalability path:** Architecture can evolve into subscription analytics, advisor tooling, and API signal products.

## Go-forward priorities
- Harden security/auth and secret management.
- Improve reliability with queue-based execution and richer observability.
- Expand KPI reporting for investor-grade performance narratives.
- Prepare for staged move from paper to controlled live deployment.


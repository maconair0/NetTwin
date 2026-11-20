# Agentic Digital Twin Framework on ns-3: End-to-End Build Plan

## 0. Summary

**Goal.** An LLM agent that, for a given network domain (IP, optical, wireless, NTN), reads the real network state from a database, builds a validated simulation scenario, runs it in an isolated ns-3 sandbox, and receives a **probabilistic** prediction that a deterministic policy layer turns into an action class (reject, gather more data, propose for approval, eligible for auto-apply).

**Design stance.**
- One independent twin per domain. No unified physics across domains.
- Cross-domain reasoning happens only at the agent level, and reports say explicitly that cross-domain effects are not modeled.
- The LLM never writes simulator code. It emits and reads JSON.
- The trust decision lives in code, not in the LLM.
- Start with one domain and sandbox mode. Expand only after measured fidelity is acceptable.

**Realistic outcome.** A scoped version (one or two domains, sandbox, calibrated, with a promotion gate) is achievable in months. Four domains with live mode is a multi-year effort. Frame the contribution as the uncertainty-aware agentic decision layer over per-domain twins.

---

## 1. Architecture

```
                         ┌───────────────────────────┐
                         │  Agent (LangGraph)        │
                         │  local LLM via vLLM/Ollama│
                         └─────────────┬─────────────┘
                                       │ MCP
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
┌───────▼────────┐           ┌─────────▼─────────┐          ┌─────────▼────────┐
│ State MCP      │           │ Skill MCP         │          │ Twin MCP         │
│ topology,      │           │ playbooks +       │          │ run_scenario()   │
│ telemetry,     │           │ Pydantic schemas  │          │ get_calibration()│
│ configs (RO)   │           │ per domain        │          │                  │
└───────┬────────┘           └───────────────────┘          └─────────┬────────┘
        │                                                             │
┌───────▼────────┐                                         ┌──────────▼─────────┐
│ DB: graph +    │                                         │ Scenario runner    │
│ time series    │                                         │ (Python orchestr.) │
└────────────────┘                                         └──────────┬─────────┘
                                                                      │ N containers
                                                           ┌──────────▼─────────┐
                                                           │ ns-3 binary        │
                                                           │ core + plugins:    │
                                                           │ ip | wireless |    │
                                                           │ ntn | optical      │
                                                           └──────────┬─────────┘
                                                                      │
                                              ┌───────────────────────▼──────────┐
                                              │ Aggregate → Calibrate → Envelope │
                                              └───────────────────────┬──────────┘
                                                                      │
                                              ┌───────────────────────▼──────────┐
                                              │ Policy gate → Human approval →   │
                                              │ Apply → Verify → Rollback        │
                                              └──────────────────────────────────┘
```

| Layer | Component | Notes |
|---|---|---|
| Reasoning | Local LLM (vLLM or Ollama) | General tool-use model, schema-constrained decoding |
| Orchestration | LangGraph | Fixed node order, bounded retries |
| State | State MCP server | Read-only credentials. Graph or relational store for topology, time-series store for telemetry |
| Knowledge | Skill MCP server | Versioned markdown playbooks and Pydantic scenario schemas per domain |
| Execution | Twin MCP + runner | Containerized ns-3 runs, no network, CPU/memory/time limits |
| Governance | Policy gate | Deterministic thresholds, approval, rollback |

---

## 2. Canonical network model

Define one vendor-neutral model first. Every adapter converts to and from it.

- **Entities:** node, interface, link, path/lightpath, flow/demand, cell/beam, constraint.
- **Naming:** follow OpenConfig/YANG-style leaves where possible, so the DB, the scenario schemas, and any edge-LM mappings share one vocabulary.
- **Metadata on every object:** `snapshot_id`, `timestamp`, `source` (controller or device), `domain`.
- **Storage:** graph (Neo4j) or relational (PostgreSQL) for topology, TimescaleDB for telemetry.

**Exit criterion:** the model can represent a real snapshot of your testbed for at least one domain with no information loss that matters for simulation.

---

## 3. Scenario schema

A single JSON scenario with a common core and a per-domain block.

```json
{
  "schema_version": "1.0",
  "domain": "ip",
  "snapshot_id": "2026-11-19T10:00:00Z",
  "question": "p_delivery_under_link_failure",
  "topology": { "nodes": [], "links": [] },
  "traffic": { "demands": [] },
  "events": [ { "t": 30.0, "type": "link_down", "target": "L3" } ],
  "duration_s": 120,
  "kpis": ["throughput", "delay", "loss"],
  "requirements": [ { "kpi": "loss", "op": "<", "value": 0.01 } ],
  "uncertain_params": [
    { "path": "links.L1.delay_ms", "dist": "normal", "mean": 5, "std": 0.5 }
  ],
  "seed_policy": { "runs": 30, "base_seed": 1 },
  "domain_block": {}
}
```

Rules:
- Pydantic models are the single source of truth. Export JSON Schema from them for constrained decoding.
- Two validation stages: **schema** validation, then **semantic** validation (referenced nodes exist, values in physical range, event times inside the duration, scenario size under a cap).
- Version the schema. Reject scenarios with an unknown version.

---

## 4. Components to build

### 4.1 State MCP server (read-only)
Tools:
- `list_domains()`
- `inspect_domain(domain, scope)`: nodes, links, constraints, snapshot timestamp
- `get_subnetwork(domain, anchor, hops)`: extract a small relevant slice
- `get_telemetry(entity, kpi, window)`
- `get_snapshot(snapshot_id)`

Requirements: read-only DB credentials, every response carries `snapshot_id` and age, and large results are summarized.

### 4.2 Skill MCP server
Per domain:
- A markdown playbook: what questions the twin can answer, how to pick parameters, common pitfalls, and what the twin cannot model.
- The scenario schema.
- Few-shot examples of valid scenarios.
- The declared fidelity tier and limits.

Tools: `list_skills()`, `load_skill(domain)`, `get_schema(domain)`.

### 4.3 Twin MCP server and scenario runner
Tools: `validate_scenario(scenario)`, `run_scenario(scenario)`, `get_calibration(domain, kpi)`, `get_run(run_id)`.

Runner steps:
1. Validate (schema, then semantic).
2. Expand into an ensemble: seeds × samples of the uncertain parameters.
3. Launch N containerized ns-3 runs in parallel.
4. Collect per-run KPI files (Parquet or JSON).
5. Aggregate, apply the calibration model, and produce the envelope.

### 4.4 ns-3 core binary
- Reads the JSON scenario and builds nodes, links, and devices from the topology.
- Event injector using `Simulator::Schedule` for failures, config changes, load changes, and mobility.
- KPI collection through FlowMonitor and trace sinks.
- Per-run seed via `RngSeedManager::SetRun`.
- One binary, with domain plugins selected by `domain`.

### 4.5 Domain plugins

| Domain | ns-3 basis | What to build | Limits to declare |
|---|---|---|---|
| IP | internet stack, routing, FlowMonitor | Topology import, routing and failure scenarios, traffic matrices | Models protocols and traffic, not real router software. Containerlab/FRR is an option if exact behavior matters |
| Optical | No usable native module | GNPy pre-processor computes GSNR per lightpath, mapped to BER and effective rate. ns-3 models lightpaths as point-to-point channels with those parameters, plus restoration events | Static QoT only. No amplifier transients |
| Wireless | 5G-LENA (NR), wifi, lte | Gridded gNB/UE scenarios, scheduler and numerology parameters | Research-grade, scale-limited |
| NTN | satellite module and NTN extensions | Orbit or delay profiles, beam handover, link budget | Least mature. Check maintenance and version compatibility first |

Each plugin declares: the **questions it can answer**, its **fidelity tier**, and its **valid parameter ranges**. The agent and the validator both use this.

### 4.5.1 Optical pre-processing detail
GNPy runs before ns-3. The uncertain inputs (fiber loss, amplifier noise figure, connector loss, launch power tolerance) are sampled in this step, so each ensemble member gets its own lightpath parameters. ns-3 then supplies the network-level behavior: rerouting, load, and delay on those lightpaths.

### 4.6 Calibration store
- Table of `(domain, kpi, scenario_features, predicted_dist, measured_value, timestamp)`.
- From it derive the **empirical error distribution** per KPI and the **interval coverage** (how often the real value falls in the stated p5-p95).
- Compute a **trust tier** per domain and KPI from coverage, sample count, and recency.

### 4.7 Output envelope

```json
{
  "domain": "optical",
  "question": "gsnr_after_reroute",
  "prediction": { "mean": 18.2, "p5": 17.1, "p50": 18.3, "p95": 19.0, "unit": "dB" },
  "p_meets_requirement": 0.93,
  "uncertainty_sources": ["seed", "input", "model_error"],
  "fidelity_tier": "static-QoT",
  "calibration": { "n": 42, "coverage_p5_p95": 0.88, "historical_error_p95": 0.8, "last_validated": "..." },
  "trust_tier": "B",
  "snapshot_age_s": 42,
  "engine_versions": { "ns3": "...", "plugin": "...", "gnpy": "...", "schema": "1.0" },
  "run_id": "..."
}
```

### 4.6 / 4.8 Uncertainty model
Combine three sources:
1. **Stochastic:** variation across seeds.
2. **Input:** sampling of the declared uncertain parameters.
3. **Model error:** the empirical predicted-vs-measured distribution, added on top. This is the most honest term.

`p_meets_requirement` is computed from the combined distribution. It is only meaningful if it is calibrated, so measure reliability (reliability diagrams, interval coverage) and report it with every result.

### 4.9 Policy gate (deterministic code)
Example policy table (thresholds are yours to configure):

| Condition | Action class |
|---|---|
| `p ≥ 0.99`, trust tier A, snapshot fresh, change allow-listed, step within bounds | Eligible for auto-apply |
| `0.90 ≤ p < 0.99`, trust tier A/B | Propose, human approval required |
| `p < 0.90`, or stale snapshot, or tier C/uncalibrated, or out of validated range | Reject or gather more data |
| Cross-domain change | Cap at the weakest twin's tier, require approval |

Also: allow-list of changeable parameters, bounded step sizes, rate limiting, canary or staged rollout, post-change verification, automatic rollback, and an audit log.

### 4.10 Agent (LangGraph)
Fixed node order:
1. **Classify** the request: domain and question type.
2. **Inspect:** `inspect_domain`, then `get_subnetwork` for the relevant slice.
3. **Load skill:** playbook and schema.
4. **Generate scenario** under schema-constrained decoding.
5. **Validate** (schema, then semantic).
6. **Run** `run_scenario`.
7. **Verify results:** check the KPIs against sanity bounds and against a baseline (no-change) run.
8. **Retry** on failure with the error fed back, capped (for example 3), then escalate.
9. **Decide:** pass the envelope to the policy gate.
10. **Report:** snapshot, scenario, engine versions, fidelity tier, trust tier, and the statement of what is not modeled.

The LLM chooses the twin and question, compares alternative scenarios, explains results, and decides the next run. It does not decide whether a probability is good enough.

---

## 5. Execution modes

- **Sandbox mode (build first):** pure simulation in virtual time, isolated, reproducible.
- **Live-twin mode (build last):** needs ns-3's real-time scheduler, which means it cannot run in fast virtual time, and has its own scale limits. Treat it as a separate mode and never mix it with sandbox runs.
- Live-twin first runs in **shadow mode** (predict, do not act) and is compared with reality continuously.

---

## 6. Infrastructure

- **Containers:** one ns-3 run per container, no network, CPU and memory limits, wall-clock timeout, read-only root filesystem, scenario mounted read-only, results written to a single output volume.
- **Parallelism:** one ns-3 run is single-threaded, so scale across containers. Use a seed budget per query (20-50 runs) to bound latency. Add a job queue (for example Celery or a simple worker pool) if you run several queries at once.
- **Versioning and reproducibility:** pin ns-3, each extension module, GNPy, the schema, and the plugin versions. Store them with every result so any run can be reproduced from `run_id`.
- **Caching:** hash `(scenario, versions)` and reuse results for identical requests.
- **Observability:** log every tool call, scenario, run time, and result. This log is also your future training data.

---

## 7. Model strategy

- **Start with a general tool-use model plus the harness.** Schema-constrained decoding, Pydantic validation, semantic validators, and the retry loop fix most format and tool-call errors.
- **Choose by measurement.** Run 2-3 current open-weight candidates on your eval set and compare tool-call accuracy and scenario validity, rather than assuming a particular older model.
- **Keep knowledge outside the weights** (playbooks and the DB) so updates need no retraining.
- **Fine-tune only on evidence:** if the logs show systematic failures (wrong terminology, poor tool selection, weak scenario generation), use your own successful runs as training data.
- **Edge LMs** (small models beside devices) are the exception and are more likely to need fine-tuning or distillation because of hardware limits.

---

## 8. Evaluation

Build the eval set early and measure everything against it.

1. **Agent eval set (30-50 requests per domain):** realistic requests with expected tool sequences and expected scenario fields. Metrics: schema-valid rate, semantic-valid rate, first-try success, retries needed, and end-to-end success.
2. **Twin fidelity eval:** predicted vs. measured on your testbed. Metrics: error distribution per KPI, p5-p95 coverage, reliability diagram, calibration error.
3. **Decision eval:** run the policy on historical cases and compare decisions made with calibrated probabilities against decisions made with point estimates. This is your paper's key experiment.
4. **Failure injection:** feed the agent out-of-range requests, stale snapshots, malformed topologies, and a deliberately miscalibrated twin, and confirm that the policy downgrades or rejects.
5. **Regression:** rerun the eval set whenever the model, schema, plugin, or ns-3 version changes.

---

## 9. Phased roadmap

| Phase | Work | Exit criterion |
|---|---|---|
| 0. Foundations | Canonical model, scenario schema (IP), runner skeleton, one dummy plugin, eval set drafted | Round trip JSON → ns-3 → envelope works |
| 1. IP twin, sandbox | State, Skill, and Twin MCP servers, IP plugin, ensemble, aggregator, containers | Eval set passes with no manual fixes |
| 2. Fidelity | Compare to real measurements on the IP testbed (ONOS-managed routers), build the calibration store | p5-p95 coverage near nominal, error model stored |
| 3. Agent and policy | LangGraph loop, policy gate, approval flow, rollback | Full propose → simulate → decide → approve → apply → verify cycle works on the testbed |
| 4. Optical twin | GNPy pre-processor plus ns-3 abstraction plugin, optical schema and skill | Predicted GSNR/BER vs. Nokia testbed measurements within thresholds |
| 5. Wireless twin | 5G-LENA plugin on a small, controlled scenario | Validated on a controlled scenario |
| 6. NTN twin | NTN plugin, after checking toolchain maintenance | Validated against link-budget references |
| 7. Live-twin (optional) | Telemetry ingestion, state calibration, staleness handling, shadow mode | Shadow predictions track reality within thresholds |

**Ordering note.** IP goes first because it is the cheapest way to prove the runner, the ensemble, and the calibration pipeline. If optical results matter more for your paper, swap phases 1-2 with phase 4, since the optical plugin only adds a pre-processor and a new schema.

**Reuse test.** At phase 4, the optical twin should need only a new plugin, schema, and skill. If you have to change the graph or the MCP servers, the abstraction is wrong and should be fixed before adding more domains.

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Twin is plausible but wrong | Fidelity validation, continuous drift monitoring, automatic trust-tier downgrade |
| Probabilities are overconfident | Reliability measurement, model-error term added to the uncertainty, conservative thresholds |
| Scope creep across four domains | Phase gates, one domain at a time |
| Immature NTN and wireless tooling | Defer, check maintenance status and version compatibility first |
| ns-3 extension version conflicts | Pin versions, build reproducible images, test upgrades against the regression eval |
| LLM scenario errors | Schema constraints, semantic validators, retry cap, eval set |
| Agent anchors on fluent explanations | Gate actions on numbers in the policy layer, not on narrative |
| Out-of-distribution queries | Record validated ranges per plugin, widen uncertainty or refuse outside them |
| Stale snapshots | Snapshot age in the policy |
| Unsafe promotion | Allow-lists, bounded steps, human approval, canary, automatic rollback |
| Latency from large ensembles | Subnetwork extraction, seed budget, caching, parallel containers |

---

## 11. Paper framing

**Contribution.** An agentic layer that consumes uncertainty-quantified predictions from independent per-domain digital twins and applies a calibrated, policy-governed trust decision.

**Claims to support with experiments.**
1. The standardized envelope lets one agent graph and one set of servers drive multiple domain twins (reuse test).
2. Calibrated probabilities lead to better decisions than point estimates (decision eval).
3. Schema-constrained generation with a validated harness gives reliable scenario generation with a general model (agent eval).
4. Per-domain fidelity is measured and reported with its limits (fidelity eval).

**Avoid claiming** a universal digital twin, unified cross-domain physics, or fully autonomous promotion to a live network.

---

## 12. First 4 weeks

| Week | Tasks |
|---|---|
| 1 | Canonical model, IP scenario schema in Pydantic, DB schema, ns-3 build in a container, dummy plugin round trip |
| 2 | IP plugin (topology import, traffic, failure events, FlowMonitor KPIs), runner with ensemble and aggregator |
| 3 | State, Skill, and Twin MCP servers, LangGraph loop, first 30 eval requests |
| 4 | Envelope and policy gate, testbed measurement collection for fidelity, first coverage numbers |

After week 4 you will know whether the abstraction and the calibration pipeline hold, which is the main feasibility question.

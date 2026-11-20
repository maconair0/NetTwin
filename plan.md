# netwins: agentic digital twins on ns-3

Version 2 (19 November 2026). Version 1 is kept as [docs/plan-v1.md](docs/plan-v1.md); what changed and why is in section 2.

## 1. Summary

**Goal.** An agent that, for one network domain at a time (IP, optical, wireless, NTN):
1. reads the network's real state;
2. builds a validated "what if" scenario;
3. runs it as an ensemble of isolated ns-3 simulations;
4. gets back a **calibrated, probabilistic** prediction;
5. passes it to a **deterministic policy** that decides what may happen next: reject, gather more data, propose for approval, or allow automatic apply.

**Stance** (unchanged from v1):
- **Per-domain twins.** There's one independent twin per domain, with no unified physics across domains. Cross-domain reasoning happens only in the agent, and every report says that cross-domain effects are not modelled.
- **JSON, not code.** The LLM writes and reads JSON. It never writes simulator code.
- **The trust decision is code.** The LLM explains and chooses what to ask; the policy decides what is good enough.
- **One domain at a time.** Start with one domain in sandbox mode, and add the next only after its fidelity has been measured.

**The contribution:** an uncertainty-aware, policy-governed decision layer over per-domain twins, with the uncertainty measured, not assumed.

## 2. What changed from v1, and why

These lessons come from building and testing CANAH, the network agent harness, on three emulated domains.

| v1 | v2 | Why |
|---|---|---|
| A new LangGraph agent with a fixed node order | **CANAH is the agent** (LangChain Deep Agents). The twin is a CANAH *provider*: tools, a skill and an `AGENTS.md` | CANAH already has sign-in, approvals, an audit log, grounding checks, budgets, a sandbox, an HTTP service and a test-bed runner. A second agent framework would duplicate all of it |
| The agent calls validate, run, aggregate and the policy in order | **One tool, `twin_predict(scenario)`**, runs the whole pipeline in code: validate → ensemble → aggregate → calibrate → envelope → policy | In CANAH's tests the local model skipped or reordered steps. The model now does what it's good at (turning a question into a scenario and explaining the result), and the pipeline can't be done out of order |
| Prediction of the changed network alone | **Paired baseline**: every what-if also runs the unchanged network on the **same seeds and the same sampled inputs** (common random numbers) | The question is almost always "what does this change do". Paired runs measure the *effect* with far less noise than two independent ensembles |
| A fixed 20–50 runs | **Adaptive ensemble**: batches of 10 until the probability of meeting the requirement is clearly above or below the policy's thresholds (Wilson interval), up to a cap | Most questions are decided early; seed budgets go where they matter |
| An "empirical error distribution" | **Conformal calibration** on predicted-vs-measured residuals per domain and KPI, which gives intervals with a finite-sample coverage guarantee. The residuals also enter `p_meets_requirement` | A coverage guarantee is a stronger, simpler claim than an ad hoc error model, and it is easy to evaluate |
| Fidelity only from the testbed | First a **synthetic ground truth** (ns-3 with hidden perturbations, always labelled `synthetic`), then the testbed | The whole calibration pipeline can be built and tested before measurements exist; the label keeps it from being mistaken for fidelity |
| A State MCP, a Skill MCP and a Twin MCP | One **twin provider** (state, skill and twin tools) in-process with CANAH; an MCP wrapper later if another client needs it | Fewer moving parts; MCP adds nothing while CANAH is the only client |

## 3. Architecture

```
operator ──► CANAH (agent, approvals, audit, checks, web page)
               │ twin provider
               ├─ twin_inspect / twin_subnetwork      state: canonical snapshots (read-only)
               ├─ twin_schema / twin_validate         the domain's schema and semantic rules
               └─ twin_predict(scenario)              the whole pipeline, in code:
                     validate → expand (seeds × sampled inputs, paired baseline)
                     → N containers: ns-3 engine, no network, CPU/memory/time limits
                     → aggregate → calibrate (conformal) → envelope → policy → action class
```

| Part | Where |
|---|---|
| canonical model and scenario schema (Pydantic) | `netwins/schema.py` |
| state: snapshots, subnetwork extraction | `netwins/state.py` |
| ensemble runner, engines (Docker, local, fake) | `netwins/runner.py` |
| aggregation, paired deltas, adaptive stopping | `netwins/aggregate.py` |
| calibration store, conformal intervals, trust tier | `netwins/calibration.py` |
| envelope, policy gate | `netwins/envelope.py`, `netwins/policy.py` |
| the `canah.twin/1` contract, in-process and over HTTP; skills | `netwins/contract.py`, `netwins/service.py`, `skills/` |
| ns-3 engine | `engine/` (Docker image `netwins-ns3:3.43`) |
| live mirror, write translation, gate | `netwins/source.py`, `mirror.py`, `changes.py`, `gate.py` |

### 3.1 The live loop: twin first, then the network (added 2026-11-19)

CANAH loads the agentic MCP. The twin mirrors each packet controller's network through the same service,
and every write is tried in the twin before it is sent:

```
operator: "create a 60 Mbit/s service from pe-a to pe-b"
  CANAH ─ twin_mirror(lab) ─────────► agentic MCP: read_devices, read_links, read_services, read_statistics ×2
        │                              (the controller's own tools, from its conformance mapping)
        │                            → snapshot: topology, capacities, services as demands, counter traffic as
        │                              background; every value measured or assumed; trust cap from coverage
        ├ twin_check_write(lab, add_path, {...})
        │    translate by contract verb → scenario change (add / remove demand, set rate, link down)
        │    → twin_predict (paired, calibrated, capped) → policy decision → check id
        ├ propose            → lab__add_path → CANAH approval (operator) → CANAH's twin gate: check found → sent
        ├ reject             → not sent; the figures go to the operator
        ├ ask_operator       → the agent asks (a rate, an end); checks again with `answers`
        └ not_modelled /     → the operator decides; twin_override (approved) → CANAH's gate lets that change through
          gather_more_data
The gate (CANAH's, on every agent; any twin that implements check_write): a write with no fresh check is checked at the gate and held unless
the twin supports it. A check is matched by the change it makes in the twin, not by its argument text.
```

What makes it hold up:
- **Code, not the model, enforces the order.** The gate stands between the approval and the
  controller, so a model that skips the check still cannot send an unsupported change.
- **An operator's word is an approved write** (`twin_override`) with their reason, and it is recorded
  on the check.
- **The twin is honest about its inputs.** Assumed capacities cap the trust tier: under 50% measured
  gives tier C, and tier C never lets a change go ahead.
- **Missing information becomes a question for the operator,** never a guess.
- **Stale state is refreshed:** the twin re-mirrors before a check when its snapshot is older than
  `NETWINS_MIRROR_MAX_AGE_S`.

## 4. Canonical model and scenario

**Canonical model.**
- **Entities:** node, link, demand, event and constraint; later path/lightpath and cell/beam.
- **Metadata:** every snapshot carries `snapshot_id`, `taken_at`, `source` and `domain`.
- **Vocabulary:** OpenConfig-style names where they exist.

**Scenario** (JSON, versioned; Pydantic is the single source of truth, and JSON Schema is exported from it):

```json
{
  "schema_version": "1.0", "domain": "ip", "snapshot_id": "lab-2026-11-19T10:00Z",
  "question": "p_meets_under_change",
  "topology": {"nodes": [...], "links": [{"id": "L1", "a": "r1", "z": "r2", "rate_mbps": 100, "delay_ms": 5}]},
  "traffic": {"demands": [{"id": "D1", "src": "r1", "dst": "r4", "protocol": "udp", "rate_mbps": 20}]},
  "events": [{"t": 30, "type": "link_down", "target": "L3"}],
  "change": {"set": [{"path": "links.L2.rate_mbps", "value": 50}], "events": []},
  "duration_s": 60,
  "requirements": [{"demand": "D1", "kpi": "loss_ratio", "op": "<", "value": 0.01}],
  "uncertain_params": [{"path": "links.L1.delay_ms", "dist": "normal", "mean": 5, "std": 0.5}],
  "seed_policy": {"runs": 20, "max_runs": 60, "adaptive": true, "base_seed": 1}
}
```

- `change` is the what-if. The **baseline** is the same scenario without it, run on the same seeds and inputs.
- **Two validation stages.** First **schema** validation. Then **semantic** validation:
  - every reference exists;
  - values are within physical and validated ranges;
  - events fall inside the duration;
  - the size is under a cap;
  - uncertain parameters point at real fields.
- An unknown `schema_version` is rejected.

## 5. The pipeline (`twin_predict`)

1. **Validate** (schema, then semantics). A failure returns the list of problems, for the agent to fix and resubmit (at most 3 times).
2. **Expand** into members: seed *i* and one sample of every uncertain parameter, shared by the baseline and the changed scenario.
3. **Run** each member in its own container: no network, read-only, CPU/memory/time limits, and the scenario mounted read-only.
4. **Aggregate** per demand and KPI: the distribution (mean, p5/p50/p95), whether each member meets each requirement, and the paired delta against the baseline.
5. **Adapt**: stop early once the Wilson interval of `p_meets` is clear of the policy thresholds, otherwise add another batch, up to `max_runs`.
6. **Calibrate.** Conformal intervals come from residuals stored per (domain, KPI). `p_meets` is recomputed with the residual distribution added (the model-error term). The trust tier is set from coverage, sample count and recency. Without calibration data the tier is **C (uncalibrated)**, and the policy treats it as such.
7. **Envelope**: prediction, `p_meets` and its interval, the paired effect, the uncertainty sources, the fidelity tier, the calibration figures, the trust tier, the snapshot age, the engine versions and the run id.
8. **Policy** (code) gives the action class and its reasons.

Identical requests are served from cache, keyed on a hash of (scenario, engine and schema versions).

## 6. Calibration and trust

- **Store:** `(domain, kpi, features, predicted p5/p50/p95, measured, source, at)`, where `source` is `testbed` or `synthetic`.
- **Conformal interval:** the absolute residual quantile at `ceil((n+1)(1−α))/n`.
- **Reported coverage:** the fraction of past measurements that fell inside the stated interval, plus a reliability diagram per KPI.
- **Trust tiers:**
  - **A:** n ≥ 30, coverage ≥ 0.85, and recent.
  - **B:** n ≥ 10, coverage ≥ 0.70.
  - **C:** otherwise.
- **Synthetic data never earns tier A**, and the envelope says when calibration is synthetic.

## 7. Policy (deterministic)

| Condition | Action |
|---|---|
| p ≥ 0.99, tier A, snapshot fresh, change allow-listed, step within bounds | eligible for auto-apply (still off by default) |
| 0.90 ≤ p < 0.99, tier A/B | propose; a human approves (CANAH's approval) |
| p < 0.90, a stale snapshot, tier C, or outside the validated range | reject, or gather more data |
| a change spanning domains | capped at the weakest twin's tier; approval required |

Every threshold, the allow-list and the step bounds are settings, and the gate's reasons are part of the envelope.

## 8. Domains

| Domain | ns-3 basis | First version | Limits to declare |
|---|---|---|---|
| **IP** (first) | internet stack, point-to-point, global routing, FlowMonitor | links (rate, delay, queue, loss), UDP/TCP demands, link down/up, throughput/delay/jitter/loss per demand | protocols and traffic, not router software |
| Optical | none native | GNPy computes GSNR per lightpath, which maps to BER and effective rate; ns-3 point-to-point channels with those parameters, plus restoration events | static QoT, no transients |
| Wireless | 5G-LENA / wifi | small gridded scenarios | research-grade, small scale |
| NTN | satellite/NTN modules | delay profiles, handover | least mature; check the toolchain first |

**Reuse test:** the second domain must need only a new engine program, schema block and skill. If the provider, pipeline or policy has to change, fix the abstraction first.

**Held (2026-11-20).** Before the optical and Wi-Fi twins, the IP-specific parts were moved behind a registry
(`netwins/domains.py`: model, checks, change, settable paths, engine, example, limits of fidelity, skill). The
twins then needed only their own module, engine and skill. Two shared fixes came with them, both for every
twin: aggregation takes every KPI an engine reports, and conformal intervals are centred on the learned bias
(a model that is consistently low is moved, not only widened).

## 9. Evaluation

1. **Agent:** 30–50 requests per domain. Measured: schema- and semantically-valid rate, first-try success, retries, end-to-end success. These run in CANAH's test-bed runner style, with checked answers.
2. **Fidelity:** predicted against measured (synthetic first, then the testbed). Measured: error per KPI, interval coverage, reliability.
3. **Decisions:** the policy on historical cases, with calibrated probabilities against point estimates. This is the key experiment.
4. **Failure injection:** out-of-range requests, stale snapshots, broken topologies, and a deliberately miscalibrated twin, each of which must be downgraded or rejected.
5. **Regression:** everything reruns when the model, schema, engine or ns-3 version changes.

## 10. Roadmap and status

| Phase | Work | Exit criterion | Status |
|---|---|---|---|
| 0. Foundations | schema, engine image, runner, aggregation, envelope, policy, unit tests | JSON → ns-3 ensemble → envelope round trip | **done** (2026-11-19): ns-3.43 image, 19 unit tests incl. a real-engine test |
| 1b. Live loop | mirror through the agentic MCP, write translation by contract verb, checks, overrides, the gate (now CANAH's, twin-neutral), SDN lab fixture | CANAH end to end on the lab: supported writes go, unsupported are held, questions reach the operator | **built** (2026-11-19); see the integration results |
| 1. IP twin in CANAH | provider, skill, state snapshots, adaptive paired ensembles, cache, eval set (30) | the eval set passes with no manual fixes | **partly**: provider, skill, snapshots, paired adaptive ensembles and cache built; not yet run inside CANAH; no eval set |
| 2. Calibration | synthetic truth, then testbed measurements on the IP lab; coverage and reliability | coverage near nominal on held-out data | **synthetic only**: raw p5–p95 coverage 0.0, conformal LOO 0.91 (n=90); no testbed data yet |
| 3. Decisions | policy eval on historical cases; approval → apply → verify → rollback through CANAH | calibrated beats point estimates | |
| 4. Optical | GNPy pre-processor plus point-to-point abstraction | GSNR/BER against testbed measurements within thresholds | **done** (2026-11-20): GNPy per lightpath (restoration, modes 100–400G, margin, BER) and a fluid packet layer; against the lab's physics (177 kept lines of edgeLM's Physical Layer Simulator, rebuilt from their launch powers and measured NF): raw bias −4.2 dB, MAE 4.65 dB; bias-corrected LOO MAE 2.5 dB; conformal 90% ±5.0 dB, coverage 0.91 |
| 5. Wireless | ns-3 Wi-Fi (bridged APs, roaming on one channel) | validated on controlled scenarios | **done** (2026-11-20): distance, airtime sharing, roaming within a channel and not across, on real ns-3 |
| 6. NTN | after checking the toolchains | validated on controlled scenarios | |
| 7. Live mode | real-time scheduler, shadow mode only | shadow predictions track reality | |

## 11. Risks

| Risk | Mitigation |
|---|---|
| Plausible but wrong twin | measured fidelity, conformal intervals, the tier downgrades on drift |
| Overconfident probabilities | the model-error term in `p_meets`, reliability measured, conservative thresholds |
| The model mis-sequences the pipeline | one tool runs it in code |
| The model invents numbers | CANAH's grounding checks; the envelope is the only source of figures |
| Scope creep | phase gates, one domain at a time |
| Version drift | a pinned engine image; versions in every envelope; regression eval |
| Latency | subnetwork extraction, adaptive ensembles, cache, parallel containers |
| Synthetic calibration mistaken for fidelity | labelled; never tier A |

## 12. Paper framing

**Claims:**
1. One agent and one pipeline drive several domain twins (reuse test).
2. Calibrated probabilities make better decisions than point estimates.
3. One pipeline tool and a validated harness give reliable scenario generation with a general local model.
4. Each twin's fidelity is measured and reported with its limits.

**Not claimed:** a universal twin, unified cross-domain physics, or autonomous changes to a live network.


## 13. The contract (2026-11-20)

netwins is used through `canah.twin/1`, the contract CANAH defines for any digital twin: describe, validate,
predict, and optionally check_write and extras, in one standard shape (decision, probability, trust, figures,
the change that ran, what is not modelled). The agent-side pieces moved to CANAH (`canah/twins`): the tools, the
write gate and the operators' overrides, which are now the same for any twin. netwins keeps the twins, the mirror
and the write translation, and serves the contract in-process (`netwins.contract:twin`) and over HTTP
(`python -m netwins serve`).

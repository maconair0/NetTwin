# netwins

Network digital twins on ns-3, built to be asked questions by an agent (CANAH) and to say how far
their answers can be trusted.

A question is a **scenario** (JSON): a topology, traffic, a proposed change, the requirements that
must hold and the inputs that are uncertain. `twin_predict` runs it end to end, in code:

1. **validate** — schema, then semantics (unknown nodes, links and demands, values outside the validated range)
2. **ensemble** — paired members: baseline and changed on the same seeds and the same sampled inputs; adaptive, so it grows only while `p_meets` is undecided
3. **aggregate** — per demand and KPI: distributions and the paired effect
4. **calibrate** — past residuals added to every member, split-conformal 90% intervals, a trust tier (A/B/C)
5. **envelope** — figures, probabilities, trust, what is not modelled, versions, a run id
6. **policy** — `reject`, `gather_more_data`, `propose` or `auto_apply_eligible` (off by default), with reasons

The language model writes the scenario and explains the envelope. It never sequences the pipeline and
never decides: an uncalibrated twin (tier C) can reject a change but never let one go ahead, and data
calibrated only on synthetic truth is never tier A.

See [plan.md](plan.md) for the design and roadmap ([docs/plan-v1.md](docs/plan-v1.md) is the original plan).

## Layout

| Path | What |
|---|---|
| `engine/` | ns-3.43 image (`netwins-ns3:3.43`) and the IP engine `ip/netwins-ip.cc`: JSON in, FlowMonitor KPIs out |
| `netwins/schema.py` | scenario model, limits, semantic checks |
| `netwins/runner.py` | sampling, paired members, Docker / local / fake engines |
| `netwins/aggregate.py`, `calibration.py`, `policy.py` | statistics, conformal calibration and tiers, the decision table |
| `netwins/twin.py` | the pipeline |
| `netwins/fidelity.py` | synthetic ground truth (hidden perturbations) for calibration |
| `netwins/state.py`, `data/snapshots/` | network snapshots and subnetwork extraction |
| `netwins/source.py`, `mirror.py` | the agentic MCP as a source; a controller's network mirrored into a snapshot |
| `netwins/changes.py`, `gate.py` | a controller write as a twin change; checks, overrides and the gate middleware |
| `netwins/canah_provider.py`, `skills/` | the CANAH tool provider; skills `ip-twin` (what-if) and `twin-first` (live changes) |
| `netwins/testing/sdn_lab.py`, `integrations/` | a test SDN controller; live end-to-end runs with CANAH |

## Use

```bash
python3 -m venv .venv && .venv/bin/pip install pydantic numpy pyyaml requests
docker build -t netwins-ns3:3.43 engine            # ~15 min the first time

.venv/bin/python -m netwins validate examples/acme-L12-failure.json
.venv/bin/python -m netwins predict  examples/acme-L12-failure.json      # --engine fake for no Docker
.venv/bin/python -m netwins calibrate-synthetic --n 30
.venv/bin/python -m netwins status
.venv/bin/python -m unittest discover -s tests -t .
```

State (calibration store, saved envelopes) lives in `NETWINS_HOME` (default `./netwins_data`).
Each ns-3 run is a container with no network, a read-only filesystem, one CPU, 512 MB and no capabilities.

## In CANAH: twin first, then the network

CANAH reaches the controllers through the agentic MCP. netwins reads the same agentic MCP (same URL and
token) and builds a twin of each packet controller's network. Every write CANAH is about to send is tried
in the twin first:

```
operator asks for a change
  → twin_mirror        the controller's network now: topology, capacities, services, the counters' traffic
  → twin_check_write   the write as a twin change; ns-3 runs the network with and without it; the policy decides
       propose          → the write is sent; the operator approves it (CANAH's approval)
       reject           → not sent; the agent says why, with the figures
       ask_operator     → the agent asks what the write leaves out (a rate, an end), then checks again
       not_modelled /   → the agent asks the operator; only the operator's word, as an approved
       gather_more_data   twin_override, lets the write through
  → TwinGate           the backstop: a write that reaches a controller unchecked is checked there and held
                       unless the twin supports it or the operator overrode it
```

- **Controller-neutral.** The mirror reads the contract's verbs (`read_devices`, `read_links`,
  `read_services`, `read_statistics`) through each controller's own tools, as the agentic MCP's
  onboarding mapped them. It reads ONOS, Ryu and other shapes.
- **Honest about its inputs.** Every link capacity and latency is marked measured or assumed. Traffic
  that no service accounts for, taken from two counter reads, runs as background load. A twin whose
  capacities are mostly assumed is capped at trust tier C, so it can reject a change but never let one
  go ahead.
- **Writes it understands, by contract verb:**
  - `provision`: a new demand;
  - `teardown`: a demand removed;
  - `adjust`: a new rate;
  - `configure_device`: a port shut (the link fails) or a port speed (the link's rate).

  Anything else is `not_modelled`, and the operator decides.

```yaml
# canah.yaml (netwins on PYTHONPATH)
providers: [canah.network:provider, netwins.canah_provider:provider]
middleware: [netwins.gate:TwinGate]
skills_dirs: [/path/to/netwins/skills]
```

Tools:
- for what-if questions: `twin_domains`, `twin_inspect`, `twin_subnetwork`, `twin_schema`,
  `twin_validate` and `twin_predict`;
- for the live loop: `twin_mirror`, `twin_check_write` and `twin_override`.

`twin_override` is the only one that waits for an operator's approval. None of them write to a network.

Try it without a real controller. [netwins/testing/sdn_lab.py](netwins/testing/sdn_lab.py) is an SDN
controller behind the agentic MCP's HTTP contract, with its own tool names, capacities, services,
counters that move, and a write gate:

```bash
CANAH_ROOT=~/CANAH ~/CANAH/.venv/bin/python integrations/canah_sdn_live.py   # CANAH + lab + twin, checked cases
```

## Status

- **IP domain:** runs end to end on real ns-3.
- **Live loop:** the twin mirrors a controller through the agentic MCP and checks every write, then the
  gate holds what the twin does not support. It runs inside CANAH against the SDN lab.
- **Real controllers:** mirroring ONOS and Ryu has been tested on their payload shapes, not yet against
  the live controllers.
- **Calibration:** synthetic only so far, so the best trust tier reachable today is B.
- **Other domains:** optical, wireless and NTN twins are planned, not built.

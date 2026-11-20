---
name: optical-twin
description: "How to answer a what-if about an optical (WDM) network with its digital twin: a fibre cut and whether lightpaths restore with enough margin, a longer or lossier span, a new lightpath or a faster mode, launch power. Use when asked about a lightpath's GSNR, OSNR, margin or BER, or what an optical change does to the traffic it carries."
---

# Asking the optical twin

The optical twin uses GNPy to work out each lightpath's quality of transmission (QoT) over the fibres it
crosses. It reports:
- **GSNR and OSNR** in 0.1 nm;
- **margin** over its transceiver mode's required OSNR;
- **pre-FEC BER**, path length and hops;
- the packet demands it carries (throughput, loss, delay), with the FEC cliff at zero margin.

1. **Start from the format.** Call `twin_schema("optical")` and copy its example. Put
   `"domain": "optical"` in the scenario.
2. **Take the network from the question when the operator describes it** (sites, lengths, positions,
   traffic), adding nothing, or from a snapshot when the twins have one of it. Neither needs a controller.
   **Describe the plant.** Read it from the network, never from memory:
   - `topology.nodes`: the ROADM sites;
   - `topology.links`: the fibres, with `length_km`, and `loss_db_per_km`, `span_km` (amplifier spacing)
     and `con_loss_db` if you know them;
   - `traffic.lightpaths`: each one's `src`, `dst`, `rate_gbps` (100, 200, 300 or 400), its `path` of
     fibre ids if fixed, and whether it `restore`s after a cut;
   - `traffic.demands`: the packet traffic riding each lightpath.
3. **Put the what-if in `change`:**
   - a cut is `{"events": [{"t": 0, "type": "link_down", "target": "<fibre>"}]}`;
   - a value is `{"set": [{"path": "links.<fibre>.loss_db_per_km", "value": 0.25}]}`;
   - a new lightpath goes in `add_lightpaths`.
4. **State the requirement on a lightpath or a demand.** For example
   `{"demand": "LP-1", "kpi": "margin_db", "op": ">=", "value": 3}`, or
   `{"demand": "acme", "kpi": "loss_ratio", "op": "<", "value": 0.01}`.
5. **Declare what is not known exactly,** as uncertain parameters: fibre loss per km, connector loss.
6. **Validate, then predict.** Call `twin_validate`, then `twin_predict`. Report:
   - the decision and its reasons;
   - the probability, and the margin's calibrated 90% range;
   - the reroute (path length, hops);
   - the trust tier, and what the twin does not model: static QoT, no transients, no wavelength
     assignment, GNPy's amplifier design.

If the calibration came from the lab's physics, say so; it is not hardware.

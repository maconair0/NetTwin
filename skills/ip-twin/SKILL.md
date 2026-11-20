---
name: ip-twin
description: "How to answer a 'what if' question about an IP network with the digital twin (netwins): a link failure, a capacity change, more traffic. Use when asked whether a change or a failure keeps a service's loss, delay or throughput within a requirement."
---

# Asking the IP twin

1. **Find the slice.** Call `twin_extra("subnetwork", {"network": "ip", "anchors": ["<the nodes or links involved>"]})` and copy its
   topology and demands into the scenario as they are. Never invent links or rates.
2. **Put the what-if in `change`.**
   - A failure goes in `change.events`: `{"t": 10, "type": "link_down", "target": "<link id>"}`.
   - A new value goes in `change.set`: `{"path": "links.<id>.rate_mbps", "value": 50}`.

   The twin also runs the network without the change, on the same seeds, and reports the difference.
3. **State the requirement** the operator cares about:
   `{"demand": "<id>", "kpi": "loss_ratio" | "delay_ms_mean" | "jitter_ms_mean" | "throughput_mbps", "op": "<", "value": ...}`.
   If none is given, ask which, or use loss < 1% for a service.
4. **Declare what is uncertain,** if known: for example a link's delay, as `normal` with its mean and std.
5. **Validate first.** Call `twin_validate(scenario)` and fix every problem it lists, then call `twin_predict(scenario)`.
6. **Report:**
   - the **decision** and its reasons, as given;
   - the **probability** that the requirement is met, and its interval;
   - the **effect** of the change (changed against baseline);
   - the **trust tier**, and what the twin **does not model**.

   Never round a probability up, or present a tier-C result as reliable.

The decision is the twin's policy's, not yours. With no change in the scenario it is "met" or "not_met"
(or "gather_more_data"): a statement about today, nothing to approve. "propose" means a human may approve the change through the
controller's own tools. The twin itself changes nothing.

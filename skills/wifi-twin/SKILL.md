---
name: wifi-twin
description: "How to answer a what-if about a Wi-Fi network with its digital twin: an access point failing and whether its stations roam without losing traffic, moving or adding an access point, transmit power, channel or width, more stations or more traffic. Use when asked about Wi-Fi coverage, roaming, contention or a station's throughput, delay or loss."
---

# Asking the Wi-Fi twin

The Wi-Fi twin runs ns-3:
- access points (802.11ax, ac or n) bridged to a wired backbone with a server;
- stations placed in metres;
- log-distance propagation;
- the traffic between stations and the server;
- per demand: throughput, delay, jitter, loss.

A station starts on the access point it hears loudest. When that access point fails, it can roam to
another on the same channel after ns-3's missed-beacon timeout.

1. **Start from the format.** Call `twin_schema("wifi")` and copy its example. Put
   `"domain": "wifi"` in the scenario.
2. **Take the network from the question when the operator describes it** (sites, lengths, positions,
   traffic), adding nothing, or from a snapshot when the twins have one of it. Neither needs a controller.
   **Describe the site:**
   - `aps`: `x`, `y`, `tx_power_dbm`, `channel`, `width_mhz`;
   - `stations`: `x`, `y`, and optionally the `ap` each starts on;
   - `radio`: `standard`, `path_loss_exponent` (about 2 in open space, 3 to 3.5 indoors).

   Every demand has `"server"` as one end.
3. **Put the what-if in `change`:**
   - a failure is `{"events": [{"t": 5, "type": "ap_down", "target": "<ap>"}]}`;
   - a move is `{"set": [{"path": "aps.<ap>.x", "value": 20}]}`;
   - more traffic goes in `add_demands`.
4. **State the requirement on a demand.** For example
   `{"demand": "camera", "kpi": "loss_ratio", "op": "<", "value": 0.01}`.
5. **Declare what is uncertain.** The path-loss exponent is the usual one:
   `{"path": "radio.path_loss_exponent", "dist": "uniform", "low": 2.8, "high": 3.4}`.
6. **Validate, then predict.** Report:
   - the decision and its reasons;
   - the probability, and the figures with and without the change;
   - the trust tier;
   - what is not modelled: walls and multipath beyond the exponent, roaming across channels, client
     vendor behaviour.

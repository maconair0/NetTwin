---
name: ntn-twin
description: "How to answer a what-if about a non-terrestrial network (satellite LEO/MEO/GEO or HAPS) with its digital twin: rain on a terminal or gateway, a satellite or gateway lost, inter-satellite links cut, another orbit, constellation or band, more traffic. Use when asked about satellite backhaul or direct-to-device coverage, availability, latency, link margin, handovers or Doppler."
---

# Asking the NTN twin

The NTN twin follows the satellites over a window of time (`duration_s`, every `step_s`):
- where each satellite is (circular orbits; a GEO stays over its longitude; a HAPS stays put);
- which satellites each site sees above its minimum elevation;
- the route: up from the source, across inter-satellite links when the satellites have them, down to the
  destination; the serving satellite is kept while it can be, and a handover interrupts for `handover_ms`;
- each hop's link budget (free-space, gaseous and rain loss, pointing loss, G/T) and the DVB-S2X MODCOD it allows.

Per flow: `availability`, `throughput_mbps`, `loss_ratio`, `delay_ms_mean`, `delay_ms_max`, `margin_db_min`,
`handovers`, `doppler_khz_max`.

1. **Start from the format.** Call `twin_schema("ntn")` and copy its example (`"domain": "ntn"`).
2. **Describe the system from the question**, adding nothing:
   - `sites`: terminals and gateways with `lat_deg`, `lon_deg`, `eirp_dbw`, `g_t_dbk`, `min_elevation_deg`;
   - a constellation as a Walker delta (`planes`, `per_plane`, `altitude_km`, `inclination_deg`, `isl`), or
     single `satellites` (`kind`: leo, meo, geo with `phase_deg` as its longitude, or haps with `lat_deg`, `lon_deg`);
   - `radio`: `uplink_ghz`, `downlink_ghz`, `bandwidth_mhz` (Ka is about 30 up and 20 down, Ku about 14 and 12);
   - `traffic.flows`: `src`, `dst`, `rate_mbps`.
3. **Put the what-if in `change`:**
   - rain: `{"events": [{"t": 0, "type": "rain", "target": "<site>", "value": 25}]}` (mm/h);
   - a satellite lost: `{"type": "satellite_down", "target": "<id>"}` (a constellation's are `<id>-P<plane>-S<slot>`);
   - inter-satellite links cut: `{"type": "isl_down", "target": "all"}`;
   - a gateway lost: `{"type": "site_down", "target": "<gateway>"}`;
   - more traffic: `{"add_flows": [...]}`; another value: `{"set": [{"path": "radio.bandwidth_mhz", "value": 500}]}`.
4. **State what must hold** in `requirements`, e.g. availability >= 0.99, delay_ms_mean < 50.
5. **Validate, then predict.** Report the decision, probability, trust tier and figures as given, and what it
   does not model (no interference between beams, simplified rain, circular orbits).

---
name: twin-first
description: "How to change a live network with its digital twin in the loop: mirror the controller's network, try every write in the twin before sending it, act on the twin's decision, and ask the operator when the twin cannot decide. Use for any request that creates, removes or changes a service, or configures a device, on a controller the agentic MCP holds."
---

# Change the network only after the twin has tried it

The twin is a copy of the live network, read from its controller: topology, link capacities, services,
and the traffic the counters show now. A change runs in the twin, with and without it, before it runs in
the network.

1. **Know the network.** On a controller you have not mirrored in this conversation, call
   `twin_mirror(controller)`. Note what it reports:
   - which link capacities and latencies were measured and which were assumed;
   - any background traffic that no service accounts for;
   - `write_tools`, the controller's own write tools and the contract verb each serves.

2. **Try the write.** Before sending any write, call
   `twin_check_write(controller, tool, arguments)`:
   - pass the controller's own tool name from `write_tools`, or a contract verb (`provision`,
     `teardown`, `adjust` or `configure_device`);
   - pass exactly the arguments you will send.

3. **Do what `next` says:**

   | action | what you do |
   |---|---|
   | `propose` | Send the write now, in this same turn, as `send_as` says (delegate to the controller's subagent if you do not hold its tools). The operator approves it. Do not stop at "I will now send it". |
   | `reject` | Do not send it, nor a smaller or different change in its place. Say what the twin predicts (the figures, which services suffer) and why. You may suggest what would fit (checked in the twin), then ask; send nothing until the operator chooses. |
   | `ask_operator` | Ask the operator the questions it lists, then stop. When they answer, check again with their answers in `answers` (for example `{"rate_mbps": 15}`). |
   | `gather_more_data`, `not_modelled` | Explain what the twin cannot settle and ask the operator how to proceed. |

4. **Override only on the operator's word.** If the operator says to go ahead with a write the twin did
   not support, call `twin_override(check_id, reason)`:
   - the `reason` is theirs, in their own words;
   - the override itself waits for approval;
   - then send the write you checked.

   Never override on your own judgement.

5. **Report:**
   - what was sent, or what was not and why;
   - the twin's probability and trust tier;
   - the figures behind them;
   - what the twin assumed.

   Read the result back from the controller before you say it is done.

A write that skips the check is not lost: the gate runs the check itself and holds the write unless the
twin supports it. But check first, so the operator sees the twin's answer before approving.

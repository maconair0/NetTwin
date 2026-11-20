// netwins IP twin: one ns-3 run of one scenario member.
//
//   netwins-ip --scenario=member.json --out=result.json --run=7
//
// The scenario (written by the Python runner, never by the LLM) is the canonical IP scenario with every
// uncertain parameter already sampled for this member:
//
//   nodes   [{"id": "r1"}, ...]
//   links   [{"id": "L1", "a": "r1", "z": "r2", "rate_mbps": 100, "delay_ms": 5, "queue_pkts": 100,
//             "loss_rate": 0.0}]
//   demands [{"id": "D1", "src": "r1", "dst": "r4", "protocol": "udp"|"tcp", "rate_mbps": 20,
//             "packet_bytes": 1200, "start_s": 1, "stop_s": 60}]
//   events  [{"t": 30, "type": "link_down"|"link_up", "target": "L3"}]
//   duration_s, routing: "global" (shortest path, recomputed on interface events)
//
// Output: per demand {throughput_mbps, delay_ms_mean, jitter_ms_mean, loss_ratio, tx_packets, rx_packets},
// plus per-link utilisation is not modelled here (v0). Exit code 0 on success, 2 on a bad scenario.
#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/traffic-control-module.h"

#include "json.hpp"

#include <fstream>
#include <map>

using namespace ns3;
using json = nlohmann::json;

namespace
{
struct LinkInfo
{
    Ptr<NetDevice> a, z;
    Ptr<Node> na, nz;
    uint32_t ia = 0, iz = 0; // interface indices on each node
};

void
SetLink(LinkInfo info, bool up)
{
    // Bring both ends of the link down (or up); global routing recomputes on the interface event.
    Ptr<Ipv4> ipa = info.na->GetObject<Ipv4>();
    Ptr<Ipv4> ipz = info.nz->GetObject<Ipv4>();
    if (up)
    {
        ipa->SetUp(info.ia);
        ipz->SetUp(info.iz);
    }
    else
    {
        ipa->SetDown(info.ia);
        ipz->SetDown(info.iz);
    }
    Ipv4GlobalRoutingHelper::RecomputeRoutingTables();
}

int
Fail(const std::string& why)
{
    std::cerr << "netwins-ip: " << why << std::endl;
    return 2;
}
} // namespace

int
main(int argc, char* argv[])
{
    std::string scenarioPath;
    std::string outPath = "result.json";
    uint32_t run = 1;
    CommandLine cmd;
    cmd.AddValue("scenario", "scenario member JSON", scenarioPath);
    cmd.AddValue("out", "result JSON", outPath);
    cmd.AddValue("run", "RNG run number (the ensemble seed)", run);
    cmd.Parse(argc, argv);

    std::ifstream in(scenarioPath);
    if (!in)
    {
        return Fail("cannot read " + scenarioPath);
    }
    json sc;
    try
    {
        in >> sc;
    }
    catch (const std::exception& e)
    {
        return Fail(std::string("not JSON: ") + e.what());
    }

    RngSeedManager::SetSeed(1);
    RngSeedManager::SetRun(run);
    Config::SetDefault("ns3::Ipv4GlobalRouting::RespondToInterfaceEvents", BooleanValue(true));

    const double duration = sc.value("duration_s", 60.0);

    // Nodes
    std::map<std::string, Ptr<Node>> nodes;
    NodeContainer all;
    for (const auto& n : sc.at("nodes"))
    {
        Ptr<Node> node = CreateObject<Node>();
        nodes[n.at("id").get<std::string>()] = node;
        all.Add(node);
    }
    InternetStackHelper stack;
    stack.Install(all);

    // Links: point-to-point, one /30 each.
    std::map<std::string, LinkInfo> links;
    Ipv4AddressHelper addr;
    uint32_t subnet = 0;
    for (const auto& l : sc.at("links"))
    {
        const std::string id = l.at("id");
        const std::string a = l.at("a"), z = l.at("z");
        if (!nodes.count(a) || !nodes.count(z))
        {
            return Fail("link " + id + " names an unknown node");
        }
        PointToPointHelper p2p;
        p2p.SetDeviceAttribute("DataRate",
                               StringValue(std::to_string(l.value("rate_mbps", 100.0)) + "Mbps"));
        p2p.SetChannelAttribute("Delay",
                                StringValue(std::to_string(l.value("delay_ms", 1.0)) + "ms"));
        p2p.SetQueue("ns3::DropTailQueue",
                     "MaxSize",
                     StringValue(std::to_string(l.value("queue_pkts", 100)) + "p"));
        NetDeviceContainer devs = p2p.Install(nodes[a], nodes[z]);
        const double loss = l.value("loss_rate", 0.0);
        if (loss > 0)
        {
            for (uint32_t i = 0; i < 2; ++i)
            {
                Ptr<RateErrorModel> em = CreateObject<RateErrorModel>();
                em->SetAttribute("ErrorRate", DoubleValue(loss));
                em->SetAttribute("ErrorUnit", StringValue("ERROR_UNIT_PACKET"));
                devs.Get(i)->SetAttribute("ReceiveErrorModel", PointerValue(em));
            }
        }
        std::ostringstream base;
        base << "10." << (subnet / 64) << "." << ((subnet % 64) * 4) << ".0";
        addr.SetBase(Ipv4Address(base.str().c_str()), "255.255.255.252");
        addr.Assign(devs);
        ++subnet;
        LinkInfo info;
        info.a = devs.Get(0);
        info.z = devs.Get(1);
        info.na = nodes[a];
        info.nz = nodes[z];
        info.ia = info.na->GetObject<Ipv4>()->GetInterfaceForDevice(info.a);
        info.iz = info.nz->GetObject<Ipv4>()->GetInterfaceForDevice(info.z);
        links[id] = info;
    }
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    // Demands: one sink + one source each, on their own port.
    std::map<std::string, std::pair<Ipv4Address, uint16_t>> demandKey; // id -> (dst addr, port)
    uint16_t port = 9000;
    for (const auto& d : sc.value("demands", json::array()))
    {
        const std::string id = d.at("id");
        const std::string src = d.at("src"), dst = d.at("dst");
        if (!nodes.count(src) || !nodes.count(dst))
        {
            return Fail("demand " + id + " names an unknown node");
        }
        Ptr<Ipv4> dip = nodes[dst]->GetObject<Ipv4>();
        if (dip->GetNInterfaces() < 2)
        {
            return Fail("demand " + id + ": " + dst + " has no link");
        }
        Ipv4Address dstAddr = dip->GetAddress(1, 0).GetLocal();
        const std::string proto = d.value("protocol", "udp");
        const double start = d.value("start_s", 1.0);
        const double stop = std::min(d.value("stop_s", duration), duration);
        const std::string factory =
            proto == "tcp" ? "ns3::TcpSocketFactory" : "ns3::UdpSocketFactory";

        PacketSinkHelper sink(factory, InetSocketAddress(Ipv4Address::GetAny(), port));
        ApplicationContainer sinks = sink.Install(nodes[dst]);
        sinks.Start(Seconds(0.0));
        sinks.Stop(Seconds(duration));

        if (proto == "tcp")
        {
            BulkSendHelper bulk(factory, InetSocketAddress(dstAddr, port));
            bulk.SetAttribute("MaxBytes", UintegerValue(0));
            ApplicationContainer app = bulk.Install(nodes[src]);
            app.Start(Seconds(start));
            app.Stop(Seconds(stop));
        }
        else
        {
            OnOffHelper onoff(factory, InetSocketAddress(dstAddr, port));
            onoff.SetConstantRate(DataRate(std::to_string(d.value("rate_mbps", 1.0)) + "Mbps"),
                                  d.value("packet_bytes", 1200));
            ApplicationContainer app = onoff.Install(nodes[src]);
            app.Start(Seconds(start));
            app.Stop(Seconds(stop));
        }
        demandKey[id] = {dstAddr, port};
        ++port;
    }

    // Events
    for (const auto& e : sc.value("events", json::array()))
    {
        const std::string type = e.at("type");
        const std::string target = e.at("target");
        if (!links.count(target))
        {
            return Fail("event names an unknown link " + target);
        }
        if (type != "link_down" && type != "link_up")
        {
            return Fail("unknown event type " + type);
        }
        Simulator::Schedule(Seconds(e.at("t").get<double>()), &SetLink, links[target], type == "link_up");
    }

    FlowMonitorHelper fmh;
    Ptr<FlowMonitor> monitor = fmh.InstallAll();
    Simulator::Stop(Seconds(duration));
    Simulator::Run();

    monitor->CheckForLostPackets();
    Ptr<Ipv4FlowClassifier> cls = DynamicCast<Ipv4FlowClassifier>(fmh.GetClassifier());
    json out;
    out["run"] = run;
    out["demands"] = json::object();
    for (const auto& d : sc.value("demands", json::array()))
    {
        const std::string id = d.at("id");
        const auto key = demandKey[id];
        const double start = d.value("start_s", 1.0);
        const double stop = std::min(d.value("stop_s", duration), duration);
        uint64_t txp = 0, rxp = 0, rxb = 0;
        double delaySum = 0, jitterSum = 0;
        for (const auto& [fid, st] : monitor->GetFlowStats())
        {
            Ipv4FlowClassifier::FiveTuple t = cls->FindFlow(fid);
            if (t.destinationAddress == key.first && t.destinationPort == key.second)
            {
                txp += st.txPackets;
                rxp += st.rxPackets;
                rxb += st.rxBytes;
                delaySum += st.delaySum.GetSeconds();
                jitterSum += st.jitterSum.GetSeconds();
            }
        }
        json r;
        r["tx_packets"] = txp;
        r["rx_packets"] = rxp;
        r["throughput_mbps"] = (stop > start) ? rxb * 8.0 / (stop - start) / 1e6 : 0.0;
        r["delay_ms_mean"] = rxp ? json(delaySum / rxp * 1000.0) : json(nullptr);
        r["jitter_ms_mean"] = rxp > 1 ? json(jitterSum / (rxp - 1) * 1000.0) : json(nullptr);
        r["loss_ratio"] = txp ? double(txp - std::min(rxp, txp)) / txp : 0.0;
        out["demands"][id] = r;
    }
    Simulator::Destroy();
    std::ofstream(outPath) << out.dump(1) << std::endl;
    return 0;
}

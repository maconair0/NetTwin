// netwins Wi-Fi twin: one ns-3 run of one scenario member.
//
//   netwins-wifi --scenario=member.json --out=result.json --run=7
//
// The ns-3 "wifi-wired-bridging" pattern: access points bridge their Wi-Fi to a wired (CSMA) backbone that
// a server sits on, and every station is in the one IP subnet, so a station whose access point fails can
// re-associate with another on the same channel and SSID and keep its address. The member (written by the
// Python runner, every uncertain parameter already sampled):
//
//   aps       [{"id", "x", "y", "tx_power_dbm", "channel", "width_mhz"}]
//   stations  [{"id", "x", "y", "ap"}]          ap: the access point it starts on (its channel is that AP's)
//   radio     {"standard": "ax"|"ac"|"n", "path_loss_exponent", "reference_loss_db"}
//   backbone  {"rate_mbps", "delay_ms"}
//   demands   [{"id", "src", "dst", "protocol", "rate_mbps", "packet_bytes", "start_s", "stop_s"}]
//             (src/dst: a station or "server")
//   events    [{"t", "type": "ap_down"|"ap_up", "target": <ap id>}]
//   duration_s
//
// Output: per demand {throughput_mbps, delay_ms_mean, jitter_ms_mean, loss_ratio, tx_packets, rx_packets};
// per station {"ap_at_end", "associated"}. Exit code 0 on success, 2 on a bad scenario.
#include "ns3/applications-module.h"
#include "ns3/bridge-module.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/network-module.h"
#include "ns3/propagation-module.h"
#include "ns3/wifi-module.h"

#include "json.hpp"

#include <fstream>
#include <map>

using namespace ns3;
using json = nlohmann::json;

namespace
{
int
Fail(const std::string& why)
{
    std::cerr << "netwins-wifi: " << why << std::endl;
    return 2;
}

void
SetAp(Ptr<WifiNetDevice> dev, bool up)
{
    // An access point that fails stops transmitting (its beacons stop, so its stations lose it after the
    // missed-beacon timeout and scan again); one that comes back resumes.
    if (up)
    {
        dev->GetPhy()->ResumeFromOff();
    }
    else
    {
        dev->GetPhy()->SetOffMode();
    }
}

std::string
ChannelSettings(int channel, int width)
{
    return "{" + std::to_string(channel) + ", " + std::to_string(width) + ", BAND_5GHZ, 0}";
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

    const double duration = sc.value("duration_s", 20.0);
    const json radio = sc.value("radio", json::object());
    const json backbone = sc.value("backbone", json::object());
    const std::string standard = radio.value("standard", "ax");

    // Nodes
    Ptr<Node> server = CreateObject<Node>();
    std::map<std::string, Ptr<Node>> aps, stas;
    std::map<std::string, json> apSpec;
    NodeContainer apNodes, staNodes;
    for (const auto& a : sc.at("aps"))
    {
        Ptr<Node> n = CreateObject<Node>();
        aps[a.at("id")] = n;
        apSpec[a.at("id")] = a;
        apNodes.Add(n);
    }
    for (const auto& s : sc.at("stations"))
    {
        Ptr<Node> n = CreateObject<Node>();
        stas[s.at("id")] = n;
        staNodes.Add(n);
    }
    if (aps.empty() || stas.empty())
    {
        return Fail("a Wi-Fi scenario needs at least one access point and one station");
    }

    // Positions
    MobilityHelper mob;
    mob.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    mob.Install(apNodes);
    mob.Install(staNodes);
    mob.Install(server);
    for (const auto& a : sc.at("aps"))
    {
        aps[a.at("id")]->GetObject<MobilityModel>()->SetPosition(
            Vector(a.value("x", 0.0), a.value("y", 0.0), 3.0));
    }
    for (const auto& s : sc.at("stations"))
    {
        stas[s.at("id")]->GetObject<MobilityModel>()->SetPosition(
            Vector(s.value("x", 0.0), s.value("y", 0.0), 1.0));
    }

    // Wired backbone: the server and every access point on one CSMA segment.
    CsmaHelper csma;
    csma.SetChannelAttribute(
        "DataRate",
        StringValue(std::to_string(backbone.value("rate_mbps", 1000.0)) + "Mbps"));
    csma.SetChannelAttribute("Delay", TimeValue(MilliSeconds(backbone.value("delay_ms", 0.5))));
    NodeContainer wired(server);
    wired.Add(apNodes);
    NetDeviceContainer csmaDevs = csma.Install(wired);

    // One radio channel shared by all: access points on the same channel contend and interfere.
    YansWifiChannelHelper channel;
    channel.SetPropagationDelay("ns3::ConstantSpeedPropagationDelayModel");
    channel.AddPropagationLoss("ns3::LogDistancePropagationLossModel",
                               "Exponent",
                               DoubleValue(radio.value("path_loss_exponent", 3.0)),
                               "ReferenceLoss",
                               DoubleValue(radio.value("reference_loss_db", 46.6777)));
    Ptr<YansWifiChannel> ch = channel.Create();

    WifiHelper wifi;
    wifi.SetStandard(standard == "n"    ? WIFI_STANDARD_80211n
                     : standard == "ac" ? WIFI_STANDARD_80211ac
                                        : WIFI_STANDARD_80211ax);
    wifi.SetRemoteStationManager("ns3::IdealWifiManager");
    Ssid ssid("netwins");

    InternetStackHelper stack;
    stack.Install(server);
    stack.Install(staNodes);
    Ipv4AddressHelper addr;
    addr.SetBase("10.1.0.0", "255.255.0.0");
    addr.Assign(NetDeviceContainer(csmaDevs.Get(0))); // the server

    // Access points: Wi-Fi bridged to the backbone.
    std::map<std::string, Ptr<WifiNetDevice>> apDev;
    std::map<Mac48Address, std::string> apOfBssid;
    uint32_t i = 1;
    for (const auto& a : sc.at("aps"))
    {
        const std::string id = a.at("id");
        YansWifiPhyHelper phy;
        phy.SetChannel(ch);
        phy.Set("ChannelSettings",
                StringValue(ChannelSettings(a.value("channel", 36), a.value("width_mhz", 20))));
        phy.Set("TxPowerStart", DoubleValue(a.value("tx_power_dbm", 20.0)));
        phy.Set("TxPowerEnd", DoubleValue(a.value("tx_power_dbm", 20.0)));
        WifiMacHelper mac;
        mac.SetType("ns3::ApWifiMac", "Ssid", SsidValue(ssid));
        NetDeviceContainer d = wifi.Install(phy, mac, aps[id]);
        Ptr<WifiNetDevice> w = DynamicCast<WifiNetDevice>(d.Get(0));
        apDev[id] = w;
        apOfBssid[Mac48Address::ConvertFrom(w->GetAddress())] = id;
        BridgeHelper bridge;
        NetDeviceContainer ports;
        ports.Add(w);
        ports.Add(csmaDevs.Get(i++));
        bridge.Install(aps[id], ports);
    }

    // Stations: each on the channel of the access point it starts with.
    std::map<std::string, Ptr<WifiNetDevice>> staDev;
    for (const auto& s : sc.at("stations"))
    {
        const std::string id = s.at("id");
        const std::string ap = s.value("ap", std::string(""));
        if (!apSpec.count(ap))
        {
            return Fail("station " + id + " starts on an unknown access point " + ap);
        }
        YansWifiPhyHelper phy;
        phy.SetChannel(ch);
        phy.Set("ChannelSettings",
                StringValue(ChannelSettings(apSpec[ap].value("channel", 36),
                                            apSpec[ap].value("width_mhz", 20))));
        WifiMacHelper mac;
        mac.SetType("ns3::StaWifiMac", "Ssid", SsidValue(ssid));
        NetDeviceContainer d = wifi.Install(phy, mac, stas[id]);
        staDev[id] = DynamicCast<WifiNetDevice>(d.Get(0));
        addr.Assign(d);
    }

    auto nodeOf = [&](const std::string& name) -> Ptr<Node> {
        if (name == "server")
        {
            return server;
        }
        return stas.count(name) ? stas[name] : nullptr;
    };

    // Demands
    std::map<std::string, std::pair<Ipv4Address, uint16_t>> demandKey;
    uint16_t port = 9000;
    for (const auto& d : sc.value("demands", json::array()))
    {
        const std::string id = d.at("id");
        Ptr<Node> src = nodeOf(d.at("src")), dst = nodeOf(d.at("dst"));
        if (!src || !dst)
        {
            return Fail("demand " + id + " names an unknown end (a station or \"server\")");
        }
        Ipv4Address dstAddr = dst->GetObject<Ipv4>()->GetAddress(1, 0).GetLocal();
        const std::string proto = d.value("protocol", "udp");
        const double start = d.value("start_s", 1.0);
        const double stop = std::min(d.value("stop_s", duration), duration);
        const std::string factory =
            proto == "tcp" ? "ns3::TcpSocketFactory" : "ns3::UdpSocketFactory";
        PacketSinkHelper sink(factory, InetSocketAddress(Ipv4Address::GetAny(), port));
        ApplicationContainer sinks = sink.Install(dst);
        sinks.Start(Seconds(0.0));
        sinks.Stop(Seconds(duration));
        if (proto == "tcp")
        {
            BulkSendHelper bulk(factory, InetSocketAddress(dstAddr, port));
            bulk.SetAttribute("MaxBytes", UintegerValue(0));
            ApplicationContainer app = bulk.Install(src);
            app.Start(Seconds(start));
            app.Stop(Seconds(stop));
        }
        else
        {
            OnOffHelper onoff(factory, InetSocketAddress(dstAddr, port));
            onoff.SetConstantRate(DataRate(std::to_string(d.value("rate_mbps", 1.0)) + "Mbps"),
                                  d.value("packet_bytes", 1200));
            ApplicationContainer app = onoff.Install(src);
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
        if (!apDev.count(target))
        {
            return Fail("event names an unknown access point " + target);
        }
        if (type != "ap_down" && type != "ap_up")
        {
            return Fail("unknown event type " + type);
        }
        Simulator::Schedule(Seconds(e.at("t").get<double>()), &SetAp, apDev[target], type == "ap_up");
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
    out["stations"] = json::object();
    for (const auto& [id, dev] : staDev)
    {
        Ptr<StaWifiMac> mac = DynamicCast<StaWifiMac>(dev->GetMac());
        json s;
        s["associated"] = mac->IsAssociated();
        s["ap_at_end"] = nullptr;
        if (mac->IsAssociated())
        {
            auto it = apOfBssid.find(mac->GetBssid(0));
            if (it != apOfBssid.end())
            {
                s["ap_at_end"] = it->second;
            }
        }
        out["stations"][id] = s;
    }
    Simulator::Destroy();
    std::ofstream(outPath) << out.dump(1) << std::endl;
    return 0;
}

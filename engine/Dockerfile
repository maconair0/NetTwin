# The netwins engines: ns-3 with the twin programs (netwins-ip, netwins-wifi), built once, run one container
# per ensemble member (the entrypoint is chosen per run: --entrypoint netwins-wifi).
#   docker build -t netwins-ns3:3.43-r2 engine
# Pinned: ns-3.43, nlohmann/json 3.11.3. Only the modules the twins use are built. ns-3 is built before the
# programs are copied in, so changing a program rebuilds only that program.
FROM ubuntu:24.04 AS build
ARG NS3=3.43
RUN apt-get update && apt-get install -y --no-install-recommends \
      g++ cmake ninja-build python3 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL https://gitlab.com/nsnam/ns-3-dev/-/archive/ns-${NS3}/ns-3-dev-ns-${NS3}.tar.gz | tar xz \
 && mv ns-3-dev-ns-${NS3} ns3
RUN curl -fsSL -o /src/ns3/scratch/json.hpp https://github.com/nlohmann/json/releases/download/v3.11.3/json.hpp
WORKDIR /src/ns3
ENV MODULES="core;network;internet;point-to-point;applications;flow-monitor;traffic-control;wifi;mobility;propagation;spectrum;csma;bridge"
RUN ./ns3 configure -d optimized -G Ninja --disable-examples --disable-tests \
      --enable-modules "$MODULES" \
 && ./ns3 build
COPY ip/netwins-ip.cc /src/ns3/scratch/netwins-ip.cc
COPY wifi/netwins-wifi.cc /src/ns3/scratch/netwins-wifi.cc
RUN ./ns3 configure -d optimized -G Ninja --disable-examples --disable-tests --enable-modules "$MODULES" \
 && ./ns3 build scratch/netwins-ip scratch/netwins-wifi \
 && mkdir -p /opt/netwins/lib /opt/netwins/bin \
 && cp build/scratch/ns${NS3}-netwins-ip-optimized /opt/netwins/bin/netwins-ip \
 && cp build/scratch/ns${NS3}-netwins-wifi-optimized /opt/netwins/bin/netwins-wifi \
 && cp build/lib/*.so /opt/netwins/lib/

FROM ubuntu:24.04
COPY --from=build /opt/netwins /opt/netwins
ENV LD_LIBRARY_PATH=/opt/netwins/lib PATH=/opt/netwins/bin:$PATH
# Ubuntu's image already has an unprivileged user with uid 1000 ("ubuntu"); the engine runs as it.
USER 1000
WORKDIR /work
ENTRYPOINT ["netwins-ip"]

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build

# Torii fork carries the FairTouchScreen replay-classifier endpoint
# (POST /touchscreen/classify) and the TD-pp-bypass passthrough on
# POST /performance. Both live entirely in the perf-server wrapper so
# the shared osu.Game DLLs ship to the client untouched.
ARG PERF_SERVER_REPO=https://github.com/ShikkesoraSIM/torii-performance-server.git
ARG PERF_SERVER_REF=601d057
# osu! difficulty/pp source: ppy's pp-dev rework branch (Reading skill etc.).
# Pinned to a SHA for reproducible builds. The old vernonlim ref was the Sunny
# MANIA rework and did NOT match what was actually deployed (no Reading skill);
# this is the real source the perf server runs as of 2026-06-29. The Torii RX
# policies (reading cap, CS/OD) live server-side in g0v0, not here, so this stays
# vanilla ppy. Bump this SHA to re-sync; rebuild + recreate performance-server.
ARG OSU_REPO=https://github.com/ppy/osu.git
ARG OSU_REF=34416814a220eb40024de2d397f8247b2fd2f8a4

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

# 1) Clone performance server source
RUN git clone --filter=blob:none ${PERF_SERVER_REPO} perf \
    && cd perf \
    && git checkout ${PERF_SERVER_REF}

# 2) Clone exact Sunny author-port osu source
RUN git clone --filter=blob:none ${OSU_REPO} osu \
    && cd osu \
    && git checkout ${OSU_REF}

# 3) Override csproj to use project refs to author-port source (server-side exact)
COPY PerformanceServer.author-port.csproj /src/perf/PerformanceServer/PerformanceServer.csproj

# 4) Build and publish
RUN dotnet publish /src/perf/PerformanceServer/PerformanceServer.csproj -c Release -o /out

FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime
WORKDIR /app
COPY --from=build /out /app

ENV ASPNETCORE_URLS=http://0.0.0.0:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "PerformanceServer.dll"]

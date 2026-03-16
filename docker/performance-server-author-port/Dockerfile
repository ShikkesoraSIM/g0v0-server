FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build

ARG PERF_SERVER_REPO=https://github.com/GooGuTeam/osu-performance-server.git
ARG PERF_SERVER_REF=cb976bc
ARG OSU_REPO=https://github.com/vernonlim/osu.git
ARG OSU_REF=c2f5ac34625264846a4379e313b96fc4debd06ac

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

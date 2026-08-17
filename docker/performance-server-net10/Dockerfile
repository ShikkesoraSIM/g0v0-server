FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build

# Torii perf-server rebuild sobre el osu ACTUAL del cliente (nova HEAD) para que
# el pp server matchee 1-1 al cliente + traiga la #9 flow-aim. nova referencia el
# framework/resources por ProjectReference vendored, asi que hay que clonarlos a
# ../../toriirefresh-{framework,resources} (mismos refs que usa el build de release).
ARG PERF_SERVER_REPO=https://github.com/ShikkesoraSIM/torii-performance-server.git
ARG PERF_SERVER_REF=601d057
ARG OSU_REPO=https://github.com/ShikkesoraSIM/torii-osu.git
ARG OSU_REF=4a32bc465225200ebbe03664ca7143b3d3b262a9
ARG FRAMEWORK_REPO=https://github.com/ShikkesoraSIM/torii-framework.git
ARG FRAMEWORK_REF=32974cded87da78c014f330928f0bb481d9847a9
ARG RESOURCES_REPO=https://github.com/ShikkesoraSIM/torii-resources.git
ARG RESOURCES_REF=e2442fadd68e5d440ca807295e700b2b7a6b8aaf

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

# 1) wrapper del perf-server (FairTouchScreen + TD passthrough viven aca)
RUN git clone --filter=blob:none ${PERF_SERVER_REPO} perf && cd perf && git checkout ${PERF_SERVER_REF}

# 2) osu del cliente en nova HEAD (tiene la #9 + algoritmo actual)
RUN git clone --filter=blob:none ${OSU_REPO} osu && cd osu && git checkout ${OSU_REF}

# 3) framework + resources vendored (nova los referencia por ProjectReference)
RUN git clone --filter=blob:none ${FRAMEWORK_REPO} toriirefresh-framework && cd toriirefresh-framework && git checkout ${FRAMEWORK_REF}
RUN git clone --filter=blob:none ${RESOURCES_REPO} toriirefresh-resources && cd toriirefresh-resources && git checkout ${RESOURCES_REF}

# 4) un solo pin de SDK para todo el build (el net10 del osu); sacar cualquier pin net8 del wrapper
RUN cp /src/osu/global.json /src/global.json && rm -f /src/perf/global.json

# 5) override del csproj del wrapper -> project refs al source del osu
COPY PerformanceServer.author-port.csproj /src/perf/PerformanceServer/PerformanceServer.csproj

# 6) build + publish
RUN dotnet publish /src/perf/PerformanceServer/PerformanceServer.csproj -c Release -o /out

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS runtime
WORKDIR /app
COPY --from=build /out /app

ENV ASPNETCORE_URLS=http://0.0.0.0:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "PerformanceServer.dll"]

FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build

# Torii perf-server rebuild sobre el osu ACTUAL del cliente (nova HEAD) para que
# el pp server matchee 1-1 al cliente + traiga la #9 flow-aim. nova referencia el
# framework/resources por ProjectReference vendored, asi que hay que clonarlos a
# ../../toriirefresh-{framework,resources} (mismos refs que usa el build de release).
ARG PERF_SERVER_REPO=https://github.com/ShikkesoraSIM/torii-performance-server.git
ARG PERF_SERVER_REF=601d057
ARG OSU_REPO=https://github.com/ShikkesoraSIM/torii-osu.git
ARG OSU_REF=94d3e79fea814e83e364fcbe11054e80a160831f
# OJO: estos DOS refs van sincronizados con el OSU_REF de arriba, son los mismos que
# FRAMEWORK_REF/RESOURCES_REF del build-gu.yml del tag que corre el cliente. Si se
# actualiza el OSU_REF y estos quedan viejos, el build muere con errores de override
# tipo CS0115 (el codigo de nova llama a APIs que ese framework todavia no tiene).
ARG FRAMEWORK_REPO=https://github.com/ShikkesoraSIM/torii-framework.git
ARG FRAMEWORK_REF=0ebb6908cbe70b763c6b4aab45dd6520b40c6bea
ARG RESOURCES_REPO=https://github.com/ShikkesoraSIM/torii-resources.git
ARG RESOURCES_REF=cddd70c3acba2e3cac84f6494f8cecd07a321710

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

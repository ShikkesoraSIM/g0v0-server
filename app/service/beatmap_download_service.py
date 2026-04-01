import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging

from fastapi import HTTPException
import httpx

logger = logging.getLogger(__name__)


@dataclass
class DownloadEndpoint:
    """下载端点配置"""

    name: str
    base_url: str
    health_check_url: str
    url_template: str  # 下载URL模板，使用{sid}和{type}占位符
    is_china: bool = False
    priority: int = 0  # 优先级，数字越小优先级越高
    timeout: int = 10  # 健康检查超时时间（秒）


@dataclass
class EndpointStatus:
    """端点状态"""

    endpoint: DownloadEndpoint
    is_healthy: bool = True
    last_check: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class BeatmapDownloadService:
    """谱面下载服务 - 负载均衡和健康检查"""

    def __init__(self):
        # 中国区域端点
        self.china_endpoints = [
            DownloadEndpoint(
                name="Sayobot",
                base_url="https://dl.sayobot.cn",
                health_check_url="https://dl.sayobot.cn/",
                url_template="https://dl.sayobot.cn/beatmaps/download/{type}/{sid}",
                is_china=True,
                priority=0,
                timeout=5,
            )
        ]

        # 国外区域端点
        self.international_endpoints = [
            DownloadEndpoint(
                name="OsuDirect",
                base_url="https://osu.direct",
                health_check_url="https://osu.direct/api/status",
                url_template="https://osu.direct/api/d/{sid}?noVideo={no_video}",
                is_china=False,
                priority=0,
                timeout=10,
            ),
            DownloadEndpoint(
                name="Ripple",
                base_url="https://storage.ripple.moe",
                health_check_url="https://storage.ripple.moe",
                url_template="https://storage.ripple.moe/d/{sid}",
                is_china=False,
                priority=1,
                timeout=10,
            ),
        ]

        # 端点状态跟踪
        self.endpoint_status: dict[str, EndpointStatus] = {}
        self._initialize_status()

        # 健康检查配置
        self.health_check_interval = 600  # 健康检查间隔（秒）
        self.max_consecutive_failures = 3  # 最大连续失败次数
        self.health_check_running = False
        self.health_check_task = None  # 存储健康检查任务引用

        # HTTP客户端
        self.http_client = httpx.AsyncClient(timeout=30)

    def _initialize_status(self):
        """初始化端点状态"""
        all_endpoints = self.china_endpoints + self.international_endpoints
        for endpoint in all_endpoints:
            self.endpoint_status[endpoint.name] = EndpointStatus(endpoint=endpoint)

    async def start_health_check(self):
        """启动健康检查任务"""
        if self.health_check_running:
            return

        self.health_check_running = True
        self.health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("Beatmap download service health check started")

    async def stop_health_check(self):
        """停止健康检查任务"""
        self.health_check_running = False
        await self.http_client.aclose()
        logger.info("Beatmap download service health check stopped")

    async def _health_check_loop(self):
        """健康检查循环"""
        while self.health_check_running:
            try:
                await self._check_all_endpoints()
                await asyncio.sleep(self.health_check_interval)
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(5)  # 错误时短暂等待

    async def _check_all_endpoints(self):
        """检查所有端点的健康状态"""
        all_endpoints = self.china_endpoints + self.international_endpoints

        # 并发检查所有端点
        tasks = []
        for endpoint in all_endpoints:
            task = asyncio.create_task(self._check_endpoint_health(endpoint))
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_endpoint_health(self, endpoint: DownloadEndpoint):
        """检查单个端点的健康状态"""
        status = self.endpoint_status[endpoint.name]

        try:
            async with httpx.AsyncClient(timeout=endpoint.timeout) as client:
                response = await client.get(endpoint.health_check_url)

                # 根据不同端点类型判断健康状态
                is_healthy = False
                if endpoint.name == "Sayobot":
                    # Sayobot 端点返回 200, 302 (Redirect), 304 (Not Modified) 表示正常
                    is_healthy = response.status_code in [200, 302, 304]
                else:
                    # 其他端点返回 200 表示正常
                    is_healthy = response.status_code == 200

                if is_healthy:
                    # 健康检查成功
                    if not status.is_healthy:
                        logger.info(f"Endpoint {endpoint.name} is now healthy")

                    status.is_healthy = True
                    status.consecutive_failures = 0
                    status.last_error = None
                else:
                    raise httpx.HTTPStatusError(
                        f"Health check failed with status {response.status_code}",
                        request=response.request,
                        response=response,
                    )

        except Exception as e:
            # 健康检查失败
            status.consecutive_failures += 1
            status.last_error = str(e)

            if status.consecutive_failures >= self.max_consecutive_failures:
                if status.is_healthy:
                    logger.warning(
                        f"Endpoint {endpoint.name} marked as unhealthy after "
                        f"{status.consecutive_failures} consecutive failures: {e}"
                    )
                status.is_healthy = False

        finally:
            status.last_check = datetime.now()

    def get_healthy_endpoints(self, is_china: bool) -> list[DownloadEndpoint]:
        """获取健康的端点列表"""
        endpoints = self.china_endpoints if is_china else self.international_endpoints

        healthy_endpoints = []
        for endpoint in endpoints:
            status = self.endpoint_status[endpoint.name]
            if status.is_healthy:
                healthy_endpoints.append(endpoint)

        # 按优先级排序
        healthy_endpoints.sort(key=lambda x: x.priority)
        return healthy_endpoints

    def _get_endpoint_pool(self, is_china: bool) -> list[DownloadEndpoint]:
        """获取端点池。中国用户优先中国镜像，失败时回退国际镜像。"""
        if is_china:
            return self.china_endpoints + self.international_endpoints
        return self.international_endpoints

    def _build_download_url(self, endpoint: DownloadEndpoint, beatmapset_id: int, no_video: bool) -> str:
        """根据端点配置生成下载 URL。"""
        if endpoint.name == "Sayobot":
            video_type = "novideo" if no_video else "full"
            return endpoint.url_template.format(type=video_type, sid=beatmapset_id)
        if endpoint.name in {"Nerinyan", "OsuDirect"}:
            return endpoint.url_template.format(sid=beatmapset_id, no_video="true" if no_video else "false")
        return endpoint.url_template.format(sid=beatmapset_id)

    def get_download_urls(self, beatmapset_id: int, no_video: bool, is_china: bool) -> list[str]:
        """获取下载 URL 列表（按优先级，健康端点优先）。"""
        endpoints = self._get_endpoint_pool(is_china)
        if not endpoints:
            return []

        healthy = [ep for ep in sorted(endpoints, key=lambda x: x.priority) if self.endpoint_status[ep.name].is_healthy]
        healthy_names = {ep.name for ep in healthy}
        unhealthy = [ep for ep in sorted(endpoints, key=lambda x: x.priority) if ep.name not in healthy_names]
        ordered_endpoints = healthy + unhealthy

        return [self._build_download_url(ep, beatmapset_id, no_video) for ep in ordered_endpoints]

    def get_download_url(self, beatmapset_id: int, no_video: bool, is_china: bool) -> str:
        """获取下载URL，带负载均衡和故障转移"""
        urls = self.get_download_urls(beatmapset_id=beatmapset_id, no_video=no_video, is_china=is_china)
        if not urls:
            raise HTTPException(status_code=503, detail="No download endpoints available")
        return urls[0]

    def get_service_status(self) -> dict:
        """获取服务状态信息"""
        status_info = {
            "service_running": self.health_check_running,
            "last_update": datetime.now().isoformat(),
            "endpoints": {},
        }

        for name, status in self.endpoint_status.items():
            status_info["endpoints"][name] = {
                "healthy": status.is_healthy,
                "last_check": status.last_check.isoformat() if status.last_check else None,
                "consecutive_failures": status.consecutive_failures,
                "last_error": status.last_error,
                "priority": status.endpoint.priority,
                "is_china": status.endpoint.is_china,
            }

        return status_info


# 全局服务实例
download_service = BeatmapDownloadService()

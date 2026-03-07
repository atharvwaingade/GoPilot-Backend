import logging
import sys
from dataclasses import dataclass, field

from config import settings


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@dataclass
class HardwareProfile:
    gpu_available: bool
    gpu_count: int
    gpu_names: list[str] = field(default_factory=list)
    gpu_vram_gb: list[float] = field(default_factory=list)

    @property
    def total_vram_gb(self) -> float:
        return sum(self.gpu_vram_gb)


def detect_hardware() -> HardwareProfile:
    logger = logging.getLogger(__name__)

    try:
        import torch

        if not torch.cuda.is_available():
            logger.info("CUDA not available — running in CPU-only mode")
            return HardwareProfile(gpu_available=False, gpu_count=0)

        count = torch.cuda.device_count()
        names: list[str] = []
        vrams: list[float] = []

        for i in range(count):
            name = torch.cuda.get_device_name(i)
            vram = round(torch.cuda.get_device_properties(i).total_memory / (1024 ** 3), 2)
            names.append(name)
            vrams.append(vram)
            logger.info("GPU %d: %s — %.2f GB VRAM", i, name, vram)

        return HardwareProfile(
            gpu_available=True,
            gpu_count=count,
            gpu_names=names,
            gpu_vram_gb=vrams,
        )

    except Exception as exc:
        logger.warning("Hardware detection failed: %s", exc)
        return HardwareProfile(gpu_available=False, gpu_count=0)


class Engine:
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self.hardware: HardwareProfile | None = None

    def startup(self) -> None:
        self._logger.info("Engine starting")
        self.hardware = detect_hardware()
        self._logger.info("GPU available: %s", self.hardware.gpu_available)

    def shutdown(self) -> None:
        self._logger.info("Engine stopped")


engine = Engine()
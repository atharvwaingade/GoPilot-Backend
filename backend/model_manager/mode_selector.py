import logging
from enum import Enum

from core.engine import HardwareProfile

logger = logging.getLogger(__name__)


class OperationMode(str, Enum):
    PERFORMANCE = "performance"
    LIGHTWEIGHT = "lightweight"


def select_mode(hardware: HardwareProfile) -> OperationMode:
    if hardware.gpu_available:
        logger.info("GPU detected — mode: performance")
        return OperationMode.PERFORMANCE

    logger.info("No GPU detected — mode: lightweight")
    return OperationMode.LIGHTWEIGHT
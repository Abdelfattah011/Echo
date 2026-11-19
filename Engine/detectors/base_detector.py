from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

SEVERITY_RANK = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}


class BaseBugDetector(ABC):
    """Abstract Base Class for all SQL Bug Detectors."""

    detector_name: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.detector_name:
            cls.detector_name = cls.__name__

    @abstractmethod
    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """Analyzes query steps and execution data to find anomalies."""
        pass

    def _format_bug_report(self, bug_type: str, severity: str, description: str) -> Dict[str, Any]:
        """Helper utility for consistent, standardized bug report formatting."""
        return {
            "bug_type": bug_type,
            "severity": severity,
            "description": description,
        }
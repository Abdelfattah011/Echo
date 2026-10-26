from abc import ABC, abstractmethod


class BaseBugDetector(ABC):
  """Abstract Base Class for all SQL Bug Detectors.

  Enables a plug-and-play architecture where new bug detectors can be added
  infinitely without modifying the core engine code.
  """

  @abstractmethod
  def detect(self, parsed_steps: list, execution_results: list) -> dict:
    """Analyzes query steps and execution data to find anomalies.

    Returns a bug report if detected, or None.
    """
    pass

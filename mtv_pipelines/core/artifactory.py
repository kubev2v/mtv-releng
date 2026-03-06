import logging
from typing import Any

logger = logging.getLogger(__name__)


class Artifactory:
    _instance = None

    def __init__(self):
        if not Artifactory._instance:
            Artifactory._instance = self
            self._store: dict[str, Any] = {}
            logger.info('"Artifactory initialized"')

    def put(self, task_name: str, output: Any):
        self._store[task_name] = output
        logger.info('"Artifactory put: %s"', task_name)

    def get(self, task_name: str) -> Any:
        if task_name not in self._store:
            raise KeyError(f"No output stored for task '{task_name}'")
        return self._store[task_name]

    def has(self, task_name: str) -> bool:
        return task_name in self._store

    def get_all(self) -> dict[str, Any]:
        return dict(self._store)

    def clear(self):
        self._store.clear()

import asyncio
import functools
import inspect
import json
import logging
import os
from argparse import Namespace
from enum import Enum, auto
from typing import Any, Callable, Type, get_type_hints

from models.dto import CollectorDTO

PRETTY_PRINT = "  "
PIPELINE_TASKS = {}

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    NOT_STARTED = auto()
    PENDING = auto()
    RUNNING = auto()
    FINISHED = auto()
    FAILED = auto()
    CANCELED = auto()
    SKIPPED = auto()


# Functional wrapper around a unit of work
class Task:
    def __init__(
        self,
        func: Callable,
        input_model: Type[Any],
        output_model: Type[Any],
    ):
        self.func: Callable = func
        self.name: str = func.__name__
        self.tg: asyncio.TaskGroup
        self.input_model: Type[Any] = input_model
        self.output_model: Type[Any] = output_model
        self.status = TaskStatus.NOT_STARTED
        self.input_data: Any | None = None
        self.output_data: Any | None = None
        self.subscribers: list["Task"] = []
        self.dependencies: list["Task"] = []

        # If the func has dependencies, create links between tasks
        self.dependencies = getattr(func, "_deps", [])
        for dep in self.dependencies:
            dep.subscribers.append(self)

    async def run(
        self, data: Any, args: Namespace, tg: asyncio.TaskGroup
    ) -> Any:
        return await self.func(data, args, tg)

    def validate(self):
        if len(self.dependencies) > 0:
            input_models = []
            for d in self.dependencies:
                input_models.append(d.output_model)
                logger.debug(
                    json.dumps(
                        {
                            "task_name": self.name,
                            "message": "Validating input model",
                            "expected": str(self.input_model),
                            "got": str(d.output_model),
                            "from_task": d.name,
                        }
                    )
                )
            if self.is_aggregated() and self.input_model != CollectorDTO:
                self._validation_failed(CollectorDTO)
            if len(input_models) == 1 and self.input_model != input_models[0]:
                self._validation_failed(input_models)
        for s in self.subscribers:
            s.validate()

    def _validation_failed(self, got):
        ex = RuntimeError(
            "Input models mismatch!\n"
            f"[Task] {self.name}\n"
            f"{PRETTY_PRINT}expected: {self.input_model}\n"
            f"{PRETTY_PRINT}got: {got}\n"
            f"{PRETTY_PRINT}from: {[d.name for d in self.dependencies]}"
        )
        logger.exception(ex)
        raise ex

    def can_run(self) -> bool:
        if self.status != TaskStatus.NOT_STARTED:
            return False
        return all(
            list(
                map(
                    lambda d: d.status == TaskStatus.FINISHED,
                    self.dependencies,
                )
            )
        )

    def has_deps(self) -> bool:
        return len(self.dependencies) > 0

    def is_aggregated(self) -> bool:
        return len(self.dependencies) > 1

    def has_input_data(self) -> bool:
        return self.input_data is not None

    def __str__(self):
        return f"[Task] {self.name}"

    def details(self):
        s = f"Name: {self.name}\n"
        s += f"Func: {self.func}\n"
        s += f"Status: {self.status}\n"
        s += f"Input: {self.input_model}\n"
        s += f"Output: {self.output_model}\n"
        s += f"Deps: {list(map(lambda d: d.name, self.dependencies))}\n"
        s += f"Subs: {list(map(lambda s: s.name, self.subscribers))}"
        return s


# Register a function as a Task.
# Input/Output DTOs are validated from python type hints.
def task(func: Callable):
    functools.wraps(func)
    pl_name = os.path.basename(inspect.getfile(func)).strip(".py")

    signature = inspect.signature(func)
    type_hints = get_type_hints(func)

    params = list(signature.parameters.keys())
    if not params:
        raise ValueError(
            f"[Func] {func.__name__} | must have an input DTO argument"
        )

    input_arg_name = params[0]
    input_type = type_hints.get(input_arg_name, Any)
    return_type = type_hints.get("return", Any)

    # if not input_type or not issubclass(input_type, BaseModel):
    #     raise TypeError(
    #         f"[Func] {func.__name__} | first arg must be a DTO model."
    #     )
    # if not return_type or not issubclass(return_type, BaseModel):
    #     raise TypeError(f"[Func] {func.__name__} | must return a DTO model.")

    t = Task(func, input_type, return_type)

    if not PIPELINE_TASKS.get(pl_name):
        PIPELINE_TASKS[pl_name] = []
    PIPELINE_TASKS[pl_name].append(t)

    return t


# Register task dependecies
def depends_on(*dependencies):
    def decorator_depends(obj: Callable):
        setattr(obj, "_deps", list(dependencies))
        return obj

    return decorator_depends


# Get pipeline tasks from pipeline __name__ attr
def get_pipeline_tasks(pl_name: str) -> list[Task]:
    return PIPELINE_TASKS.get(pl_name.replace("pipelines.", ""), [])

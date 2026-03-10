import asyncio
import logging
import signal
import time
from argparse import Namespace
from enum import Enum, auto

from core.artifactory import Artifactory
from core.db import DB
from core.task import Task, TaskStatus
from models.dto import CollectorDTO, EmptyDTO

PRETTY_PRINT = "  "
logger = logging.getLogger(__name__)


class PipelineStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    FINISHED = auto()
    FAILED = auto()
    CANCELED = auto()


class Pipeline:
    def __init__(self, name: str, tasks: list[Task]):
        self.name = name
        self.tasks = tasks
        self.args: Namespace
        self._pipeline_run_id: int | None = None
        self._task_run_ids: dict[str, int] = {}
        self._artifactory = Artifactory()
        self._canceled = False

    def validate(self):
        if len(self.tasks) == 0:
            raise RuntimeError(f"[Pipeline] {self.name} | No tasks present")

        for task in self.tasks:
            if task.can_run():
                task.validate()

    def _get_pipeline_run_id(self) -> int | None:
        try:
            db = DB()
            pipelines = db.read_pipelines(name=self.name)
            if not pipelines:
                return None
            run = db.write_pipeline_run(
                pipeline_id=pipelines[0].id,
                status=PipelineStatus.RUNNING.name,
                args={k: v for k, v in vars(self.args).items() if k != "func"},
                started_ts=int(time.time()),
            )
            return run.id
        except Exception as e:
            logger.error(f"Failed to create pipeline_run in DB: {e}")
            return None

    def _finish_pipeline_run(self, run_id: int, status: str):
        try:
            db = DB()
            db.update_pipeline_run_status(run_id, status)
            db.update_pipeline_run_ended_ts(run_id, int(time.time()))
        except Exception as e:
            logger.error(f"Failed to finish pipeline_run in DB: {e}")

    def _create_task_runs(self):
        if self._pipeline_run_id is None:
            return
        try:
            db = DB()
            for task in self.tasks:
                db_tasks = db.read_tasks(name=task.name)
                if not db_tasks:
                    continue
                run = db.write_task_run(
                    task_id=db_tasks[0].id,
                    pipeline_run_id=self._pipeline_run_id,
                    status=TaskStatus.PENDING.name,
                )
                self._task_run_ids[task.name] = run.id
        except Exception as e:
            logger.error(f"Failed to create task_runs in DB: {e}")

    def _update_task_run(
        self, task_name: str, status: str, ts_field: str = ""
    ):
        task_run_id = self._task_run_ids.get(task_name)
        if task_run_id is None:
            return
        try:
            db = DB()
            db.update_task_run_status(task_run_id, status)
            if ts_field == "started":
                db.update_task_run_started_ts(task_run_id, int(time.time()))
            elif ts_field == "ended":
                db.update_task_run_ended_ts(task_run_id, int(time.time()))
        except Exception as e:
            logger.error(f"Failed to update task_run in DB: {e}")

    def _skip_pending_task_runs(self):
        try:
            for task in self.tasks:
                if task.status == TaskStatus.NOT_STARTED:
                    task.status = TaskStatus.SKIPPED
                    self._update_task_run(task.name, TaskStatus.SKIPPED.name)
                    logger.info(f"{task} | Skipped")
        except Exception as e:
            logger.error(f"Failed to skip pending task_runs in DB: {e}")

    def _cancel_active_task_runs(self):
        for task in self.tasks:
            if task.status in (TaskStatus.NOT_STARTED, TaskStatus.RUNNING):
                task.status = TaskStatus.CANCELED
                self._update_task_run(
                    task.name, TaskStatus.CANCELED.name, ts_field="ended"
                )
                logger.info(f"{task} | Canceled")

    def _handle_sigint(self):
        logger.info("Received SIGINT, canceling pipeline...")
        self._canceled = True
        for async_task in asyncio.all_tasks():
            async_task.cancel()

    def _serialize_data(self, data) -> dict:
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        if hasattr(data, "model_dump"):
            return data.model_dump()
        return {"value": str(data)}

    def _set_task_run_input(self, task_name: str, data):
        task_run_id = self._task_run_ids.get(task_name)
        if task_run_id is None:
            return
        try:
            db = DB()
            db.update_task_run_input_data(
                task_run_id, self._serialize_data(data)
            )
        except Exception as e:
            logger.error(f"Failed to set task_run input_data: {e}")

    def _set_task_run_output(self, task_name: str, data):
        task_run_id = self._task_run_ids.get(task_name)
        if task_run_id is None:
            return
        try:
            db = DB()
            db.update_task_run_output_data(
                task_run_id, self._serialize_data(data)
            )
        except Exception as e:
            logger.error(f"Failed to set task_run output_data: {e}")

    async def _run_task(self, task: Task):
        logger.info(f"{task} | Started")
        logger.debug(f"{task} | input_model: {task.input_model}")
        task.status = TaskStatus.RUNNING
        self._update_task_run(
            task.name, TaskStatus.RUNNING.name, ts_field="started"
        )

        input_data = EmptyDTO()
        if task.is_aggregated():
            output_data = {}
            for dep in task.dependencies:
                if self._artifactory.has(dep.name):
                    output_data[dep.name] = self._artifactory.get(dep.name)
                elif dep.output_data is not None:
                    output_data[dep.name] = dep.output_data
            input_data = CollectorDTO(task_outputs=output_data)
            logger.debug(f"{task} | Using aggregator")
        elif task.has_deps():
            dep_name = task.dependencies[0].name
            if self._artifactory.has(dep_name):
                input_data = self._artifactory.get(dep_name)
            else:
                input_data = task.dependencies[0].output_data
            logger.debug(f"{task} | Using deps output")
        elif task.has_input_data():
            input_data = task.input_data
            logger.debug(f"{task} | Using task.input_data")
        else:
            logger.debug(f"{task} | Using Empty()")
        logger.debug(f"{task} | input_data: {input_data}")
        self._set_task_run_input(task.name, input_data)

        try:
            output_data = await task.run(input_data, self.args, self.tg)
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELED
            self._update_task_run(
                task.name, TaskStatus.CANCELED.name, ts_field="ended"
            )
            raise
        except Exception:
            task.status = TaskStatus.FAILED
            self._update_task_run(
                task.name, TaskStatus.FAILED.name, ts_field="ended"
            )
            raise

        task.output_data = output_data
        self._artifactory.put(task.name, output_data)
        logger.debug(f"{task} | output_data: {output_data}")
        self._set_task_run_output(task.name, output_data)
        task.status = TaskStatus.FINISHED
        logger.info(f"{task} | Finished")
        self._update_task_run(
            task.name, TaskStatus.FINISHED.name, ts_field="ended"
        )

        for subscriber in task.subscribers:
            logger.debug(f"{task} | Checking if {subscriber.name} sub can run")
            if subscriber.can_run():
                self.tg.create_task(self._run_task(subscriber))

    async def start(self):
        self.validate()
        self._pipeline_run_id = self._get_pipeline_run_id()
        self._create_task_runs()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._handle_sigint)

        try:
            async with asyncio.TaskGroup() as tg:
                self.tg = tg
                for task in self.tasks:
                    logger.debug(f"{task} | Checking if task can run")
                    if task.can_run():
                        tg.create_task(self._run_task(task))
        except BaseException as e:
            if self._canceled:
                logger.info("Pipeline canceled by user")
                self._cancel_active_task_runs()
                if self._pipeline_run_id is not None:
                    self._finish_pipeline_run(
                        self._pipeline_run_id,
                        TaskStatus.CANCELED.name,
                    )
            else:
                logger.error(f"Failed pipeline: {e}")
                self._skip_pending_task_runs()
                if self._pipeline_run_id is not None:
                    self._finish_pipeline_run(
                        self._pipeline_run_id,
                        TaskStatus.FAILED.name,
                    )
            raise
        else:
            if self._pipeline_run_id is not None:
                self._finish_pipeline_run(
                    self._pipeline_run_id, TaskStatus.FINISHED.name
                )
        finally:
            loop.remove_signal_handler(signal.SIGINT)

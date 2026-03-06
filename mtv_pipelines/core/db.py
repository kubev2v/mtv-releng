import logging
from typing import Sequence

from config.config import get_db_path
from sqlalchemy import (
    JSON,
    Engine,
    ForeignKey,
    Integer,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logger = logging.getLogger(__name__)

DB_PATH = get_db_path()


class Base(DeclarativeBase):
    pass


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, unique=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    num_tasks: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return (
            f"Pipeline(id={self.id}, name={self.name!r}, "
            f"num_tasks={self.num_tasks})"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "num_tasks": self.num_tasks,
        }


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, unique=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown"
    )
    args: Mapped[dict] = mapped_column(JSON, nullable=False, default={})
    started_ts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ended_ts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"PipelineRun(id={self.id}, status={self.status!r}, "
            f"args={self.args!r}, started_ts={self.started_ts}, "
            f"ended_ts={self.ended_ts}, pipeline_id={self.pipeline_id})"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "args": self.args,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "pipeline_id": self.pipeline_id,
        }


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, unique=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dependencies: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    subscribers: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    pipeline_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"Task(id={self.id}, name={self.name!r}, "
            f"dependencies={self.dependencies!r}, "
            f"subscribers={self.subscribers!r}, "
            f"pipeline_id={self.pipeline_id})"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "dependencies": self.dependencies,
            "subscribers": self.subscribers,
            "pipeline_id": self.pipeline_id,
        }


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, unique=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown"
    )
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False, default={})
    output_data: Mapped[dict] = mapped_column(JSON, nullable=False, default={})
    started_ts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ended_ts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=False
    )
    pipeline_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id"), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"TaskRun(id={self.id}, status={self.status!r}, "
            f"input_data={self.input_data!r}, "
            f"output_data={self.output_data!r}, "
            f"started_ts={self.started_ts}, ended_ts={self.ended_ts}, "
            f"task_id={self.task_id}, "
            f"pipeline_run_id={self.pipeline_run_id})"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "task_id": self.task_id,
            "pipeline_run_id": self.pipeline_run_id,
        }


class DB:
    _instance: "DB | None" = None
    _engine: Engine

    def __new__(cls, db_url: str = DB_PATH):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._engine = create_engine(db_url)
            Base.metadata.create_all(cls._instance._engine)
            logger.info("DB initialized with %s", db_url)
        return cls._instance

    def _session(self) -> Session:
        return Session(self._engine)

    def _get_or_raise(self, session: Session, model: type, row_id: int):
        row = session.get(model, row_id)
        if row is None:
            raise ValueError(f"{model.__name__} with id={row_id} not found")
        return row

    def write_pipeline(self, name: str, num_tasks: int = 0) -> Pipeline:
        row = Pipeline(name=name, num_tasks=num_tasks)
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        logger.debug("write_pipeline: %s", row)
        return row

    def read_pipelines(
        self,
        pipeline_id: int | None = None,
        name: str | None = None,
    ) -> Sequence[Pipeline]:
        stmt = select(Pipeline)
        if pipeline_id is not None:
            stmt = stmt.where(Pipeline.id == pipeline_id)
        if name is not None:
            stmt = stmt.where(Pipeline.name == name)
        with self._session() as s:
            results = s.scalars(stmt).all()
            s.expunge_all()
        logger.debug("read_pipelines: %d result(s)", len(results))
        return results

    def update_pipeline_num_tasks(
        self, pipeline_id: int, num_tasks: int
    ) -> Pipeline:
        with self._session() as s:
            row = self._get_or_raise(s, Pipeline, pipeline_id)
            row.num_tasks = num_tasks
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_pipeline_num_tasks: %s", row)
        return row

    def write_pipeline_run(
        self,
        pipeline_id: int,
        status: str = "not_set",
        args: dict | None = None,
        started_ts: int = 0,
        ended_ts: int = 0,
    ) -> PipelineRun:
        row = PipelineRun(
            pipeline_id=pipeline_id,
            status=status,
            args=args or {},
            started_ts=started_ts,
            ended_ts=ended_ts,
        )
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        logger.debug("write_pipeline_run: %s", row)
        return row

    def read_pipeline_runs(
        self,
        run_id: int | None = None,
        pipeline_id: int | None = None,
        status: str | None = None,
    ) -> Sequence[PipelineRun]:
        stmt = select(PipelineRun)
        if run_id is not None:
            stmt = stmt.where(PipelineRun.id == run_id)
        if pipeline_id is not None:
            stmt = stmt.where(PipelineRun.pipeline_id == pipeline_id)
        if status is not None:
            stmt = stmt.where(PipelineRun.status == status)
        stmt = stmt.order_by(PipelineRun.id.desc())
        with self._session() as s:
            results = s.scalars(stmt).all()
            s.expunge_all()
        logger.debug("read_pipeline_runs: %d result(s)", len(results))
        return results

    def update_pipeline_run_status(
        self, run_id: int, status: str
    ) -> PipelineRun:
        with self._session() as s:
            row = self._get_or_raise(s, PipelineRun, run_id)
            row.status = status
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_pipeline_run_status: %s", row)
        return row

    def update_pipeline_run_started_ts(
        self, run_id: int, started_ts: int
    ) -> PipelineRun:
        with self._session() as s:
            row = self._get_or_raise(s, PipelineRun, run_id)
            row.started_ts = started_ts
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_pipeline_run_started_ts: %s", row)
        return row

    def update_pipeline_run_ended_ts(
        self, run_id: int, ended_ts: int
    ) -> PipelineRun:
        with self._session() as s:
            row = self._get_or_raise(s, PipelineRun, run_id)
            row.ended_ts = ended_ts
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_pipeline_run_ended_ts: %s", row)
        return row

    def write_task(
        self,
        name: str,
        pipeline_id: int,
        dependencies: str = "[]",
        subscribers: str = "[]",
    ) -> Task:
        row = Task(
            name=name,
            pipeline_id=pipeline_id,
            dependencies=dependencies,
            subscribers=subscribers,
        )
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        logger.debug("write_task: %s", row)
        return row

    def read_tasks(
        self,
        task_id: int | None = None,
        name: str | None = None,
        pipeline_id: int | None = None,
    ) -> Sequence[Task]:
        stmt = select(Task)
        if task_id is not None:
            stmt = stmt.where(Task.id == task_id)
        if name is not None:
            stmt = stmt.where(Task.name == name)
        if pipeline_id is not None:
            stmt = stmt.where(Task.pipeline_id == pipeline_id)
        with self._session() as s:
            results = s.scalars(stmt).all()
            s.expunge_all()
        logger.debug("read_tasks: %d result(s)", len(results))
        return results

    def update_task(
        self,
        task_id: int,
        dependencies: str | None = None,
        subscribers: str | None = None,
    ) -> Task:
        with self._session() as s:
            row = self._get_or_raise(s, Task, task_id)
            if dependencies is not None:
                row.dependencies = dependencies
            if subscribers is not None:
                row.subscribers = subscribers
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task: %s", row)
        return row

    def write_task_run(
        self,
        task_id: int,
        pipeline_run_id: int,
        status: str = "unknown",
        input_data: dict | None = None,
        output_data: dict | None = None,
        started_ts: int = 0,
        ended_ts: int = 0,
    ) -> TaskRun:
        row = TaskRun(
            task_id=task_id,
            pipeline_run_id=pipeline_run_id,
            status=status,
            input_data=input_data or {},
            output_data=output_data or {},
            started_ts=started_ts,
            ended_ts=ended_ts,
        )
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        logger.debug("write_task_run: %s", row)
        return row

    def read_task_runs(
        self,
        run_id: int | None = None,
        task_id: int | None = None,
        pipeline_run_id: int | None = None,
        status: str | None = None,
    ) -> Sequence[TaskRun]:
        stmt = select(TaskRun)
        if run_id is not None:
            stmt = stmt.where(TaskRun.id == run_id)
        if task_id is not None:
            stmt = stmt.where(TaskRun.task_id == task_id)
        if pipeline_run_id is not None:
            stmt = stmt.where(TaskRun.pipeline_run_id == pipeline_run_id)
        if status is not None:
            stmt = stmt.where(TaskRun.status == status)
        stmt = stmt.order_by(TaskRun.id.desc())
        with self._session() as s:
            results = s.scalars(stmt).all()
            s.expunge_all()
        logger.debug("read_task_runs: %d result(s)", len(results))
        return results

    def update_task_run_status(self, run_id: int, status: str) -> TaskRun:
        with self._session() as s:
            row = self._get_or_raise(s, TaskRun, run_id)
            row.status = status
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task_run_status: %s", row)
        return row

    def update_task_run_started_ts(
        self, run_id: int, started_ts: int
    ) -> TaskRun:
        with self._session() as s:
            row = self._get_or_raise(s, TaskRun, run_id)
            row.started_ts = started_ts
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task_run_started_ts: %s", row)
        return row

    def update_task_run_ended_ts(self, run_id: int, ended_ts: int) -> TaskRun:
        with self._session() as s:
            row = self._get_or_raise(s, TaskRun, run_id)
            row.ended_ts = ended_ts
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task_run_ended_ts: %s", row)
        return row

    def update_task_run_input_data(
        self, run_id: int, input_data: dict
    ) -> TaskRun:
        with self._session() as s:
            row = self._get_or_raise(s, TaskRun, run_id)
            row.input_data = input_data
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task_run_input_data: %s", row)
        return row

    def update_task_run_output_data(
        self, run_id: int, output_data: dict
    ) -> TaskRun:
        with self._session() as s:
            row = self._get_or_raise(s, TaskRun, run_id)
            row.output_data = output_data
            s.commit()
            s.refresh(row)
            s.expunge(row)
        logger.debug("update_task_run_output_data: %s", row)
        return row

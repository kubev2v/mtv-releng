import asyncio
import datetime
import json
import logging
import os
import sys
import traceback
from argparse import ArgumentParser
from importlib import import_module

from core.db import DB
from core.pipeline import Pipeline
from core.task import get_pipeline_tasks
from pydantic import BaseModel

PRETTY_PRINT = "  "
PIPELINES = {}
PL_DIR = os.path.join(os.getcwd(), "mtv_pipelines", "pipelines")
logger = logging.getLogger(__name__)


def _sync_pipelines_to_db():
    try:
        db = DB()
        for pl_name in PIPELINES:
            tasks = get_pipeline_tasks(pl_name)
            existing = db.read_pipelines(name=pl_name)
            if not existing:
                pipeline = db.write_pipeline(
                    name=pl_name, num_tasks=len(tasks)
                )
            else:
                pipeline = existing[0]
                if pipeline.num_tasks != len(tasks):
                    db.update_pipeline_num_tasks(pipeline.id, len(tasks))

            for task in tasks:
                deps = json.dumps([d.name for d in task.dependencies])
                subs = json.dumps([s.name for s in task.subscribers])
                existing_task = db.read_tasks(
                    name=task.name, pipeline_id=pipeline.id
                )
                if not existing_task:
                    db.write_task(
                        name=task.name,
                        pipeline_id=pipeline.id,
                        dependencies=deps,
                        subscribers=subs,
                    )
                else:
                    t = existing_task[0]
                    if t.dependencies != deps or t.subscribers != subs:
                        db.update_task(
                            t.id,
                            dependencies=deps,
                            subscribers=subs,
                        )
    except Exception as e:
        logger.error(f"Could not sync pipelines to DB: {e}")


# Dynamically import all of the pipelines
def import_pipelines():
    # print(f"Importing pipelines from {PL_DIR}")
    for pl in os.listdir(PL_DIR):
        if pl not in [
            "__pycache__",
            "__init__.py",
        ]:
            pl_name = pl.removesuffix(".py")
            PIPELINES[pl_name] = import_module(f"pipelines.{pl_name}")


class JsonStringFormatter(logging.Formatter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def format(self, record):
        if (
            type(record.msg) == dict
            or type(record.msg) == list
            or type(record.msg) == BaseModel
        ):
            record.msg = json.dumps(json.dumps(record.msg))
            return super().format(record)
        record.msg = json.dumps(str(record.msg))
        return super().format(record)


def setup_logging(level, pipeline_name):
    log_format = (
        '{"level":"$levelname","time":"$asctime",'
        '"source":"$name","msg":$message}'
    )

    formatter = JsonStringFormatter(fmt=log_format, style="$")

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(
        f"logs/{pipeline_name}_{datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d_%H-%M-%S')}.log",
        mode="w",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)

    console_handler.setLevel(level)

    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def init_subparsers(root_parser: ArgumentParser):
    sub_parsers = root_parser.add_subparsers(
        title="Available pipelines",
        description="List of available pipelines that can be executed",
        help="Description",
        required=True,
        metavar="pipeline",
        dest="pipeline",
    )
    for pl_name, pl_module in PIPELINES.items():
        sub_parser = sub_parsers.add_parser(
            pl_name, help=pl_module.DESCRIPTION
        )
        pl_module.arg_parse(sub_parser)

        sub_parser.set_defaults(func=exec_pipeline)


def arg_parse():
    parser = ArgumentParser(
        description="MTV Pipelines runner",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Increase verbosity to DEBUG level",
    )
    parser.add_argument(
        "--quay",
        action="store_true",
        help="Try to replace image URLs with quay image URLs where possible",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Parallelize tasks (rendering of catalogs, downloading resources...)",
    )

    init_subparsers(parser)

    args = parser.parse_args()
    if args.verbose:
        setup_logging(logging.DEBUG, args.pipeline)
    else:
        setup_logging(logging.INFO, args.pipeline)
    return args


# Define subcommand exec function
def exec_pipeline(args):
    pipeline = Pipeline(args.pipeline, get_pipeline_tasks(args.pipeline))
    pipeline.args = args
    asyncio.run(pipeline.start())


def run():
    import_pipelines()
    args = arg_parse()
    _sync_pipelines_to_db()
    exec_pipeline(args)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logger.error(traceback.format_exc())

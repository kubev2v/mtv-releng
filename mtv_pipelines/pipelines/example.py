import logging
from argparse import ArgumentParser, Namespace
from asyncio import TaskGroup

from core.task import depends_on, task
from models.dto import CollectorDTO, EmptyDTO

"""
Each pipeline module (e.g. pipeline .py file) must have:
- DESCRIPTION const
    This const holds the description for the pipeline
- arg_parse function
    This function enables CLI configuration options.
    On CLI argument parsing, these arguments will be considered.
    If no parameters are needed, use empty function (e.g. with "pass")
- arbitrary number of decorated task functions
    These are your tasks that will be executed.
    If decorated only by @task, then they will be executed at the start of the pipeline
    If also annotated with @depends_on, you can set dependency chains
"""

DESCRIPTION = "Example pipeline to show how to configure it."

logger = logging.getLogger(__name__)


def arg_parse(arg_parser: ArgumentParser):
    arg_parser.add_argument(
        "-e",
        "--example-arg",
        required=True,
        help="Example argument that must be supplied",
    )


@task
async def example_task1(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> EmptyDTO:
    return data


@task
@depends_on(example_task1)
async def example_task2(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> EmptyDTO:
    logger.info("Hello from task2")
    return data


@task
@depends_on(example_task1)
async def example_task3(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> EmptyDTO:
    logger.info({"some_data": "Hello from task3"})
    return data


@task
@depends_on(example_task2, example_task3)
async def example_task4(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> CollectorDTO:
    return data

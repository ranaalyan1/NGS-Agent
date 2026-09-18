#!/usr/bin/env python3
import asyncio
import os

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

from workflows import activities
from workflows.pipeline_workflow import NGSPipelineWorkflow, NGSSampleWorkflow

load_dotenv()


async def main() -> None:
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    print(f"Connecting to Temporal at {temporal_host} ...")
    client = await Client.connect(temporal_host)
    worker = Worker(
        client,
        task_queue="ngs-pipeline",
        workflows=[NGSPipelineWorkflow, NGSSampleWorkflow],
        activities=[
            activities.ingest_activity,
            activities.qc_activity,
            activities.ai_decider_activity,
            activities.trim_activity,
            activities.align_activity,
            activities.bwa_activity,
            activities.gatk_activity,
            activities.annotation_activity,
            activities.annotate_activity,
            activities.coverage_activity,
            activities.count_activity,
            activities.de_activity,
            activities.insight_activity,
            activities.report_activity,
            activities.report_builder_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

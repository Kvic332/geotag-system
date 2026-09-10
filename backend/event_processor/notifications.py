"""Event notification publishing.

CLAUDE.md: "Enter / exit / dwell event notifications via SNS or webhook."
This module is the one place that talks to SNS. It is a no-op (structured log
only) whenever `ENVIRONMENT=local` or `SNS_TOPIC_ARN` is unset, so local dev
and tests never need a real AWS account.
"""

from __future__ import annotations

import json
from typing import Any

from shared import config
from shared.logging_utils import get_logger

logger = get_logger("geotag.event_processor.notifications")

_sns_client: Any = None


def _get_sns_client() -> Any:
    global _sns_client
    if _sns_client is None:
        import boto3  # local import: keep boto3 optional for local dev

        _sns_client = boto3.client("sns", region_name=config.aws_region())
    return _sns_client


def publish_event(event: dict[str, Any]) -> None:
    """Publish one enter/exit/dwell event. No-ops (with a log line) locally.

    Never raises — a notification failure must not fail position ingestion,
    which is the caller's actual job.
    """
    topic_arn = config.env("SNS_TOPIC_ARN")

    if config.is_local() or not topic_arn:
        logger.info(
            "event_notification_skipped",
            extra={
                "reason": "local_environment" if config.is_local() else "sns_topic_arn_unset",
                "event_type": event.get("event_type"),
                "device_id": event.get("device_id"),
                "geofence_id": event.get("geofence_id"),
            },
        )
        return

    try:
        client = _get_sns_client()
        client.publish(TopicArn=topic_arn, Message=json.dumps(event, default=str))
        logger.info(
            "event_notification_published",
            extra={"event_type": event.get("event_type"), "device_id": event.get("device_id")},
        )
    except Exception:  # noqa: BLE001 - never let SNS failure fail ingestion
        logger.exception(
            "event_notification_failed",
            extra={"event_type": event.get("event_type"), "device_id": event.get("device_id")},
        )

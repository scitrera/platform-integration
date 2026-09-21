"""Shared policy for tenant metrics bridges in Compose and Helm."""
# SPDX-License-Identifier: AGPL-3.0-only
from metering_config import openmeter_config


def environment(tenant, endpoint):
    return {"BILLING_TENANT": tenant, "OPENMETER_API_URL": endpoint,
            "BILLING_USAGE_ONLY": "true", "BILLING_USAGE_POLL_SECONDS": "300",
            "BILLING_USAGE_METER_SLUGS": ",".join(m["slug"] for m in openmeter_config()["meters"]),
            # The gateway ledger includes sidecar and direct backend LLM calls.
            "BILLING_METRICS_EXCLUDE": "tokens_in,tokens_out"}

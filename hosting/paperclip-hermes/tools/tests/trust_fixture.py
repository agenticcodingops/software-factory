"""Generate synthetic subscription IDs without adding identifier allowlist exceptions."""

import json
import uuid


def targets():
    return {
        "alpha": {"subscription_resource_id": "/subscriptions/00000000-0000-0000-0000-000000000000", "resource_group_name": "rg-fixture", "location": "uksouth"},
        "beta": {"subscription_resource_id": f"/subscriptions/{uuid.uuid5(uuid.NAMESPACE_DNS, 'beta.example.test')}", "resource_group_name": "rg-second", "location": "uksouth"},
    }


if __name__ == "__main__":
    print(json.dumps({"targets": targets()}))

"""Shared application-owned qualification registry; never populated by model output."""
import json
from pathlib import Path


def load_project(project, legacy_path):
    """Use the shared registry when present; legacy files only before migration.

    An absent project in an existing registry is deliberately unqualified. Never
    resurrect approvals from a legacy file after removing a registry entry.
    Task/prompt/contract/model/expiry admission is still enforced at dispatch.
    """
    legacy_path = Path(legacy_path)
    shared = legacy_path.parent / 'model_capabilities.json'
    if shared.exists():
        data = json.loads(shared.read_text())
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('projects'), dict):
            raise ValueError('invalid shared model capability registry')
        record = data['projects'].get(project)
    elif legacy_path.exists():
        record = json.loads(legacy_path.read_text())
    else:
        return None
    if record is not None and not isinstance(record, dict):
        raise ValueError('invalid project capability record')
    return record

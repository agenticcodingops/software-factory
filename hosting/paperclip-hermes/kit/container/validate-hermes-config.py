#!/usr/bin/env python3
"""Check the Hermes startup policy against the kit's fixed safety baseline.

Usage: validate-hermes-config.py POLICY

Restore installs hermes-config.yaml from the kit running it, never from the
archive; this check is defence in depth before any service reads it, and
configure-hermes.py runs the same check at every start before writing the
policy into the Hermes home. The baseline lives here, as constants, so the
validated file cannot supply its own allowlist. It is an allowlist: a safe-YAML
mapping with only the top-level sections below; model with only the allowed
keys, the OpenAI provider and base URL; terminal exactly the SSH sandbox
settings, with no environment passthrough; dashboard with only the keys
configure-hermes.py sets. Anything else, such as MCP servers, hooks, plugins,
toolsets or messaging platforms, is refused.
"""
from pathlib import Path
import stat
import sys

from ruamel.yaml import YAML

LIMIT = 1024 * 1024
SECTIONS = ('model', 'terminal', 'dashboard')
MODEL_KEYS = {'provider', 'base_url', 'default'}
MODEL = {'provider': 'openai', 'base_url': 'https://api.openai.com/v1'}
# The SSH sandbox service in compose.yaml; the key is the tmpfs copy
# hermes-entrypoint.sh installs.
TERMINAL = {
    'backend': 'ssh',
    'cwd': '/workspace',
    'ssh_host': 'sandbox',
    'ssh_user': 'hermes',
    'ssh_port': 2222,
    'ssh_key': '/run/kit/sandbox_ed25519',
    'env_passthrough': [],
    'client_env_passthrough': [],
}
# The keys configure-hermes.py sets; nothing else may configure the dashboard.
DASHBOARD_KEYS = {'public_url', 'trusted_proxies'}


def fail(message):
    raise SystemExit('Hermes policy rejected: ' + message)


def load(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
        fail(f'{path.name} must be a regular file under 1 MiB')
    return YAML(typ='safe').load(path.read_text(encoding='utf-8'))


def only(mapping, allowed, where):
    # repr() keeps control characters from untrusted keys out of the terminal.
    extra = sorted(repr(key) for key in mapping if key not in allowed)
    if extra:
        fail(f'{where} has keys the kit baseline does not allow: ' + ', '.join(extra))


def check(policy):
    if not isinstance(policy, dict):
        fail('the document must be a mapping')
    only(policy, set(SECTIONS), 'the policy')
    for section in SECTIONS:
        if not isinstance(policy.get(section), dict):
            fail(f'{section} must be a mapping')
    model = policy['model']
    only(model, MODEL_KEYS, 'model')
    for key, value in MODEL.items():
        if model.get(key) != value:
            fail(f'model.{key} differs from the kit baseline')
    if not isinstance(model.get('default', ''), str):
        fail('model.default must be a string')
    if policy['terminal'] != TERMINAL:
        fail('terminal differs from the kit baseline (SSH sandbox, no passthrough)')
    only(policy['dashboard'], DASHBOARD_KEYS, 'dashboard')


def main(policy):
    check(load(Path(policy)))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

"""Clean all SLB config from the NSAE device (one-time pre-test cleanup).

Dynamically discovers and removes virtual servers, groups, reals, and
health checks so the device starts from a clean SLB state.
"""
import re
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'INAGENT')
from INAGENT.utils import env_utils
env_utils.load_inagent_env()
from INAGENT.utils.ssh_client import create_ssh_client_from_env

c = create_ssh_client_from_env()
c.connect()
c.enter_enable_mode()

# ── 1. Discover existing objects ──────────────────────────────────────
def _parse_names(cmd, pattern):
    """Run a show command and extract quoted names via regex."""
    raw = c.send_command(cmd, wait=3.0)
    return re.findall(pattern, raw)

virtuals = _parse_names("show slb virtual all", r'slb virtual \w+ "([^"]+)"')
groups   = _parse_names("show slb group method", r'slb group method "([^"]+)"')
reals    = _parse_names("show slb real all", r'slb real \w+ "([^"]+)"')
healths  = _parse_names("show slb health", r'slb health "([^"]+)"')
members  = re.findall(
    r'slb group member "([^"]+)" "([^"]+)"',
    c.send_command("show slb group member", wait=3.0),
)
# health request/response indices
health_idx = set()
hr_raw = c.send_command("show running-config | include health", wait=5.0)
for m in re.finditer(r'health (?:request|response) (\d+)', hr_raw):
    health_idx.add(int(m.group(1)))

print(f"Discovered: {len(virtuals)} virtuals, {len(groups)} groups, "
      f"{len(reals)} reals, {len(healths)} healths, {len(members)} memberships, "
      f"{len(health_idx)} health req/resp indices")

# ── 2. Build ordered cleanup commands ────────────────────────────────
cleanup_cmds = []
# Disable + remove policies for virtuals
for v in virtuals:
    cleanup_cmds.append(f'no slb policy default "{v}"')
    cleanup_cmds.append(f'slb virtual disable "{v}"')
# Remove group health bindings
for g in groups:
    for h in healths:
        cleanup_cmds.append(f'no slb group health "{g}" "{h}"')
# Remove group members
for grp, real in members:
    cleanup_cmds.append(f'no slb group member "{grp}" "{real}"')
# Remove health server bindings, then health checks
for h in healths:
    cleanup_cmds.append(f'no health server "{h}"')
    cleanup_cmds.append(f'no slb health "{h}"')
# Remove health request/response entries
for idx in sorted(health_idx):
    cleanup_cmds.append(f'no health request {idx}')
    cleanup_cmds.append(f'no health response {idx}')
# Remove reals
for r in reals:
    cleanup_cmds.append(f'no slb real http "{r}"')
# Remove virtuals
for v in virtuals:
    cleanup_cmds.append(f'no slb virtual http "{v}"')
# Remove groups (must specify method to delete)
for g in groups:
    cleanup_cmds.append(f'no slb group method "{g}"')

if cleanup_cmds:
    results = c.execute_config_commands(cleanup_cmds, enter_config=True)
    ok = sum(1 for r in results if r['status'] == 'success')
    err = len(results) - ok
    print(f'Cleanup done: {ok} success, {err} errors/skips')
    for r in results:
        if r['status'] != 'success':
            print(f'  SKIP/ERR: {r["command"]} -> {r["output"][:120]}')
else:
    print('Nothing to clean.')

# ── 3. Verify clean state ────────────────────────────────────────────
# exit_config already done by execute_config_commands, just ensure enable
try:
    c.exit_config_mode()
except Exception:
    pass
c.enter_enable_mode()
for cmd in ['show slb virtual all', 'show slb real all', 'show slb group method', 'show slb health']:
    out = c.send_command(cmd, wait=3.0).strip()
    print(f'\n--- {cmd} ---')
    print(out[:300] if out else '(empty)')

c.disconnect()
print('\nDevice cleanup complete.')

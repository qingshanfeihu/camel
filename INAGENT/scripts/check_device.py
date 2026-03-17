"""Quick check: show current SLB state."""
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'INAGENT')
from INAGENT.utils import env_utils
env_utils.load_inagent_env()
from INAGENT.utils.ssh_client import create_ssh_client_from_env

c = create_ssh_client_from_env()
c.connect()
c.enter_enable_mode()
for cmd in ['show slb virtual http', 'show slb real http', 'show ip address']:
    r = c.execute_show_commands([cmd])
    out = r[0]['output'].strip() if r else ''
    # Remove NSAE# prompt lines
    lines = [l for l in out.split('\n') if l.strip() and 'NSAE' not in l]
    print(f'=== {cmd} ===')
    for l in lines:
        print(f'  {l}')
    if not lines:
        print('  (empty)')
c.disconnect()

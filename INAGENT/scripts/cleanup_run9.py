"""Cleanup run9 leftover configuration from NSAE device."""
import sys
sys.path.insert(0, r"C:\SynologyDrive\INFOAGEN")

from INAGENT.utils.ssh_client import NSAESSHClient

client = NSAESSHClient("172.16.6.215", "admin", "admin", port=22)
client.connect()

cleanup_cmds = [
    'no slb virtual enable "HTTP_VS"',
    'no slb policy default "HTTP_VS"',
    'no slb group health "WEB_SERVER_GROUP" "hc_content"',
    'no slb group member "WEB_SERVER_GROUP" "rs1"',
    'no slb group method "WEB_SERVER_GROUP"',
    'no health server "hc_content"',
    'no slb health "hc_content"',
    'no health request 1',
    'no health response 1',
    'no slb real http "rs1"',
    'no slb virtual http "HTTP_VS"',
]

results = client.execute_config_commands(cleanup_cmds)
for r in results:
    print(f"  {r.get('status', '?'):8s} | {r.get('command', '?')}")

# Verify cleanup
verify_results = client.execute_verify_commands([
    'show slb virtual http',
    'show slb real http',
    'show slb health',
])
print("\n--- Verify after cleanup ---")
for r in verify_results:
    print(f"  {r.get('command')}: {r.get('output', '').strip()[:100]}")

client.disconnect()
print("\nCleanup done.")

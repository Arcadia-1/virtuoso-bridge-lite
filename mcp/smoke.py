"""Portable, bounded stdio smoke check; does not contact Cadence."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    requests = [{'jsonrpc': '2.0', 'id': i, 'method': method,
                 'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}}
                for i, method in enumerate(['server/discover', 'tools/list', 'skills/list'], 1)]
    result = subprocess.run([sys.executable, str(root / 'mcp_server.py')],
        input=''.join(json.dumps(r) + '\n' for r in requests),
        capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr)
    responses = {r['id']: r for r in map(json.loads, result.stdout.splitlines())}
    assert '2026-07-28' in responses[1]['result']['supportedVersions']
    tools = responses[2]['result']['tools']
    assert {'startup', 'evaluate', 'build_schematic', 'run_test'} <= {t['name'] for t in tools}
    skills = responses[3]['result']['skills']
    assert skills
    print('SMOKE_OK tools=%d skills=%d' % (len(tools), len(skills)))


if __name__ == '__main__':
    main()

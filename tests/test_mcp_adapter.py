"""Offline adapter regressions. No Cadence, SSH or GUI is contacted."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import jsonschema
import pytest

from virtuoso_bridge.models import ExecutionStatus, SimulationResult, VirtuosoResult
from virtuoso_bridge.virtuoso.requests import RequestRecoveryError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vb_mcp_test', ROOT / 'mcp_server.py')
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


@pytest.fixture(autouse=True)
def isolated_adapter(monkeypatch):
    monkeypatch.setattr(mcp, '_CLIENT', {'obj': None, 'at': 0.0})
    monkeypatch.setattr(mcp, '_RATE', {})


def test_timeout_never_replays_mutation(monkeypatch):
    client = SimpleNamespace(execute_skill=Mock(side_effect=TimeoutError('response lost')))
    factory = Mock(return_value=client)
    monkeypatch.setattr(mcp, '_client', factory)
    response = mcp.dispatch_tool('evaluate', {'expr': 'mutate()'}, {})
    assert response['isError']
    assert response['structuredContent']['outcome'] == 'unknown'
    client.execute_skill.assert_called_once_with('mutate()', timeout=60, retry_connect=False)
    factory.assert_called_once()


def test_all_adapter_calls_disable_bridge_replay(monkeypatch):
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    monkeypatch.setattr(mcp.VirtuosoClient, 'execute_skill', execute)
    client = object.__new__(mcp._AdapterClient)
    client.execute_skill('mutate()', timeout=9, retry_connect=True)
    execute.assert_called_once_with('mutate()', timeout=9, retry_connect=False)


def uncertain_result():
    return VirtuosoResult(status=ExecutionStatus.ERROR, errors=['response pending'],
        metadata={'outcome': 'unknown', 'request_handle': {
            'request_id': 'a' * 32, 'daemon_instance': 'b' * 32, 'virtuoso_pid': 42},
            'history': 'Interactive.7', 'session': 'session-1'})


@pytest.mark.parametrize('raised', [False, True])
def test_original_request_diagnostics_survive(monkeypatch, raised):
    result = uncertain_result()
    execute = Mock(side_effect=RequestRecoveryError(result)) if raised else Mock(return_value=result)
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(execute_skill=execute))
    response = mcp.dispatch_tool('evaluate', {'expr': 'mutate()'}, {})
    assert response['isError']
    assert response['structuredContent'] == result.model_dump(mode='json')
    assert execute.call_count == 1


def test_cache_keeps_original_client_for_receipts(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr(mcp._AdapterClient, 'from_env', factory)
    first = mcp._client()
    mcp._CLIENT['at'] -= 10000
    assert mcp._client() is first
    factory.assert_called_once_with()


def test_start_delegates_to_registered_cli_with_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('VB_REMOTE_HOST', 'configured.example')
    monkeypatch.setenv('VB_REMOTE_PORT', '45678')
    monkeypatch.setenv('VB_VIRTUOSO_BIN', '/site/wrapper')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, 'load("setup.il")', ''))
    monkeypatch.setattr(subprocess, 'run', run)
    response = mcp._impl_bridge_start({})
    argv = run.call_args.args[0]
    assert Path(argv[0]).name in ('virtuoso-bridge', 'virtuoso-bridge.exe')
    assert argv[1:] == ['start']
    assert 'env' not in run.call_args.kwargs
    assert 'cwd' not in run.call_args.kwargs
    assert response['structuredContent']['setup_complete']
    assert 'load("setup.il")' in response['structuredContent']['output']


def test_real_registered_entrypoint_is_available():
    executable = Path(sys.executable).with_name(
        'virtuoso-bridge.exe' if sys.platform == 'win32' else 'virtuoso-bridge')
    result = subprocess.run([str(executable), '--help'], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'start' in result.stdout


@pytest.mark.parametrize('replace', [False, True])
def test_new_schematic_guards_existing_view_in_same_batch(monkeypatch, replace):
    from virtuoso_bridge.virtuoso.schematic import SchematicOps
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    client = SimpleNamespace(execute_operations=execute)
    client.schematic = SchematicOps(client)
    monkeypatch.setattr(mcp, '_client', lambda: client)
    args = {'lib': 'demo', 'cell': 'test'}
    if replace:
        args['replace'] = True
    mcp._impl_schematic_create(args)
    commands = execute.call_args.args[0]
    script = '\n'.join(commands)
    if replace:
        assert 'ddGetObj' not in script
    else:
        assert 'ddGetObj("demo" "test" "schematic")' in script
        assert 'error(' in script
        assert script.index('ddGetObj("demo" "test" "schematic")') < script.index('dbOpenCellViewByType')
    execute.assert_called_once()


@pytest.mark.parametrize('fresh,replace', [(None, False), (True, False), (False, False), (True, True)])
def test_batch_uses_existing_editor_with_explicit_overwrite_policy(monkeypatch, fresh, replace):
    from virtuoso_bridge.virtuoso.schematic import SchematicOps
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    client = SimpleNamespace(execute_operations=execute)
    client.schematic = SchematicOps(client)
    monkeypatch.setattr(mcp, '_client', lambda: client)
    args = {'lib': 'demo', 'cell': 'test', 'ops': [], 'replace': replace}
    if fresh is not None:
        args['fresh'] = fresh
    mcp._impl_schematic_build(args)
    commands = execute.call_args.args[0]
    assert ('ddGetObj' in commands[0]) == (fresh is not False and not replace)
    assert any(('"a")' if fresh is False else '"w")') in c for c in commands)


def test_maestro_reads_explicit_session_and_history(monkeypatch):
    read = Mock(return_value={'history': 'Interactive.3'})
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    mcp._impl_maestro_read({'session': 's1', 'history': 'Interactive.3'})
    read.assert_called_once_with('s1', history='Interactive.3')


@pytest.mark.parametrize('status,history,reads', [('done', '"Interactive.4"', 1),
    ('running', 'Interactive.4', 0), ('done', 'nil', 0)])
def test_maestro_reads_only_confirmed_run(monkeypatch, status, history, reads):
    events = []
    def run(**kwargs):
        events.append(('run', kwargs))
        return history, status
    read = Mock(side_effect=lambda *a, **kw: events.append(('read', a, kw)) or {'outputs': []})
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(
        maestro=SimpleNamespace(run_and_wait=run, read_results=read)))
    response = mcp._impl_maestro_run({'session': 's1', 'timeout': 300})
    assert events[0] == ('run', {'session': 's1', 'timeout': 300})
    assert read.call_count == reads
    if reads:
        read.assert_called_once_with('s1', history='Interactive.4')
    else:
        assert response['isError']


def test_maestro_start_uncertainty_is_not_retried(monkeypatch):
    result = uncertain_result()
    maestro = SimpleNamespace(run_and_wait=Mock(side_effect=RequestRecoveryError(result)), read_results=Mock())
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=maestro))
    response = mcp.dispatch_tool('run_test', {'session': 's1'}, {})
    assert response['structuredContent']['metadata'] == result.metadata
    maestro.run_and_wait.assert_called_once()
    maestro.read_results.assert_not_called()


@pytest.mark.parametrize('poll_error', [
    subprocess.TimeoutExpired('marker poll', 10),
    ConnectionError('SSH connection lost'),
    RuntimeError('Persistent SSH shell exited unexpectedly.'),
])
def test_maestro_poll_failure_preserves_acknowledged_run(monkeypatch, poll_error):
    from virtuoso_bridge.virtuoso.maestro import MaestroOps

    client = SimpleNamespace(
        execute_skill=Mock(side_effect=[
            VirtuosoResult(status=ExecutionStatus.SUCCESS, output='t'),
            VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"Interactive.42"'),
        ]),
        ssh_runner=SimpleNamespace(run_command=Mock(side_effect=[
            SimpleNamespace(returncode=0, stdout=''),
            poll_error,
        ])),
    )
    client.maestro = MaestroOps(client)
    monkeypatch.setattr(mcp, '_client', lambda: client)

    response = mcp.dispatch_tool('run_test', {'session': 's1', 'timeout': 30}, {})

    assert response['isError']
    metadata = response['structuredContent']['metadata']
    assert metadata['outcome'] == 'unknown'
    assert metadata['session'] == 's1'
    assert metadata['history'] == 'Interactive.42'
    assert metadata['phase'] == 'completion_wait'
    assert metadata['simulation_start_sent'] is True
    marker = metadata['completion_marker']
    assert marker.startswith('/tmp/vb_sim_done_')
    # Only the pre-start cleanup and one read-only poll: no retry or cleanup
    # after uncertainty, so the original callback marker remains observable.
    assert [call.args[0] for call in client.ssh_runner.run_command.call_args_list] == [
        f'rm -f {marker}', f'cat {marker} 2>/dev/null',
    ]
    calls = client.execute_skill.call_args_list
    assert len(calls) == 2
    assert sum('maeRunSimulation(' in call.args[0] for call in calls) == 1


def test_waveform_samples_exact_nearest_and_boundaries(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': [0., 1., 2.], 'OUT': [10., 20., 30.]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['OUT'],
                                  'at': [-1, 0, .4, .5, .8, 1, 2, 9]})
    rows = response['structuredContent']['samples']
    assert [r['t'] for r in rows] == [0, 0, 0, 0, 1, 1, 2, 2]
    assert [r['OUT'] for r in rows] == [10, 10, 10, 10, 20, 20, 30, 30]


def test_tiny_waveform_sample_keeps_full_precision(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': [0., 1.], 'I': [0., 2e-9]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['I'], 'at': [1.]})
    assert response['structuredContent']['samples'][0]['I'] == 2e-9


def test_waveform_extrema_cover_samples_after_old_display_limit(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': list(range(100001)), 'I': [0.] * 100000 + [2e-9]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['I']})
    stats = response['structuredContent']['stats']['I']
    assert stats['numeric'] == 100001
    assert stats['max'] == 2e-9


@pytest.mark.parametrize('name,args', [('list_libraries', {}), ('list_cells', {'lib': 'L'})])
def test_library_queries_preserve_uncertain_receipt(monkeypatch, name, args):
    result = uncertain_result()
    monkeypatch.setattr(mcp, '_eval_skill', lambda *a, **kw: result)
    response = mcp.dispatch_tool(name, args, {})
    assert response['isError']
    assert response['structuredContent']['metadata']['request_handle'] == result.metadata['request_handle']


@pytest.mark.parametrize('include', [None, 'INCLUDE', 'SOFTINCLUDE'])
def test_local_pdk_discovery_resolves_complete_variables_and_source_paths(
    monkeypatch, tmp_path, include,
):
    install = tmp_path / 'cadence'
    rf = install / 'libs' / 'RF'
    setup = tmp_path / 'setup'
    source_dir = setup / 'nested' if include else setup
    passive = source_dir / 'libs with space' / 'Passive'
    for lib in (rf, passive):
        lib.mkdir(parents=True)
        (lib / 'cdsinfo.tag').touch()
    main = setup / 'cds.lib'
    source = source_dir / 'cds.lib'
    source.write_text(
        'DEFINE RF $CDS_INST_DIR/libs/RF\n'
        'DEFINE Passive "libs with space/Passive"\n'
        + ('SOFTINCLUDE ../cds.lib\n' if include else ''),
        encoding='utf-8',
    )
    if include:
        main.write_text(f'{include} nested/cds.lib\n', encoding='utf-8')
    monkeypatch.setenv('CDS_INST_DIR', str(install))
    monkeypatch.setattr(mcp, '_cds_lib_files', lambda: [main])
    monkeypatch.setattr(mcp, '_pdk_roots', lambda: [])
    monkeypatch.setattr(mcp, '_PDK', {'at': 0.0, 'libs': []})

    response = mcp.dispatch_tool('list_pdk', {}, {})

    assert not response.get('isError')
    data = response['structuredContent']
    assert data['scope'] == 'server-local filesystem'
    assert {row['lib']: Path(row['path']).resolve() for row in data['libs']} == {
        'RF': rf.resolve(), 'Passive': passive.resolve(),
    }


def test_check_schematic_marks_uncertain_save_and_keeps_receipt(monkeypatch):
    from virtuoso_bridge.virtuoso.schematic.diagnostics import SchematicCheckSaveResult
    result = SchematicCheckSaveResult(
        status='operation_error', lib='L', cell='C', view='schematic',
        checked=False, saved=False, check_error_count=0, check_warning_count=0,
        log_capture_available=False, request_metadata=uncertain_result().metadata)
    check = Mock(return_value=result)
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(
        schematic=SimpleNamespace(check_and_save=check)))
    monkeypatch.setattr(mcp, '_editor_shot', lambda: None)
    response = mcp.dispatch_tool('check_schematic', {'lib': 'L', 'cell': 'C'}, {})
    assert response['isError']
    assert response['structuredContent']['request_metadata']['request_handle']
    assert response['structuredContent']['saved'] is False
    check.assert_called_once_with('L', 'C')


def test_screenshot_returns_real_png_block_and_original_failure(monkeypatch, tmp_path):
    from virtuoso_bridge.models import VirtuosoResult, ExecutionStatus
    shot = tmp_path / 'shot.png'
    shot.write_bytes(mcp._wave_png({'time': [0., 1.], 'OUT': [0., 1.]}))
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    take = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS, output=str(shot)))
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(screenshot=take))
    response = mcp.dispatch_tool('screenshot', {'target': 'schematic'}, {})
    assert any(part['type'] == 'image' and part['mimeType'] == 'image/png'
               for part in response['content'])
    assert response['structuredContent']['file'] == str(shot.resolve())
    take.assert_called_once_with(output=tmp_path, target='schematic', timeout=120)
    take.return_value = uncertain_result()
    response = mcp.dispatch_tool('screenshot', {'target': 'schematic'}, {})
    assert response['isError']
    assert response['structuredContent']['metadata']['request_handle']


def test_screenshot_rejects_target_path_escape_before_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    capture = Mock()
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(screenshot=capture))
    response = mcp.dispatch_tool('screenshot', {'target': '../../outside'}, {})
    assert response['isError']
    capture.assert_not_called()


def test_load_file_uses_local_tunnel_without_ssh_upload(monkeypatch, tmp_path):
    from virtuoso_bridge.transport.tunnel import SSHClient
    source = tmp_path / 'params.il'
    source.write_text('1 + 2\n', encoding='utf-8')
    tunnel = SSHClient(remote_host='localhost', port=65432)
    assert tunnel.ssh_runner is None
    client = mcp._AdapterClient.from_tunnel(tunnel)
    client._log_to_ciw = False
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    monkeypatch.setattr(client, 'execute_skill', execute)
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    monkeypatch.setattr(mcp, '_client', lambda: client)
    response = mcp.dispatch_tool('load_file', {'file': str(source)}, {})
    assert not response.get('isError')
    assert response['structuredContent']['metadata']['uploaded'] is False
    assert 'load(' in execute.call_args.args[0]


def test_snapshot_uses_registered_cli_and_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'snapshot failed'))
    monkeypatch.setattr(subprocess, 'run', run)
    response = mcp._impl_snapshot({})
    assert response['isError']
    assert 'snapshot failed' in response['content'][0]['text']
    assert Path(run.call_args.args[0][0]).name in ('virtuoso-bridge', 'virtuoso-bridge.exe')


def test_wave_plot_uses_elapsed_time_and_retains_short_peak():
    import struct
    import zlib
    png = mcp._wave_png({'time': [0., 1., 2., 100.], 'V': [0., 1., 0., 0.]},
                        width=120, height=50)
    assert png and png.startswith(b'\x89PNG')
    pos = 8
    chunks = []
    while pos < len(png):
        size = struct.unpack('>I', png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        if tag == b'IDAT':
            chunks.append(png[pos + 8:pos + 8 + size])
        pos += 12 + size
    rows = zlib.decompress(b''.join(chunks))
    width = 120
    def pixel(x, y):
        offset = y * (1 + width * 3) + 1 + x * 3
        return rows[offset:offset + 3]
    # The peak is at t=1/100, near the left edge, rather than at sample 1/3.
    blue = bytes((37, 99, 235))
    peak_xs = [x for x in range(width) if pixel(x, 8) == blue]
    assert peak_xs and max(peak_xs) < 20


def test_spectre_failure_keeps_run_diagnostics(monkeypatch, tmp_path):
    netlist = tmp_path / 'amp.scs'
    netlist.touch()
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    failed = SimulationResult(status=ExecutionStatus.ERROR,
        errors=['license checkout failed'], warnings=['site warning'],
        metadata={'output_dir': '/runs/amp.raw', 'returncode': 1})
    monkeypatch.setattr(mcp, '_spectre_sim', lambda *a: SimpleNamespace(
        run_simulation=Mock(return_value=failed)))
    response = mcp._impl_spectre_run({'netlist': str(netlist)})
    assert response['isError']
    details = response['structuredContent']
    assert details['netlist'] == str(netlist)
    assert details['errors'] == ['license checkout failed']
    assert details['metadata']['returncode'] == 1


def test_parallel_run_exposes_success_image_and_failed_run(monkeypatch, tmp_path):
    first = tmp_path / 'first.scs'
    second = tmp_path / 'second.scs'
    first.touch()
    second.touch()
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    successful = SimulationResult(status=ExecutionStatus.SUCCESS,
        data={'time': [0., 1., 2.], 'VOUT': [0., 1., 0.]},
        metadata={'output_dir': '/runs/first.raw'})
    failed = SimulationResult(status=ExecutionStatus.ERROR, errors=['bad model'])
    monkeypatch.setattr(mcp, '_spectre_sim', lambda *a: SimpleNamespace(
        run_parallel=Mock(return_value=[successful, failed])))
    response = mcp._impl_spectre_parallel({'netlists': [str(first), str(second)]})
    assert response['isError']
    assert response['structuredContent']['runs'][1]['errors'] == ['bad model']
    assert any(item['type'] == 'image' for item in response['content'])


def wire(requests):
    env = dict(__import__('os').environ)
    env['PYTHONPATH'] = str(ROOT / 'src')
    result = subprocess.run([sys.executable, str(ROOT / 'mcp_server.py')],
        input=''.join(json.dumps(r) + '\n' for r in requests),
        capture_output=True, text=True, timeout=20, cwd=ROOT, env=env)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_discovery_and_tools_validate_against_official_schema():
    schema = json.loads((ROOT / 'tests/fixtures/mcp/2026-07-28.schema.json').read_text())
    responses = wire([{'jsonrpc': '2.0', 'id': i, 'method': method,
        'params': {'_meta': {mcp.META_VER: '2026-07-28'}}}
        for i, method in enumerate(['server/discover', 'tools/list'])])
    for response, name in zip(responses, ['DiscoverResultResponse', 'ListToolsResultResponse']):
        contract = {**schema, '$ref': '#/$defs/' + name}
        jsonschema.validate(response, contract)
    assert 'io.modelcontextprotocol/tasks' not in responses[0]['result']['capabilities']['extensions']


@pytest.mark.parametrize('version,expected', [('2024-11-05', '2024-11-05'),
    ('2025-11-25', '2025-11-25'), ('2099-01-01', '2025-11-25')])
def test_legacy_negotiation(version, expected):
    response, = wire([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                      'params': {'protocolVersion': version, 'capabilities': {},
                                 'clientInfo': {'name': 'test', 'version': '1'}}}])
    assert response['result']['protocolVersion'] == expected


@pytest.mark.parametrize('method', ['tools/list', 'server/discover'])
def test_unsupported_modern_version_lists_supported_versions(method):
    response, = wire([{'jsonrpc': '2.0', 'id': 1, 'method': method,
        'params': {'_meta': {mcp.META_VER: '2099-01-01'}}}])
    assert response['error']['code'] == -32022
    assert response['error']['data'] == {'supported': mcp.SUPPORTED_VERSIONS, 'requested': '2099-01-01'}


def test_old_maestro_arguments_cannot_start_all_tests(monkeypatch):
    client = Mock()
    monkeypatch.setattr(mcp, '_client', client)
    response = mcp.dispatch_tool('run_test', {'test': 'TRAN', 'session': 's1'}, {})
    assert response['isError']
    client.assert_not_called()


def test_official_schema_rejects_the_old_discovery_field():
    schema = json.loads((ROOT / 'tests/fixtures/mcp/2026-07-28.schema.json').read_text())
    discovery = mcp._discover()
    discovery['protocolVersions'] = discovery.pop('supportedVersions')
    contract = {**schema, '$ref': '#/$defs/DiscoverResultResponse'}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'jsonrpc': '2.0', 'id': 1, 'result': discovery}, contract)


def test_bridge_prints_do_not_corrupt_rpc_stdout(monkeypatch, capsys):
    def evaluate(args):
        print('bridge setup diagnostic')
        return mcp._text_result('ok')
    monkeypatch.setitem(mcp.IMPL, 'evaluate', evaluate)
    mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'evaluate', 'arguments': {'expr': '1+1'}}})
    captured = capsys.readouterr()
    assert json.loads(captured.out)['id'] == 1
    assert 'bridge setup diagnostic' in captured.err

def test_large_output_does_not_discard_original_receipt():
    result = uncertain_result()
    result.output = 'x' * (mcp.STRUCT_LIMIT + 1)
    response = mcp._bridge_result(result)
    assert response['structuredContent']['metadata'] == result.metadata
    assert response['structuredContent']['output_truncated']
    assert response['isError']


def test_adapter_has_no_followup_feature_dependencies():
    names = {t['name'] for t in mcp._tool_defs()}
    assert not names & {'search', 'search_status', 'update_search', 'signoff', 'log_note'}
    assert {'search_docs', 'read_waveform'} <= names
    source = (ROOT / 'mcp_server.py').read_text(encoding='utf-8')
    assert 'unified_search' not in source
    assert 'parsers_fast' not in source


def test_document_search_uses_existing_remote_aware_bridge(monkeypatch):
    search = Mock(return_value={'results': []})
    close = Mock()
    monkeypatch.setattr(mcp, '_docs_client', lambda: SimpleNamespace(search_docs=search, close=close))
    monkeypatch.setattr(mcp, '_client', lambda: pytest.fail('doc lookup must not use CIW _client()'))
    mcp._impl_doc_search({'query': 'dbOpenCellViewByType', 'limit': 4})
    search.assert_called_once_with('dbOpenCellViewByType', limit=4)
    close.assert_called_once_with()
    assert mcp._CLIENT['obj'] is None


def test_maestro_read_and_run_report_no_results_as_error(monkeypatch):
    from virtuoso_bridge.virtuoso.maestro.reader import NoResults

    blocked = NoResults('the CIW is blocked by a dialog (PDK info)')
    read = Mock(return_value=blocked)
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    resp_read = mcp.dispatch_tool('test_results', {'session': 's1', 'history': 'Interactive.3'}, {})
    assert resp_read['isError'] is True
    assert blocked.reason in resp_read['content'][0]['text']
    assert resp_read['structuredContent'] == {
        'session': 's1', 'history': 'Interactive.3', 'reason': blocked.reason,
    }

    missing_csv = NoResults('maeExportOutputView did not produce a CSV for Interactive.5')
    maestro = SimpleNamespace(
        run_and_wait=Mock(return_value=('"Interactive.5"', 'done')),
        read_results=Mock(return_value=missing_csv),
    )
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=maestro))
    resp_run = mcp.dispatch_tool('run_test', {'session': 's1', 'timeout': 120}, {})
    assert resp_run['isError'] is True
    assert missing_csv.reason in resp_run['content'][0]['text']
    assert resp_run['structuredContent'] == {
        'session': 's1',
        'history': 'Interactive.5',
        'status': 'done',
        'reason': missing_csv.reason,
    }


def test_docs_client_avoids_ciw_owner_check_and_does_not_cache_client(monkeypatch):
    from virtuoso_bridge.transport.tunnel import SSHClient

    fake_ssh = SimpleNamespace(port=65111, close=Mock())
    monkeypatch.setattr(SSHClient, 'from_env', classmethod(lambda cls, **kw: fake_ssh))
    monkeypatch.setattr(
        mcp.VirtuosoClient,
        '_reject_cross_user_daemon_if_reachable',
        lambda *a, **kw: pytest.fail('docs client must not verify CIW owner'),
    )
    client = mcp._docs_client()
    try:
        assert isinstance(client, mcp._AdapterClient)
        assert client._tunnel is fake_ssh
        assert callable(client._install_root_probe)
        assert mcp._CLIENT['obj'] is None
    finally:
        client.close()
    fake_ssh.close.assert_called_once_with()


def test_startup_refreshes_unauthenticated_cached_client_and_preserves_active_client(monkeypatch):
    ok_proc = subprocess.CompletedProcess([], 0, 'load("setup.il")', '')
    fail_proc = subprocess.CompletedProcess([], 1, '', 'ssh failed')

    # Unauthenticated pre-startup client (_daemon_token=None, _daemon_caps=None) is closed and cleared.
    pre_startup = SimpleNamespace(_daemon_token=None, _daemon_caps=None, close=Mock())
    mcp._CLIENT['obj'] = pre_startup
    mcp._CLIENT['at'] = 123.0
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=ok_proc))
    resp = mcp._impl_bridge_start({})
    assert not resp.get('isError')
    pre_startup.close.assert_called_once_with()
    assert mcp._CLIENT['obj'] is None
    assert mcp._CLIENT['at'] == 0.0

    # Cached client with token but never handshaked (_daemon_caps=None) is also cleared.
    unconnected = SimpleNamespace(_daemon_token='ab' * 32, _daemon_caps=None, close=Mock())
    mcp._CLIENT['obj'] = unconnected
    resp = mcp._impl_bridge_start({})
    assert not resp.get('isError')
    unconnected.close.assert_called_once_with()
    assert mcp._CLIENT['obj'] is None

    # Active authenticated client preserves receipt context across startup.
    active = SimpleNamespace(_daemon_token='ab' * 32, _daemon_caps={'proto': 1}, close=Mock())
    mcp._CLIENT['obj'] = active
    resp = mcp._impl_bridge_start({})
    assert not resp.get('isError')
    active.close.assert_not_called()
    assert mcp._CLIENT['obj'] is active

    # Failed startup does not clear the cached client.
    stale = SimpleNamespace(_daemon_token=None, _daemon_caps=None, close=Mock())
    mcp._CLIENT['obj'] = stale
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=fail_proc))
    resp = mcp._impl_bridge_start({})
    assert resp['isError'] is True
    stale.close.assert_not_called()
    assert mcp._CLIENT['obj'] is stale


def test_describe_skill_uses_get_skill_more_info_and_handles_missing(monkeypatch):
    from virtuoso_bridge.virtuoso.skill_finder import SKILLFinderNotFoundError

    more_info = Mock(return_value={
        'func_name': 'dbOpenCellViewByType',
        'file_path': '$skdfref/cvio.html',
        'topic': 'pgfId-123',
        'raw_html': '<html><body>huge html</body></html>',
        'plain_text': 'Opens a cellview in the specified mode.',
    })
    close = Mock()
    monkeypatch.setattr(mcp, '_client', lambda: pytest.fail('describe_skill must not use _client()'))
    monkeypatch.setattr(
        mcp, '_docs_client',
        lambda: SimpleNamespace(
            get_skill_more_info=more_info,
            find_skill=Mock(side_effect=AssertionError('must not call find_skill')),
            close=close,
        ),
    )

    resp = mcp.dispatch_tool('describe_skill', {'func': 'dbOpenCellViewByType'}, {})
    assert not resp.get('isError')
    more_info.assert_called_once_with('dbOpenCellViewByType', missing_ok=False)
    close.assert_called_once_with()
    sc = resp['structuredContent']
    assert sc == {
        'func_name': 'dbOpenCellViewByType',
        'file_path': '$skdfref/cvio.html',
        'topic': 'pgfId-123',
        'plain_text': 'Opens a cellview in the specified mode.',
    }
    assert 'raw_html' not in sc
    assert 'Opens a cellview in the specified mode.' in resp['content'][0]['text']
    assert mcp._CLIENT['obj'] is None

    # Missing function entry -> _tool_error
    more_info.return_value = None
    resp_none = mcp.dispatch_tool('describe_skill', {'func': 'noSuchFunc'}, {})
    assert resp_none['isError'] is True
    assert 'noSuchFunc' in resp_none['content'][0]['text']

    # Missing doc installation -> _tool_error
    more_info.side_effect = SKILLFinderNotFoundError('no SKILL Finder data on eda-host')
    resp_err = mcp.dispatch_tool('describe_skill', {'func': 'dbOpenCellViewByType'}, {})
    assert resp_err['isError'] is True
    assert 'no SKILL Finder data' in resp_err['content'][0]['text']


def test_lookup_skill_uses_docs_client_without_poisoning_client(monkeypatch):
    find = Mock(return_value=[{'name': 'dbOpenCellViewByType', 'syntax': 'dbOpenCellViewByType(...)'}])
    close = Mock()
    monkeypatch.setattr(mcp, '_client', lambda: pytest.fail('lookup_skill must not use _client()'))
    monkeypatch.setattr(mcp, '_docs_client', lambda: SimpleNamespace(find_skill=find, close=close))
    resp = mcp.dispatch_tool('lookup_skill', {'query': 'dbOpen'}, {})
    assert not resp.get('isError')
    find.assert_called_once_with('dbOpen')
    close.assert_called_once_with()
    assert mcp._CLIENT['obj'] is None


def test_safe_path_expands_tilde_and_enforces_roots_before_exists(monkeypatch, tmp_path):
    fake_home = tmp_path / 'home'
    pdk_dir = fake_home / 'pdks'
    pdk_dir.mkdir(parents=True)
    netlist = pdk_dir / 'model.scs'
    netlist.write_text('// model\n', encoding='utf-8')
    outside_existing = tmp_path / 'outside_exists.scs'
    outside_existing.write_text('secret\n', encoding='utf-8')
    outside_missing = tmp_path / 'outside_missing.scs'

    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.setenv('USERPROFILE', str(fake_home))
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [pdk_dir.resolve()])

    # Tilde path inside allowed root resolves properly.
    assert mcp._safe_path('~/pdks/model.scs') == netlist.resolve()

    # Outside paths fail with identical root error regardless of existence.
    with pytest.raises(ValueError, match='path outside allowed roots'):
        mcp._safe_path(str(outside_existing), must_exist=True)
    with pytest.raises(ValueError, match='path outside allowed roots'):
        mcp._safe_path(str(outside_missing), must_exist=True)

    # Inside-root missing file reports file not found.
    with pytest.raises(ValueError, match='file not found'):
        mcp._safe_path('~/pdks/missing.scs', must_exist=True)


def test_library_and_cell_list_escape_scope_errors_and_truncation(monkeypatch):
    seen = []

    def fake_eval(expr, timeout):
        seen.append(expr)
        if 'ddGetLibList' in expr:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='("analogLib" "demo")')
        if 'missingLib' in expr:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"error: no lib"')
        if 'hugeLib' in expr:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='c:v,;' * 2000)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"inv:schematic,symbol,;"')

    monkeypatch.setattr(mcp, '_eval_skill', fake_eval)

    lib_resp = mcp.dispatch_tool('list_libraries', {}, {})
    assert not lib_resp.get('isError')
    assert lib_resp['structuredContent']['libs'] == ['analogLib', 'demo']
    assert seen[-1].startswith('let((vbOut) ')

    # Escapes quotes and trailing backslashes and uses let((vbLib vbOut vbVs) ...).
    tricky = 'bad"lib\\'
    cell_resp = mcp.dispatch_tool('list_cells', {'lib': tricky}, {})
    assert not cell_resp.get('isError')
    assert cell_resp['structuredContent']['truncated'] is False
    expr = seen[-1]
    assert expr.startswith('let((vbLib vbOut vbVs) ')
    assert 'ddGetObj("bad\\"lib\\\\")' in expr

    # Missing library returns isError=True instead of swallowing "error: no lib".
    miss_resp = mcp.dispatch_tool('list_cells', {'lib': 'missingLib'}, {})
    assert miss_resp['isError'] is True
    assert 'missingLib' in miss_resp['content'][0]['text']

    # Over-8000-char cell list flags truncated=True and caps cells at 8000 chars.
    huge_resp = mcp.dispatch_tool('list_cells', {'lib': 'hugeLib'}, {})
    assert not huge_resp.get('isError')
    assert huge_resp['structuredContent']['truncated'] is True
    assert len(huge_resp['structuredContent']['cells']) == 8000


def test_skills_get_rejects_traversal_and_deduplicates_skill_md(monkeypatch, tmp_path, capsys):
    skills_dir = tmp_path / 'skills'
    valid = skills_dir / 'demo'
    (valid / 'references').mkdir(parents=True)
    (valid / 'SKILL.md').write_text('---\nname: demo\n---\n# Demo\n', encoding='utf-8')
    (valid / 'references' / 'guide.md').write_text('# Guide\n', encoding='utf-8')

    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SKILL.md').write_text('# Secret\n', encoding='utf-8')
    (skills_dir / 'escape').symlink_to(outside)

    monkeypatch.setattr(mcp, 'SKILLS_DIR', skills_dir)

    def call_get(uri):
        mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'skills/get', 'params': {'uri': uri}})
        return json.loads(capsys.readouterr().out)

    # Valid skill returns SKILL.md exactly once.
    ok = call_get('skill://demo/SKILL.md')
    assert 'result' in ok
    uris = [r['uri'] for r in ok['result']['skill']['resources']]
    assert uris.count('skill://demo/SKILL.md') == 1
    assert 'skill://demo/references/guide.md' in uris

    # Every traversal / malformed / symlink-escape URI is rejected.
    for bad_uri in (
        '',
        'skill://',
        'skill://.',
        'skill://..',
        'skill:///etc/passwd',
        'skill://../outside',
        'skill://..\\outside',
        'skill://demo/../../outside',
        'skill://escape/SKILL.md',
    ):
        resp = call_get(bad_uri)
        assert 'error' in resp and resp['error']['code'] == -32602, (bad_uri, resp)


def test_simulate_and_simulate_many_default_to_spectre_and_handle_null_mode(monkeypatch, tmp_path):
    defs = {t['name']: t for t in mcp._tool_defs()}
    assert defs['simulate']['inputSchema']['properties']['mode']['default'] == 'spectre'
    assert defs['simulate_many']['inputSchema']['properties']['mode']['default'] == 'spectre'
    assert 'spectre mode' in defs['simulate']['description']

    net = tmp_path / 'amp.scs'
    net.write_text('// empty\n', encoding='utf-8')
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    seen_modes = []

    def fake_sim(mode, timeout):
        seen_modes.append((mode, timeout))
        ok = SimulationResult(
            status=ExecutionStatus.SUCCESS,
            data={'time': [0.0, 1.0], 'VOUT': [0.0, 1.0]},
            metadata={'output_dir': str(tmp_path)},
        )
        return SimpleNamespace(
            run_simulation=Mock(return_value=ok),
            run_parallel=Mock(return_value=[ok]),
        )

    monkeypatch.setattr(mcp, '_spectre_sim', fake_sim)

    for args in ({'netlist': str(net)}, {'netlist': str(net), 'mode': None}):
        resp = mcp.dispatch_tool('simulate', args, {})
        assert not resp.get('isError')
        assert resp['structuredContent']['mode'] == 'spectre'

    for args in ({'netlists': [str(net)]}, {'netlists': [str(net), ], 'mode': None}):
        resp = mcp.dispatch_tool('simulate_many', args, {})
        assert not resp.get('isError')
        assert resp['structuredContent']['mode'] == 'spectre'

    assert [m for m, _ in seen_modes] == ['spectre', 'spectre', 'spectre', 'spectre']


def test_read_waveform_surfaces_psf_parse_errors_and_rejects_invalid_at(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)

    # Binary / corrupt PSF file -> isError=True (not empty success).
    binary_psf = tmp_path / 'binary.tran'
    binary_psf.write_bytes(b'\x00\x01\x02\x03not psf ascii\xff\xfe')
    resp_bin = mcp.dispatch_tool('read_waveform', {'path': str(binary_psf)}, {})
    assert resp_bin['isError'] is True
    assert 'failed to parse PSF file' in resp_bin['content'][0]['text']

    # Empty PSF file -> surfaces parser's error message.
    empty_psf = tmp_path / 'empty.tran'
    empty_psf.write_text('', encoding='utf-8')
    resp_empty = mcp.dispatch_tool('read_waveform', {'path': str(empty_psf)}, {})
    assert resp_empty['isError'] is True
    assert 'PSF ASCII file is empty' in resp_empty['content'][0]['text']

    # Empty directory -> isError=True.
    empty_dir = tmp_path / 'empty_raw'
    empty_dir.mkdir()
    resp_dir = mcp.dispatch_tool('read_waveform', {'path': str(empty_dir)}, {})
    assert resp_dir['isError'] is True

    # Non-transient PSF (AC sweep with freq, no time vector) + at=[...] -> isError=True, never fabricates samples.
    ac_psf = tmp_path / 'ac.ac'
    ac_psf.write_text(
        'HEADER\nSWEEP\n"freq" 1\nTRACE\n"VO" "V"\n'
        'VALUE\n"freq" 1e6\n"VO" (1.0 0.0)\n"freq" 1e9\n"VO" (0.5 -0.5)\nEND\n',
        encoding='utf-8',
    )
    resp_ac_no_at = mcp.dispatch_tool('read_waveform', {'path': str(ac_psf), 'signals': ['VO']}, {})
    assert not resp_ac_no_at.get('isError')

    resp_ac_at = mcp.dispatch_tool('read_waveform', {'path': str(ac_psf), 'signals': ['VO'], 'at': [1e-9]}, {})
    assert resp_ac_at['isError'] is True
    assert 'time' in resp_ac_at['content'][0]['text'].lower()

    # Transient PSF with non-finite at values -> isError=True.
    tran_psf = tmp_path / 'wave.tran'
    tran_psf.write_text(
        'HEADER\nSWEEP\n"time" 1\nTRACE\n"OUT" "V"\n'
        'VALUE\n"time" 0.0\n"OUT" 1.0\n"time" 1e-9\n"OUT" 2.0\nEND\n',
        encoding='utf-8',
    )
    for bad_at in ([float('nan')], [float('inf')], [float('-inf')], ['nan'], [True]):
        resp_bad = mcp.dispatch_tool('read_waveform', {'path': str(tran_psf), 'signals': ['OUT'], 'at': bad_at}, {})
        assert resp_bad['isError'] is True, (bad_at, resp_bad)

    # Non-finite time vector -> isError=True when 'at' is provided.
    nan_time = tmp_path / 'nan_time.tran'
    nan_time.write_text(
        'HEADER\nSWEEP\n"time" 1\nTRACE\n"OUT" "V"\n'
        'VALUE\n"time" nan\n"OUT" 1.0\nEND\n',
        encoding='utf-8',
    )
    resp_nan_t = mcp.dispatch_tool('read_waveform', {'path': str(nan_time), 'signals': ['OUT'], 'at': [0.0]}, {})
    assert resp_nan_t['isError'] is True

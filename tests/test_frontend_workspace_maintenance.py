from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _run(script):
    prefix = """
import assert from 'node:assert/strict';
const ids = ['workspaceMaintenanceRefresh', 'workspaceStorageInspect', 'workspaceMaintenanceStatus',
  'workspaceStorageStatus', 'workspaceStorageRoots', 'workspaceBackupRecords', 'workspaceRunnerRecords'];
const elements = new Map(ids.map(id => [id, {textContent:'',innerHTML:'',disabled:false,addEventListener(){}}]));
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
const {createWorkspaceMaintenanceController} = await import('./static/js/workspace-maintenance.js');
const base = {schema_version:'workspace-maintenance-status-v1',status:'configured',scope_digest:'a'.repeat(64),
  storage_root_count:1,checked_at:'2026-10-16T03:00:00Z',backups:[],runners:[]};
const json = value => new Response(JSON.stringify(value), {headers:{'Content-Type':'application/json'}});
"""
    result = subprocess.run(["node", "--input-type=module", "-e", prefix + script], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_maintenance_load_is_lightweight_and_storage_binds_scope_identity():
    _run("""
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push([url, options]);
  return json(url.includes('/storage?') ? {status:'partial',roots:[],totals:{unique_bytes:1048576,unique_file_count:3},checked_at:base.checked_at} : base);
};
const controller = createWorkspaceMaintenanceController();
assert.equal(await controller.load(), true);
assert.equal(calls.length,1);
assert.equal(elements.get('workspaceStorageInspect').disabled,false);
await controller.inspectStorage();
assert.equal(calls[1][1].method,'POST');
assert.ok(calls[1][0].endsWith('scope_digest='+'a'.repeat(64)));
assert.match(elements.get('workspaceStorageStatus').textContent,/部分结果/);
assert.match(elements.get('workspaceStorageStatus').textContent,/1.00 MiB/);
controller.deactivate();
""")


def test_maintenance_does_not_render_late_response_after_leaving_surface():
    _run("""
let resolve;
globalThis.fetch = () => new Promise(done => {resolve=done;});
let visible=true;
const controller=createWorkspaceMaintenanceController({isVisible:()=>visible});
const pending=controller.load();
const before=elements.get('workspaceMaintenanceStatus').textContent;
visible=false;
controller.deactivate();
resolve(json(base));
assert.equal(await pending,false);
assert.equal(elements.get('workspaceMaintenanceStatus').textContent,before);
assert.equal(await controller.load(),false);
""")


def test_maintenance_renders_historical_verification_and_escapes_untrusted_names():
    _run("""
globalThis.fetch=async()=>json({...base,backups:[{name:'<img src=x onerror=alert(1)>',status:'manifest_checked',
  verification_record_status:'recorded',last_verified_at:base.checked_at,root_names:['<script>'],file_count:1,size_bytes:10}],
  runners:[{name:'<svg>',plan_id:'study',status:'fault',phase:'waiting',fault_codes:['heartbeat_expired']}]});
const controller=createWorkspaceMaintenanceController();
await controller.load();
const backup=elements.get('workspaceBackupRecords').innerHTML;
assert.ok(!backup.includes('<img') && backup.includes('&lt;img'));
assert.match(backup,/历史验证记录不代表当前文件/);
assert.match(elements.get('workspaceRunnerRecords').innerHTML,/巡检已超时/);
assert.match(elements.get('workspaceRunnerRecords').innerHTML,/运行环境及样本资格需独立验证/);
globalThis.fetch=async()=>{throw new Error('private path');};
await controller.load();
assert.match(elements.get('workspaceMaintenanceStatus').textContent,/读取失败/);
assert.equal(elements.get('workspaceStorageInspect').disabled,true);
assert.ok(!elements.get('workspaceMaintenanceStatus').textContent.includes('private path'));
controller.deactivate();
""")

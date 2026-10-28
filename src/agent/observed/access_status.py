"""Keep actual SDK response status visible without using it to prove historical causes."""
import json
from diagnostics.contract import timestamp

MAX_CHECKS = 64
IDENTITY = ('tenant', 'observed_id', 'service', 'env', 'cluster', 'instance', 'boot_id')
INTERFACES = {'snapshot': '/_aiops/snapshot', 'capabilities': '/_aiops/capabilities',
              'health': '/health', 'probe_observations': '/_aiops/probe-observations'}
ERROR_LABELS = {'connect_timeout': '连接建立超时', 'read_timeout': '读取响应超时',
    'connection_refused': '连接被拒绝', 'dns_error': 'DNS解析失败', 'connection_error': '连接失败',
    'unauthorized': '鉴权未通过', 'forbidden': '读取权限被拒绝', 'not_found': '登记接口未找到',
    'http_5xx': '接口返回服务端错误', 'rate_limited': '接口限流', 'invalid_params': '请求参数被拒绝',
    'unavailable': '接口不可用', 'budget_stop': '访问因预算停止', 'cancelled': '访问被中止',
    'internal_error': '访问处理错误'}


def error_label(code):
    return ERROR_LABELS.get(code.removeprefix('SDK_ACCESS_').lower(), code)


def remember(scope, row, persist_state):
    """One latest witness per request/identity/interface/observer, including failed retries."""
    if row['transport'] != 'sdk_direct' or not row['sent'] or not row['interface_id']:
        return
    identity = row['identity']
    if identity.get('tenant') != scope.tenant or identity.get('observed_id') != scope.observed_id:
        return
    key = (row['incident_id'], *(identity.get(k) for k in IDENTITY),
           row['interface_id'], row['observer_id'])
    query=row['params'].get('query') or ({'run_id':row['params']['run_id']} if row['interface_id']=='probe_observations' and row['params'].get('run_id') else {})
    key=(*key,json.dumps(query,sort_keys=True))
    value = {k: row[k] for k in ('identity', 'incident_id', 'observer_id', 'tool',
        'interface_id', 'attempt_id', 'call_id', 'attempt', 'ok', 'error', 'http_status',
        'elapsed_ms', 'raw_ref')}
    value.update(identity=dict(identity), checked_at=row['finished_at'], persist_state=persist_state,query=query)
    old = scope.sdk_access_checks.get(key)
    if old and timestamp(old['checked_at']) > timestamp(value['checked_at']):
        return
    scope.sdk_access_checks[key] = value
    if len(scope.sdk_access_checks) > MAX_CHECKS:
        oldest = min(scope.sdk_access_checks, key=lambda k: timestamp(scope.sdk_access_checks[k]['checked_at']))
        del scope.sdk_access_checks[oldest]


def capture(snapshot, evidence, scope, incident_id):
    """Project this incident's actual attempts; no additional requests or archive reads."""
    if scope is None:
        return snapshot
    from ..attribution.v2.contracts import SDKAccessCheck
    snapshot.sdk_access_checks = [SDKAccessCheck.model_validate(row) for row in
        sorted(scope.sdk_access_checks.values(), key=lambda r: (r['identity']['service'], r['interface_id'], r['checked_at']))
        if row['incident_id'] == incident_id]
    annotate(snapshot, evidence)
    return snapshot


def annotate(snapshot, evidence):
    """Same boot success shows later access, another boot never proves old boot recovery."""
    from ..diagnostics.evidence import fault
    # For access status retain individual attempts. Cause-proof event readers deliberately
    # collapse retries under one observation_id; that would hide a first timeout here.
    rows = []
    seen = set()
    for item in evidence:
        encoded = next((t.split('=',1)[1] for t in item.tags if t.startswith('diagnostic_event_json=')), None)
        if encoded:
            row = json.loads(encoded)
            if row['kind'] == 'sdk_access' and row['event_id'] not in seen:
                rows.append(row); seen.add(row['event_id'])
    for p in snapshot.problems+snapshot.observability_problems:
        if not p.error_type.startswith('SDK_ACCESS_'):
            continue
        if p.current_state.startswith('sdk_'):
            p.current_state = 'unknown'
            p.current_state_note = '历史样本不能证明此刻仍发生'
        failures = [r for r in rows if r['kind'] == 'sdk_access' and fault(r) and
            fault(r)[0] == p.error_type and r['identity']['service'] == p.service and
            r['identity'].get('observed_id', '') == p.observed_id and
            r['identity']['env'] == p.env and (r['identity']['cluster'] or '') == p.cluster and
            r['identity']['instance'] == p.instance and r['identity']['boot_id'] == p.boot_id and
            'GET ' + INTERFACES.get(r['payload']['interface_id'], '/registered-interface') == p.endpoint]
        if not failures:
            continue
        last = max(failures, key=lambda r: timestamp(r['occurred_at']))
        matches = [c for c in snapshot.sdk_access_checks if
            c.interface_id == last['payload']['interface_id'] and
            c.query == last['payload'].get('sdk_query',{}) and
            c.observer_id == last['payload']['observer_id'] and
            all(c.identity.get(k) == last['identity'].get(k) for k in IDENTITY[:5]) and
            c.checked_at > timestamp(last['occurred_at'])]
        same = [c for c in matches if c.identity['instance'] == p.instance and c.identity['boot_id'] == p.boot_id]
        if same:
            check = max(same, key=lambda c: c.checked_at)
            p.current_state = 'sdk_later_access_succeeded' if check.ok else 'sdk_latest_access_failed'
            p.current_state_note = (f'本轮 {check.checked_at.isoformat()} 同实例/启动、同 SDK 接口访问成功；'
                '保留之前失败，不能据此确认历史形成原因或业务全量恢复' if check.ok else
                f'本轮 {check.checked_at.isoformat()} 同实例/启动、同 SDK 接口仍访问失败：{check.error}；不等于业务宕机')
        elif matches:
            latest = max(matches, key=lambda c: c.checked_at)
            if latest.ok:
                p.current_state = 'sdk_other_boot_access_succeeded'
                p.current_state_note = (f'该失败来自历史实例/启动；本轮 {latest.checked_at.isoformat()} '
                    '另一已登记实例/启动的同 SDK 接口访问成功；不能据此认定原启动恢复或业务正常')
            else:
                p.current_state = 'sdk_other_boot_access_failed'
                p.current_state_note = (f'该失败来自历史实例/启动；本轮 {latest.checked_at.isoformat()} '
                    f'另一已登记实例/启动的同 SDK 接口访问失败：{latest.error}；'
                    '不代表原启动仍失败、两次原因相同或整个业务宕机')


def rows(snapshot):
    out = []
    previous = snapshot.get('source', {}).get('kind') == 'previous_snapshot'
    for c in snapshot.get('sdk_access_checks', []):
        i = c['identity']
        result = '访问成功' if c['ok'] else '访问失败：' + c['error']
        path = INTERFACES.get(c['interface_id'], '/registered-interface')
        status = '' if c['http_status'] is None else f"；HTTP {c['http_status']}"
        out.append({'label': ('留存 SDK 核查 · ' if previous else '本轮 SDK 核查 · ') + i['service'] + ' · GET ' + path,
            'value': f"{result}{status}；核查时刻 {c['checked_at']}；耗时 {c['elapsed_ms']:.1f}ms；"
            f"环境={i['env']}；实例={i['instance']}；启动={i['boot_id']}；观测方={c['observer_id']}；"
            f"尝试={c['attempt_id']}（第 {c['attempt']} 次）；原始记录={c['raw_ref']}；"
            f"留存状态={c['persist_state']}；仅证明该时刻该接口的实际响应，不证明历史原因或整个业务正常"})
    return out


def summary(snapshot):
    checks = snapshot.get('sdk_access_checks', [])
    if not checks:
        return ''
    n = len(checks)
    success = sum(c['ok'] for c in checks)
    prefix = '留存的上轮 SDK 核查' if snapshot.get('source', {}).get('kind') == 'previous_snapshot' else '本轮按需 SDK 核查'
    result = f'{n} 个接口访问均成功' if success == n else f'{n} 个接口访问中 {success} 个成功、{n-success} 个失败'
    return f'{prefix}：{result}；仅说明核查时刻的接口响应，不代表整个业务正常。'

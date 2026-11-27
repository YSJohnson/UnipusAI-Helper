
import re
from collections import Counter, defaultdict


READ_GRADING_JS = """
    const resource = arguments[0], units = arguments[1];
    const done = arguments[arguments.length - 1];
    if (location.origin !== 'https://ucloud.unipus.cn' || !/^\\d+$/.test(resource)) {
        done({units: [], failed: units.length});
        return;
    }
    const read = key => {
        try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; }
        catch (_) { return {}; }
    };
    const legacy = read('__token'), portal = read('PORTAL_STATE_PERSISTENT');
    const jwt = legacy.jwt || portal.Authorization || (portal.tokenInfo || {}).jwt;
    const headers = jwt ? {Authorization: jwt} : {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    const get = async path => {
        const response = await fetch(path, {
            headers, credentials: 'same-origin', signal: controller.signal
        });
        if (!response.ok) throw new Error('records unavailable');
        const body = await response.json();
        if (body.code !== 1 || body.success === false || !body.value)
            throw new Error('records unavailable');
        return body.value;
    };
    (async () => {
        try {
            const {userInfo: user} = await get('/api/account/user/info');
            if (!user || !user.appUserId || !user.ssoId) throw new Error('no user');
            const results = await Promise.all(units.map(async unit => {
                try {
                    const query = new URLSearchParams({
                        id: resource, nodeId: unit,
                        appUserId: user.appUserId, ssoId: user.ssoId
                    });
                    const value = await get(
                        '/api/tla/learningDetail/studyRecord/unitTaskSituation?' + query);
                    if (!Array.isArray(value.list)) throw new Error('no tree');
                    return {unit_id: unit, tree: value.list};
                } catch (_) { return null; }
            }));
            done({units: results.filter(Boolean), failed: results.filter(x => !x).length});
        } catch (_) {
            done({units: [], failed: units.length});
        } finally { clearTimeout(timer); }
    })();
"""


def _clean(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def _path(section):
    return tuple(_clean(part) for part in section.split('/') if _clean(part))


def _is_leaf_node(node):
    if not isinstance(node, dict) or node.get('role') in ('unit', 'section', 'node'):
        return False
    children = node.get('children')
    return not (isinstance(children, list) and children)


def _aggregate_leaf_children(children):
    if not children or not all(_is_leaf_node(child) for child in children):
        return None
    flags = [child.get('scoreTaskFlag') for child in children]
    if any(flag is True for flag in flags):
        scored = True
    elif all(flag is False for flag in flags):
        scored = False
    else:
        scored = None

    weighted_sum = 0.0
    weight_total = 0.0
    simple_scores = []
    scored_scores = []
    for child in children:
        raw_score = child.get('score')
        if type(raw_score) in (int, float) and 0 <= raw_score <= 100:
            val = float(raw_score)
            simple_scores.append(val)
            if child.get('scoreTaskFlag') is True:
                scored_scores.append(val)
                weight = child.get('taskQuesTotalScore')
                if type(weight) in (int, float) and weight > 0:
                    weighted_sum += val * float(weight)
                    weight_total += float(weight)
    if weight_total > 0:
        score = round(weighted_sum / weight_total, 1)
    elif scored_scores:
        score = round(sum(scored_scores) / len(scored_scores), 1)
    elif simple_scores:
        score = round(sum(simple_scores) / len(simple_scores), 1)
    else:
        score = None

    progresses = [child.get('finishProgress') for child in children]
    numeric = [float(p) for p in progresses if type(p) in (int, float)]
    if len(numeric) == len(children) and all(p >= 100 for p in numeric):
        progress = 100
    elif any(p > 0 for p in numeric):
        avg = sum(p for p in numeric) / len(children)
        progress = max(1, min(99, int(round(avg))))
    elif any(p == 0 for p in numeric):
        progress = 0
    else:
        progress = None

    return {'scored': scored, 'score': score, 'progress': progress}


def build_unit_names(units):
    names = {}
    for unit in units:
        unit_id = unit.get('unit_id')
        if not unit_id:
            continue
        for root in unit.get('tree', []):
            if isinstance(root, dict) and root.get('nodeId') == unit_id and root.get('role') == 'unit':
                label = str(root.get('name') or root.get('caption') or '').strip()
                if label:
                    names[unit_id] = re.sub(r'\s+', ' ', label)
                break
    return names


def build_record_index(units):
    candidates = defaultdict(list)
    container_candidates = defaultdict(list)

    def visit(node, unit_id, parents):
        if not isinstance(node, dict):
            return
        name = ' '.join(str(node.get(k) or '').strip() for k in ('caption', 'name')).strip()
        children = node.get('children')
        if isinstance(children, list) and children:
            if node.get('role') == 'node' and name:
                record = _aggregate_leaf_children(children)
                if record is not None:
                    key = (unit_id, tuple(_clean(p) for p in parents), _clean(name))
                    container_candidates[key].append(record)
            for child in children:
                visit(child, unit_id, parents + ([name] if name else []))
        elif node.get('role') not in ('unit', 'section', 'node') and name:
            flag = node.get('scoreTaskFlag')
            score = node.get('score')
            progress = node.get('finishProgress')
            key = (unit_id, tuple(_clean(p) for p in parents), _clean(name))
            candidates[key].append({
                'scored': flag if type(flag) is bool else None,
                'score': round(float(score), 1)
                if type(score) in (int, float) and 0 <= score <= 100 else None,
                'progress': int(progress) if type(progress) in (int, float) else None,
            })

    for unit in units:
        unit_id = unit.get('unit_id')
        if not unit_id:
            continue
        for root in unit.get('tree', []):
            if isinstance(root, dict) and root.get('nodeId') == unit_id and root.get('role') == 'unit':
                for child in root.get('children', []):
                    visit(child, unit_id, [])
    merged = {key: records[0] if len(records) == 1 else None
              for key, records in candidates.items()}
    for key, records in container_candidates.items():
        if key not in merged:
            merged[key] = records[0] if len(records) == 1 else None
    return merged


def build_grading_index(units):
    return {key: record['scored']
            for key, record in build_record_index(units).items()
            if record is not None}


def score_text(score):
    return f"{score:g}" if isinstance(score, (int, float)) else ""


def task_scoring(index, unit_id, section, name):
    return index.get((unit_id, _path(section), _clean(name)))


def _task_keys(tasks):
    keys = [(t.get('_unit_id'), _path(t.get('section') or ''), _clean(t.get('l1_title')))
            for t in tasks]
    return keys, Counter(keys)


def assign_task_scoring(tasks, index):
    keys, counts = _task_keys(tasks)
    for task, key in zip(tasks, keys):
        task['is_scored'] = index.get(key) if key[0] and counts[key] == 1 else None


def assign_task_records(tasks, index):
    keys, counts = _task_keys(tasks)
    for task, key in zip(tasks, keys):
        record = index.get(key) if key[0] and counts[key] == 1 else None
        task['is_scored'] = record['scored'] if record else None
        task['score'] = record['score'] if record else None
        task['progress'] = record['progress'] if record else None
        if task.get('status') == 'unknown' and isinstance(task['progress'], int):
            status_text = (task.get('status_text') or '').strip()
            is_placeholder = not status_text or bool(
                re.fullmatch(r'已锁定|加载中\.{0,3}|loading\.{0,3}|locked', status_text, re.I)
            )
            if task['progress'] >= 100:
                task['status'] = 'done'
                if is_placeholder:
                    task['status_text'] = '已完成'
            elif task['progress'] > 0:
                task['status'] = 'doing'
                if is_placeholder:
                    task['status_text'] = '进行中'


def grading_text(scored):
    return '计分' if scored is True else '不计分' if scored is False else '计分未知'


def selectable_task(task, exclude_ungraded=False):
    return not (exclude_ungraded and task.get('is_scored') is False)

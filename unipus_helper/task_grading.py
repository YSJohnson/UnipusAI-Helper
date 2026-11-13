"""Read the portal's explicit scoring flags without opening or submitting tasks."""

import re
from collections import Counter, defaultdict


# Keep credentials in the browser. Only the requested units' record trees leave it.
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


def build_record_index(units):
    """Index unique full task paths; repeated names alone cannot identify a task."""
    candidates = defaultdict(list)

    def visit(node, unit_id, parents):
        if not isinstance(node, dict):
            return
        name = ' '.join(str(node.get(k) or '').strip() for k in ('caption', 'name')).strip()
        children = node.get('children')
        if isinstance(children, list) and children:
            for child in children:
                visit(child, unit_id, parents + ([name] if name else []))
        elif node.get('role') not in ('unit', 'section', 'node') and name:
            flag = node.get('scoreTaskFlag')
            # A missing flag (or a changed API type) must never mean "ungraded".
            score = node.get('score')
            progress = node.get('finishProgress')
            key = (unit_id, tuple(_clean(p) for p in parents), _clean(name))
            candidates[key].append({
                'scored': flag if type(flag) is bool else None,
                # 还没做过的任务接口里没有 score，这时保持 None，不要当成 0 分
                'score': round(float(score), 1)
                if type(score) in (int, float) and 0 <= score <= 100 else None,
                'progress': int(progress) if type(progress) in (int, float) else None,
            })

    for unit in units:
        unit_id = unit.get('unit_id')
        if not unit_id:
            continue
        for root in unit.get('tree', []):
            # Ignore data for other units, even if names and sections coincide.
            if isinstance(root, dict) and root.get('nodeId') == unit_id and root.get('role') == 'unit':
                for child in root.get('children', []):
                    visit(child, unit_id, [])
    return {key: records[0] if len(records) == 1 else None
            for key, records in candidates.items()}


def build_grading_index(units):
    return {key: record['scored'] for key, record in build_record_index(units).items()}


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
    """把学习记录里的计分标记、已有分数、完成度写回任务。"""
    keys, counts = _task_keys(tasks)
    for task, key in zip(tasks, keys):
        record = index.get(key) if key[0] and counts[key] == 1 else None
        task['is_scored'] = record['scored'] if record else None
        task['score'] = record['score'] if record else None
        task['progress'] = record['progress'] if record else None


def grading_text(scored):
    return '计分' if scored is True else '不计分' if scored is False else '计分未知'


def selectable_task(task, exclude_ungraded=False):
    return not (exclude_ungraded and task.get('is_scored') is False)

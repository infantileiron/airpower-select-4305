"""累计电表读数 → 小时耗电的纯口径逻辑。

只依赖标准库，不导入任何 Web/ORM 框架，保证可在最小环境下用单元测试固定口径。

口径：
- 仪表导出的是整点“累计电量”读数，小时 H 的耗电 = reading(H) - reading(H-1)，归后一个小时 H。
- 缺任一端点（H-1 或 H）时该小时待补，不做任何摊平或插值。
- 同 (hour, revision) 同值视为幂等重复；同号异值整批拒绝。
- 低修订号到达时忽略（台账仍留痕），高修订号替换后重算受影响的相邻小时。
- 相邻整点之差为负（累计表倒退）属于整批失败，不写入任何小时样本。
"""
import hashlib
import json
import math
from datetime import datetime, timedelta

# 工况字段沿用小时样本的物理量纲与边界
LIMITS = {
    'pressure': (0.0, 20.0),
    'flow': (0.0, 1000.0),
    'temperature': (-30.0, 100.0),
    'sensor_noise': (-100.0, 100.0),
    'reading': (0.0, 1_000_000_000.0),
}
MACHINE_MIN, MACHINE_MAX = 0, 20
CONDITION_FIELDS = ('pressure', 'flow', 'temperature', 'machines', 'sensor_noise')
READING_FIELDS = CONDITION_FIELDS + ('reading',)


class MeterValidationError(ValueError):
    """整批拒绝：携带面向页面的原因。"""


def canonical_hour(value):
    """归一为工厂本地整点 'YYYY-MM-DDTHH:00'，拒绝时区偏移与非整点。"""
    if not isinstance(value, str):
        raise MeterValidationError('小时必须为 ISO 字符串')
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise MeterValidationError(f'小时无法解析：{value}')
    if d.tzinfo is not None or d.minute or d.second or d.microsecond:
        raise MeterValidationError('使用工厂本地整点小时，不带时区偏移且分钟为零')
    return d.strftime('%Y-%m-%dT%H:00')


def _check_finite(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MeterValidationError(f'{name} 必须为有限数值')


def validate_row(row, index):
    """校验并归一单条读数；任何坏数据都抛出整批拒绝。"""
    if not isinstance(row, dict):
        raise MeterValidationError(f'第 {index + 1} 行不是对象')
    hour = canonical_hour(row.get('hour'))
    revision = row.get('revision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise MeterValidationError(f'{hour} 修订号必须为不小于 1 的整数')
    normalized = {'hour': hour, 'revision': revision}
    for name in ('pressure', 'flow', 'temperature', 'sensor_noise', 'reading'):
        if name not in row:
            raise MeterValidationError(f'{hour} 缺少字段 {name}')
        value = row[name]
        _check_finite(name, value)
        lo, hi = LIMITS[name]
        if not (lo <= float(value) <= hi):
            raise MeterValidationError(f'{hour} 的 {name}={value} 超出允许范围 [{lo}, {hi}]')
        normalized[name] = float(value)
    machines = row.get('machines')
    if isinstance(machines, bool) or not isinstance(machines, int) or not (MACHINE_MIN <= machines <= MACHINE_MAX):
        raise MeterValidationError(f'{hour} 开机台数必须为 {MACHINE_MIN}-{MACHINE_MAX} 的整数')
    normalized['machines'] = machines
    return normalized


def validate_batch(rows):
    """整批校验：空批、坏数据、批内同号异值均拒绝；返回归一化行列表。"""
    if not isinstance(rows, list) or not rows:
        raise MeterValidationError('批次必须为非空读数数组')
    normalized = [validate_row(row, i) for i, row in enumerate(rows)]
    seen = {}
    for r in normalized:
        key = (r['hour'], r['revision'])
        if key in seen:
            if any(seen[key][f] != r[f] for f in READING_FIELDS):
                raise MeterValidationError(f'{r["hour"]} 修订号 {r["revision"]} 同号异值，整批拒绝')
        else:
            seen[key] = r
    return normalized


def content_hash(rows):
    """对归一化后的批载荷做稳定指纹，用于同 batch_ref 不同内容的识别。"""
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def dedupe_within_batch(rows):
    """批内同 (hour,revision) 同值折叠为一条；返回去重后的行。"""
    unique = {}
    for r in rows:
        unique[(r['hour'], r['revision'])] = r
    return list(unique.values())


def merge_readings(stored, incoming):
    """把批内读数并入按小时组织的存量台账。

    stored/incoming 均为 dict[hour] -> dict(revision -> row)。
    返回 (merged, new_keys, replaced_keys, ignored_keys)：
    - new_keys      存量没有的 (hour, revision)
    - replaced_keys 同小时出现更高修订号（含该小时首次出现）
    - ignored_keys  同号同值的幂等重复，或低于存量最高修订的迟到旧修订
    同号异值在 validate_batch 与调用方的存量比对阶段拦截，此处不处理。
    """
    merged = {h: dict(revs) for h, revs in stored.items()}
    new_keys, replaced_keys, ignored_keys = [], [], []
    for r in incoming:
        h, rev = r['hour'], r['revision']
        bucket = merged.setdefault(h, {})
        if rev in bucket:
            ignored_keys.append((h, rev))  # 同号同值，幂等重复
            continue
        new_keys.append((h, rev))
        if not bucket or rev > max(bucket):
            replaced_keys.append(h)
        else:
            ignored_keys.append((h, rev))  # 旧修订迟到，留痕但不参与推导
        bucket[rev] = r
    return merged, new_keys, replaced_keys, ignored_keys


def winners(series):
    """series: dict[hour] -> dict[revision -> row]，返回每小时最高修订读数。"""
    return {h: revs[max(revs)] for h, revs in series.items() if revs}


def assert_non_negative(win):
    """对所有相邻整点最高修订读数做负差检查；存在负差即整批失败。"""
    hours = sorted(win)
    for a, b in zip(hours, hours[1:]):
        ha, hb = datetime.fromisoformat(a), datetime.fromisoformat(b)
        if hb - ha == timedelta(hours=1) and win[b]['reading'] < win[a]['reading']:
            raise MeterValidationError(
                f'{b} 累计读数 {win[b]["reading"]} 小于前一整点 {a} 的 {win[a]["reading"]}，负差整批拒绝'
            )


def affected_sample_hours(changed_hours, winner_hours):
    """读数变化影响的小时样本：变化整点 H 自身，以及后一个小时 H+1（其起点为 H）。

    仅当该小时在最高修订序列里存在读数（终点可用）时才可能产出样本。
    """
    out = set()
    for h in changed_hours:
        if h in winner_hours:
            out.add(h)
        nxt = (datetime.fromisoformat(h) + timedelta(hours=1)).strftime('%Y-%m-%dT%H:00')
        if nxt in winner_hours:
            out.add(nxt)
    return out


def derive_sample(hour, win):
    """按口径推导单个小时样本；缺端点返回 None（待补，不摊平）。"""
    cur = win.get(hour)
    if cur is None:
        return None
    prev_hour = (datetime.fromisoformat(hour) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:00')
    prev = win.get(prev_hour)
    if prev is None:
        return None  # 起点读数缺失，该小时待补
    kwh = cur['reading'] - prev['reading']
    if kwh < 0:
        raise MeterValidationError(f'{hour} 相邻整点之差为负（{kwh}），整批拒绝')
    return {
        'hour': hour,
        'kwh': kwh,
        'pressure': cur['pressure'],
        'flow': cur['flow'],
        'temperature': cur['temperature'],
        'machines': cur['machines'],
        'sensor_noise': cur['sensor_noise'],
        'meter_revision': cur['revision'],
        'from_hour': prev_hour,
        'from_reading': prev['reading'],
        'to_reading': cur['reading'],
        'from_revision': prev['revision'],
    }


def derive_samples(hours, win):
    """批量推导；返回 (samples, pending_hours)，负差抛整批拒绝。"""
    samples, pending = [], []
    for hour in sorted(hours):
        sample = derive_sample(hour, win)
        if sample is None:
            pending.append(hour)
        else:
            samples.append(sample)
    return samples, pending

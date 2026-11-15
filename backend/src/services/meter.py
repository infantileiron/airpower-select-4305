"""累计读数批次接入的编排层：幂等、修订取舍、相邻小时重算与批次状态。

仓储通过构造函数注入（SqlMeterRepository 或测试用内存仓储），
本层不直接依赖 SQLAlchemy，事务边界由路由/调用方持有。
"""
import json
from datetime import datetime, timedelta

from src.services import meter_logic as ml
from src.services.meter_logic import MeterValidationError


def raw_fingerprint(rows):
    try:
        import hashlib
        return hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
        ).hexdigest()
    except TypeError:
        return None


def _batch_hours(batch):
    hours = set()
    for r in (batch.payload or []):
        if isinstance(r, dict) and r.get('hour'):
            try:
                hours.add(ml.canonical_hour(r['hour']))
            except MeterValidationError:
                continue
    return hours


def _is_complete(batch, win, backfill=False):
    """批次成功条件：
    - 批内除最早整点外，每个读数整点的前一整点都已在台账（内部缺口不摊平）；
    - 最早整点缺前序读数时，仅显式 backfill（补端点批次）可成功，否则保持待补，
      等迟到的前序读数到达后自动转成功。
    """
    hours = sorted(_batch_hours(batch))
    if not hours:
        return False
    first = hours[0]
    for h in hours:
        if h == first:
            continue
        prev = (datetime.fromisoformat(h) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:00')
        if prev not in win:
            return False
    first_prev = (datetime.fromisoformat(first) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:00')
    if first_prev not in win and not backfill:
        return False
    return True


def _summary(batch, replayed=False, **extra):
    data = {
        'id': getattr(batch, 'id', None),
        'batch_ref': batch.batch_ref,
        'status': batch.status,
        'reason': batch.reason,
        'affected_hours': batch.affected_hours or [],
        'replayed': replayed,
    }
    data.update(extra)
    return data


def ingest(repo, batch_ref, raw_rows, backfill=False):
    """接入一个批次。成功/拒绝都返回摘要；拒绝原因写入批次台账。

    backfill=True 表示本批为补端点批次：其最早整点允许没有前序读数。
    调用方需在外层事务中调用；本函数抛出的唯一异常是程序错误，
    业务校验失败一律落为 status='rejected' 的批次记录。
    """
    if not isinstance(batch_ref, str) or not batch_ref.strip():
        raise MeterValidationError('批次引用 batch_ref 不能为空')
    batch_ref = batch_ref.strip()
    fingerprint = raw_fingerprint(raw_rows)

    existing = repo.get_batch(batch_ref)
    if existing is not None:
        # 幂等重试：同引用同内容直接回放旧结果，不重复写入任何读数或样本
        if fingerprint is not None and existing.content_hash != fingerprint:
            raise MeterValidationError(
                f'批次引用 {batch_ref} 已存在且内容不同，拒绝覆盖；请使用新的批次引用'
            )
        return _summary(existing, replayed=True)

    try:
        normalized = ml.dedupe_within_batch(ml.validate_batch(raw_rows))
        stored_series = repo.series()
        # 与存量台账比对：同 (hour,revision) 异值 → 整批拒绝
        for r in normalized:
            old = stored_series.get(r['hour'], {}).get(r['revision'])
            if old is not None and any(old[f] != r[f] for f in ml.READING_FIELDS):
                raise MeterValidationError(
                    f'{r["hour"]} 修订号 {r["revision"]} 与存量同号异值，整批拒绝'
                )
        merged, new_keys, replaced_hours, ignored_keys = ml.merge_readings(stored_series, normalized)
        win = ml.winners(merged)
        # 全量相邻整点负差检查：失败则保留旧版，读数与样本均不切换
        ml.assert_non_negative(win)
        affected = sorted(ml.affected_sample_hours(set(replaced_hours), set(win)))
        samples, pending = ml.derive_samples(affected, win)
        manual = repo.manual_hours(set(affected))

        batch = repo.add_batch(
            batch_ref=batch_ref, status='pending', reason='',
            content_hash=fingerprint, payload=raw_rows, affected_hours=affected,
            backfill=backfill,
        )
        # 追加读数台账（低修订同样留痕，推导时只取最高修订）
        for r in normalized:
            if (r['hour'], r['revision']) not in ignored_keys:
                repo.add_reading(r, batch.id)
        # 一次性切换受影响小时样本；人工样本不被覆盖。
        # derive_samples 已把缺端点的待补小时排除在 samples 之外：
        # 这些小时若存在旧的仪表样本则下线（回到待补），人工样本保留，缺口不摊平。
        derived_hours = {sample['hour'] for sample in samples}
        written, removed = [], []
        for hour in affected:
            if hour in manual:
                continue
            if hour in derived_hours:
                sample = next(x for x in samples if x['hour'] == hour)
                repo.upsert_meter_sample(sample, batch.id)
                written.append(hour)
            elif hour in set(pending):
                if repo.delete_meter_sample(hour):
                    removed.append(hour)

        # 刷新所有待补批次：迟到端点补齐后，待补批次可转成功；
        # 显式补端点批次只要内部连续即成功
        refreshed = []
        seen_batches = set()
        for pending_batch in [batch] + repo.pending_batches():
            if pending_batch.id in seen_batches:
                continue
            seen_batches.add(pending_batch.id)
            is_backfill = bool(getattr(pending_batch, 'backfill', 0))
            if _is_complete(pending_batch, win, backfill=is_backfill):
                repo.update_batch_status(pending_batch, 'success')
                refreshed.append(pending_batch.batch_ref)
        return _summary(
            batch,
            new_readings=[{'hour': h, 'revision': rev} for h, rev in new_keys],
            ignored=[{'hour': h, 'revision': rev} for h, rev in ignored_keys],
            written_hours=written,
            removed_hours=removed,
            pending_hours=pending,
            manual_protected=sorted(manual),
            refreshed_batches=refreshed,
        )
    except MeterValidationError as exc:
        # 整批拒绝：仅落批次记录，读数与小时样本保持旧版
        batch = repo.add_batch(
            batch_ref=batch_ref, status='rejected', reason=str(exc),
            content_hash=fingerprint, payload=raw_rows, affected_hours=[],
            backfill=backfill,
        )
        return _summary(batch)

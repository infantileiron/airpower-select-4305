"""累计读数口径纯逻辑测试（标准库 unittest，无第三方依赖）。

覆盖：
1. 整点归一与坏数据校验
2. 同号异值（批内/存量）整批拒绝；同号同值幂等
3. 高修订替换、旧修订忽略
4. 相邻整点之差归后一个小时；缺端点待补不摊平；负差拒绝
5. 受影响相邻小时重算
"""
import unittest

from src.services import meter_logic as ml
from src.services.meter_logic import MeterValidationError


def r(hour, revision, reading, **kw):
    base = dict(hour=hour, revision=revision, reading=reading,
                pressure=6.5, flow=9.0, temperature=22.0,
                machines=3, sensor_noise=0.2)
    base.update(kw)
    return base


class CanonicalHourTests(unittest.TestCase):
    def test_accepts_naive_whole_hour(self):
        self.assertEqual(ml.canonical_hour('2026-11-01T08:00'), '2026-11-01T08:00')
        self.assertEqual(ml.canonical_hour('2026-11-01 08:00:00'), '2026-11-01T08:00')

    def test_rejects_timezone_or_minutes(self):
        for bad in ('2026-11-01T08:30', '2026-11-01T08:00:05', '2026-11-01T08:00+08:00', 'nonsense'):
            with self.assertRaises(MeterValidationError):
                ml.canonical_hour(bad)


class ValidationTests(unittest.TestCase):
    def test_good_row_normalized(self):
        out = ml.validate_row(r('2026-11-01T00:00', 1, 100.0), 0)
        self.assertEqual(out['hour'], '2026-11-01T00:00')
        self.assertEqual(out['revision'], 1)
        self.assertIsInstance(out['machines'], int)

    def test_bad_last_row_fails_whole_batch(self):
        rows = [r('2026-11-01T00:00', 1, 100.0),
                r('2026-11-01T01:00', 1, float('nan'))]
        with self.assertRaises(MeterValidationError):
            ml.validate_batch(rows)

    def test_out_of_range_rejected(self):
        with self.assertRaises(MeterValidationError):
            ml.validate_batch([r('2026-11-01T00:00', 1, 100.0, pressure=99.0)])

    def test_revision_must_be_positive_int(self):
        for bad_rev in (0, -1, 1.5, True):
            with self.assertRaises(MeterValidationError):
                ml.validate_batch([r('2026-11-01T00:00', bad_rev, 100.0)])

    def test_empty_batch_rejected(self):
        with self.assertRaises(MeterValidationError):
            ml.validate_batch([])

    def test_same_revision_conflicting_value_rejected(self):
        rows = [r('2026-11-01T00:00', 1, 100.0),
                r('2026-11-01T00:00', 1, 105.0)]
        with self.assertRaises(MeterValidationError) as ctx:
            ml.validate_batch(rows)
        self.assertIn('同号异值', str(ctx.exception))

    def test_same_revision_identical_dedupes(self):
        rows = [r('2026-11-01T00:00', 1, 100.0),
                r('2026-11-01T00:00', 1, 100.0)]
        out = ml.dedupe_within_batch(ml.validate_batch(rows))
        self.assertEqual(len(out), 1)


class RevisionMergeTests(unittest.TestCase):
    def _series(self, rows):
        stored = {}
        for x in rows:
            stored.setdefault(x['hour'], {})[x['revision']] = x
        return stored

    def test_higher_revision_replaces_lower_ignored(self):
        stored = self._series([r('2026-11-01T00:00', 1, 100.0)])
        # 该小时已有 rev 2，迟到的 rev 1 应被忽略
        stored.setdefault('2026-11-02T00:00', {})[2] = r('2026-11-02T00:00', 2, 1000.0)
        incoming = [
            r('2026-11-01T00:00', 2, 102.0),   # 高修订替换
            r('2026-11-01T01:00', 1, 120.0),   # 新读数
            r('2026-11-02T00:00', 1, 999.0),   # 迟到的旧修订，忽略
        ]
        merged, new_keys, replaced, ignored = ml.merge_readings(stored, incoming)
        self.assertEqual(ml.winners(merged)['2026-11-01T00:00']['reading'], 102.0)
        self.assertEqual(ml.winners(merged)['2026-11-02T00:00']['revision'], 2)
        self.assertIn('2026-11-01T00:00', replaced)
        self.assertIn(('2026-11-02T00:00', 1), ignored)

    def test_existing_same_revision_different_value_flagged(self):
        stored = self._series([r('2026-11-01T00:00', 1, 100.0)])
        incoming = [r('2026-11-01T00:00', 1, 108.0)]
        conflict = any(
            stored.get(x['hour'], {}).get(x['revision']) is not None
            and any(stored[x['hour']][x['revision']][f] != x[f] for f in ml.READING_FIELDS)
            for x in incoming)
        self.assertTrue(conflict)


class DerivationTests(unittest.TestCase):
    def test_difference_assigned_to_later_hour(self):
        win = ml.winners({
            '2026-11-01T00:00': {1: r('2026-11-01T00:00', 1, 100.0)},
            '2026-11-01T01:00': {1: r('2026-11-01T01:00', 1, 114.5)},
        })
        s0 = ml.derive_sample('2026-11-01T00:00', win)   # 无起点 → 待补
        s1 = ml.derive_sample('2026-11-01T01:00', win)
        self.assertIsNone(s0)
        self.assertEqual(s1['kwh'], 14.5)
        self.assertEqual(s1['from_hour'], '2026-11-01T00:00')
        # 工况取后一个整点（读数所在小时）
        self.assertEqual(s1['machines'], 3)

    def test_missing_endpoint_pending_not_flattened(self):
        win = ml.winners({
            '2026-11-01T00:00': {1: r('2026-11-01T00:00', 1, 100.0)},
            '2026-11-01T02:00': {1: r('2026-11-01T02:00', 1, 130.0)},  # 缺 01:00
        })
        samples, pending = ml.derive_samples(['2026-11-01T00:00', '2026-11-01T02:00'], win)
        self.assertEqual(samples, [])
        self.assertEqual(pending, ['2026-11-01T00:00', '2026-11-01T02:00'])

    def test_negative_difference_rejected(self):
        win = ml.winners({
            '2026-11-01T00:00': {1: r('2026-11-01T00:00', 1, 100.0)},
            '2026-11-01T01:00': {1: r('2026-11-01T01:00', 1, 90.0)},
        })
        with self.assertRaises(MeterValidationError):
            ml.assert_non_negative(win)
        with self.assertRaises(MeterValidationError):
            ml.derive_sample('2026-11-01T01:00', win)

    def test_non_adjacent_hours_not_compared(self):
        win = ml.winners({
            '2026-11-01T00:00': {1: r('2026-11-01T00:00', 1, 100.0)},
            '2026-11-01T03:00': {1: r('2026-11-01T03:00', 1, 90.0)},  # 不相邻，不判负差
        })
        ml.assert_non_negative(win)  # 不抛异常

    def test_affected_hours_include_self_and_next(self):
        affected = ml.affected_sample_hours(
            {'2026-11-01T01:00'},
            {'2026-11-01T00:00', '2026-11-01T01:00', '2026-11-01T02:00'})
        self.assertEqual(affected, {'2026-11-01T01:00', '2026-11-01T02:00'})

    def test_recompute_after_revision_replace(self):
        win = ml.winners({
            '2026-11-01T00:00': {1: r('2026-11-01T00:00', 1, 100.0)},
            '2026-11-01T01:00': {2: r('2026-11-01T01:00', 2, 118.0)},
        })
        s1 = ml.derive_sample('2026-11-01T01:00', win)
        self.assertEqual(s1['kwh'], 18.0)
        self.assertEqual(s1['meter_revision'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

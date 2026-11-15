"""仅依赖标准库的测试夹具：内存仓储，行为对齐 SqlMeterRepository。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import meter_logic as ml  # noqa: E402


class FakeBatch:
    _seq = 0

    def __init__(self, batch_ref, status, reason, content_hash, payload, affected_hours, backfill=False):
        FakeBatch._seq += 1
        self.id = FakeBatch._seq
        self.batch_ref = batch_ref
        self.status = status
        self.reason = reason
        self.content_hash = content_hash
        self.payload = payload
        self.affected_hours = affected_hours
        self.backfill = 1 if backfill else 0


class FakeSample:
    _seq = 0

    def __init__(self, **values):
        FakeSample._seq += 1
        self.id = FakeSample._seq
        for k in ('hour', 'pressure', 'flow', 'temperature', 'machines',
                  'sensor_noise', 'kwh', 'note', 'source',
                  'source_batch_id', 'meter_revision'):
            setattr(self, k, values.get(k))


class FakeMeterRepository:
    def __init__(self):
        self.batches = []
        self.readings = []          # 追加台账行
        self.samples = {}           # hour -> FakeSample

    # ---- 与 SqlMeterRepository 同形的接口 ----
    def get_batch(self, batch_ref):
        return next((b for b in self.batches if b.batch_ref == batch_ref), None)

    def add_batch(self, batch_ref, status, reason, content_hash, payload, affected_hours, backfill=False):
        b = FakeBatch(batch_ref, status, reason, content_hash, payload, affected_hours, backfill)
        self.batches.append(b)
        return b

    def all_reading_rows(self):
        return list(self.readings)

    def series(self):
        series = {}
        for r in self.readings:
            series.setdefault(r['hour'], {})[r['revision']] = dict(r)
        return series

    def existing_reading(self, hour, revision):
        return next((r for r in self.readings
                     if r['hour'] == hour and r['revision'] == revision), None)

    def add_reading(self, row, batch_id):
        self.readings.append({**row, 'batch_id': batch_id})

    def manual_hours(self, hours):
        return {h for h in hours
                if h in self.samples and self.samples[h].source == 'manual'}

    def get_sample(self, hour):
        return self.samples.get(hour)

    def upsert_meter_sample(self, sample, batch_id):
        row = self.samples.get(sample['hour'])
        values = {k: sample[k] for k in
                  ('hour', 'pressure', 'flow', 'temperature', 'machines',
                   'sensor_noise', 'kwh', 'meter_revision')}
        values.update(source='meter', source_batch_id=batch_id, note='')
        if row is None:
            self.samples[sample['hour']] = FakeSample(**values)
        elif row.source != 'manual':
            for k, v in values.items():
                setattr(row, k, v)

    def delete_meter_sample(self, hour):
        row = self.samples.get(hour)
        if row is not None and row.source != 'manual':
            del self.samples[hour]
            return True
        return False

    def pending_batches(self):
        return [b for b in self.batches if b.status == 'pending']

    def update_batch_status(self, batch, status, reason='', affected_hours=None):
        batch.status = status
        batch.reason = reason
        if affected_hours is not None:
            batch.affected_hours = affected_hours

    def list_batches(self, status=None):
        rows = self.batches if status is None else [b for b in self.batches if b.status == status]
        return sorted(rows, key=lambda b: b.id, reverse=True)

    def meter_hours_view(self):
        win = ml.winners(self.series())
        view = []
        for hour in sorted(win):
            sample = self.get_sample(hour)
            derived = ml.derive_sample(hour, win)
            cur = win[hour]
            view.append({
                'hour': hour,
                'reading': cur['reading'],
                'revision': cur['revision'],
                'sample_state': ('manual_protected' if sample is not None and sample.source == 'manual'
                                 else ('pending' if derived is None else 'derived')),
                'kwh': None if derived is None else derived['kwh'],
                'from_hour': None if derived is None else derived['from_hour'],
                'from_reading': None if derived is None else derived['from_reading'],
                'to_reading': cur['reading'],
                'from_revision': None if derived is None else derived['from_revision'],
                'source_batch_id': None if sample is None else sample.source_batch_id,
                'sample_id': None if sample is None else sample.id,
            })
        return view

    # ---- 测试辅助 ----
    def add_manual_sample(self, hour, kwh=100.0):
        self.samples[hour] = FakeSample(
            hour=hour, pressure=6.0, flow=9.0, temperature=22.0, machines=3,
            sensor_noise=0.1, kwh=kwh, note='人工', source='manual',
            source_batch_id=None, meter_revision=None)


def row(hour, revision, reading, **kw):
    base = dict(hour=hour, revision=revision, reading=reading,
                pressure=6.5, flow=9.0, temperature=22.0,
                machines=3, sensor_noise=0.2)
    base.update(kw)
    return base

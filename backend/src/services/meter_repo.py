"""累计读数管线的持久化仓储（SQLAlchemy 实现）。"""
from src.models.entities import MeterBatch, MeterReading, Sample
from src.services import meter_logic as ml


def series_from_rows(rows):
    series = {}
    for r in rows:
        series.setdefault(r.hour, {})[r.revision] = {
            'hour': r.hour, 'revision': r.revision, 'reading': r.reading,
            'pressure': r.pressure, 'flow': r.flow, 'temperature': r.temperature,
            'machines': r.machines, 'sensor_noise': r.sensor_noise,
        }
    return series


class SqlMeterRepository:
    def __init__(self, session):
        self.s = session

    def get_batch(self, batch_ref):
        return self.s.query(MeterBatch).filter_by(batch_ref=batch_ref).first()

    def add_batch(self, batch_ref, status, reason, content_hash, payload, affected_hours, backfill=False):
        batch = MeterBatch(batch_ref=batch_ref, status=status, reason=reason,
                           content_hash=content_hash, payload=payload,
                           affected_hours=affected_hours, backfill=1 if backfill else 0)
        self.s.add(batch)
        self.s.flush()
        return batch

    def all_reading_rows(self):
        return self.s.query(MeterReading).order_by(MeterReading.hour, MeterReading.revision).all()

    def series(self):
        return series_from_rows(self.all_reading_rows())

    def existing_reading(self, hour, revision):
        return self.s.query(MeterReading).filter_by(hour=hour, revision=revision).first()

    def add_reading(self, row, batch_id):
        self.s.add(MeterReading(hour=row['hour'], revision=row['revision'], reading=row['reading'],
                                pressure=row['pressure'], flow=row['flow'],
                                temperature=row['temperature'], machines=row['machines'],
                                sensor_noise=row['sensor_noise'], batch_id=batch_id))

    def manual_hours(self, hours):
        if not hours:
            return set()
        rows = self.s.query(Sample.hour).filter(Sample.hour.in_(list(hours)),
                                                Sample.source == 'manual').all()
        return {r[0] for r in rows}

    def get_sample(self, hour):
        return self.s.query(Sample).filter_by(hour=hour).first()

    def upsert_meter_sample(self, sample, batch_id):
        row = self.get_sample(sample['hour'])
        values = {k: sample[k] for k in
                  ('hour', 'pressure', 'flow', 'temperature', 'machines', 'sensor_noise', 'kwh', 'meter_revision')}
        values['source'] = 'meter'
        values['source_batch_id'] = batch_id
        if row is None:
            self.s.add(Sample(note='', **values))
        elif row.source != 'manual':
            for k, v in values.items():
                setattr(row, k, v)

    def delete_meter_sample(self, hour):
        row = self.get_sample(hour)
        if row is not None and row.source != 'manual':
            self.s.delete(row)
            return True
        return False

    def pending_batches(self):
        return self.s.query(MeterBatch).filter_by(status='pending').all()

    def update_batch_status(self, batch, status, reason='', affected_hours=None):
        batch.status = status
        batch.reason = reason
        if affected_hours is not None:
            batch.affected_hours = affected_hours

    def list_batches(self, status=None):
        q = self.s.query(MeterBatch).order_by(MeterBatch.id.desc())
        if status:
            q = q.filter_by(status=status)
        return q.all()

    def meter_hours_view(self):
        """逐小时来源视图：读数台账 + 样本推导状态。"""
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

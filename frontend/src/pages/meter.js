import {state} from '../stores/session.js';
import {table} from '../components/common/table.js';
import {field} from '../components/common/fields.js';
import {BATCH_STATUS} from '../constants/enums.js';

const STATE_LABEL = {derived: '已推导', pending: '待补端点', manual_protected: '人工保护'};

export function render(){
 const batches=state.meterBatches||[];
 const hours=state.meterHours||[];
 return `<section class="panel">
 <h2>累计读数批次接入</h2>
 <p class="notice">仪表导出的是整点累计电量。小时 H 的耗电 = 读数(H) − 读数(H−1)，归后一个小时 H；缺任一端点的小时保持待补，不摊平缺口。同号异值整批拒绝，高修订替换后重算受影响相邻小时。</p>
 <div class="split">
 <div>
 <h3>批次台账（待补 / 成功 / 拒绝）</h3>
 ${table(
   ['批次引用','状态','补端点','影响小时','原因 / 刷新'],
   batches.map(b=>[
     b.batch_ref,
     BATCH_STATUS[b.status]||b.status,
     b.backfill?'是':'否',
     (b.affected_hours||[]).length,
     b.reason||(b.status==='pending'?'等待迟到端点':'—'),
   ]))}
 <h3>逐小时来源（读数台账 → 小时耗电）</h3>
 ${table(
   ['本地小时','累计读数','修订','样本状态','耗电kWh','起点小时','起→止读数','来源批次'],
   hours.map(h=>[
     h.hour,h.reading,h.revision,STATE_LABEL[h.sample_state]||h.sample_state,
     h.kwh===null?'待补':h.kwh,
     h.from_hour||'—',
     h.from_reading===null?'—':`${h.from_reading} → ${h.to_reading}`,
     h.source_batch_id?('#'+h.source_batch_id):'—',
   ]))}
 </div>
 <aside class="side">
 <h2>接入读数批次</h2>
 <form id="meter">
 ${field('批次引用 batch_ref','batch_ref','text','required placeholder="如 ZX-A01-20261111-am"')}
 <label class="check"><input type="checkbox" name="backfill"> 补端点批次（最早整点允许无前序读数）</label>
 <label>读数 JSON（hour/revision/reading + 当时工况）<textarea name="readings" rows="14" required placeholder='[{"hour":"2026-11-11T00:00","revision":1,"reading":1200.5,"pressure":6.7,"flow":9,"temperature":22,"machines":4,"sensor_noise":0}]'></textarea></label>
 <button class="primary">提交批次</button>
 </form>
 <p class="eyebrow">重试请使用同一 batch_ref 与相同内容；更换内容需新引用。人工录入/修订的小时不会被批次覆盖。</p>
 </aside>
 </div>
 </section>`;
}

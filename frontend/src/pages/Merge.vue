<template>
  <div>
    <h1>同品同到期日合并</h1>
    <p class="muted">
      预览不改全层条数与余量；确认后只留一条（id 最小），余量为合并前之和。
      到期日不同、脏批与 clean 批隔离资格不一致都会整单失败并回到合并前。
    </p>

    <section v-if="groups.length" class="card">
      <h3>可合并候选（同品 · 同到期日 · 同隔离品质）</h3>
      <div v-for="g in groups" :key="g.item_id + '-' + g.expiry + '-' + g.data_quality" class="group">
        <span class="lot">
          {{ g.name }} · {{ g.expiry }}
          <em :class="g.data_quality">{{ g.data_quality === 'dirty' ? '脏批' : 'clean' }}</em>
          ×{{ g.qty_total }} {{ g.unit || '' }}
          <small>批次 #{{ g.lot_ids.join('、#') }}</small>
        </span>
        <button class="ghost" @click="preview(g.lot_ids)">预览</button>
        <button @click="confirm(g.lot_ids)">确认合并</button>
      </div>
    </section>
    <p v-else class="muted">暂无可合并候选。</p>

    <section class="card">
      <h3>自选批次（可演示失败路径：到期日不同 / 脏·clean 混选）</h3>
      <label v-for="x in lots" :key="x.id" class="pick">
        <input type="checkbox" :value="x.id" v-model="picked" />
        <span class="lot">
          #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
          <em :class="x.data_quality">{{ x.data_quality === 'dirty' ? '脏批' : 'clean' }}</em>
        </span>
      </label>
      <div>
        <button class="ghost" @click="preview(picked)" :disabled="picked.length < 2">预览所选</button>
        <button @click="confirm(picked)" :disabled="picked.length < 2">确认合并所选</button>
      </div>
    </section>

    <section v-if="plan" class="card ok">
      <h3>{{ plan.already_merged ? '该组合此前已合并（幂等回放，余量未重复累加）' : '合并预览' }}</h3>
      <p>
        留下批次：<b>#{{ plan.survivor_id }}</b>；
        吞掉：<b>{{ plan.absorbed_ids.length ? '#' + plan.absorbed_ids.join('、#') : '无' }}</b>；
        合并后余量：<b>{{ plan.qty_after }}</b>
      </p>
      <p class="muted" v-if="!plan.already_merged">
        确认后全层在架只余 #{{ plan.survivor_id }} 一条；按临期消费只会扣这一条，
        且可用到合并后的完整余量 {{ plan.qty_after }}。
      </p>
      <div v-if="plan.lots" class="muted">
        <span v-for="l in plan.lots" :key="l.id">
          #{{ l.id }} {{ l.name }} ×{{ l.qty_remain }} · {{ l.expiry }} ｜
        </span>
      </div>
    </section>

    <section v-if="error" class="card err">
      <h3>合并失败</h3>
      <p>{{ error }}</p>
      <p class="muted">余量已全部回到合并前，未产生任何额外批次行。</p>
    </section>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const groups = ref([])
const lots = ref([])
const picked = ref([])
const plan = ref(null)
const error = ref('')

const REASON_CN = {
  expiry_mismatch: '到期日不同，不能合并（所选批次到期日：见下）。',
  quality_mismatch: '脏批与 clean 批隔离资格不同，不能并进同一条。',
  item_mismatch: '品项不同，不能合并。',
  lot_not_on_shelf: '所选批次已不在架（可能刚被合并吞掉、已消费或过期下架）。',
  lot_empty: '所选批次余量为零。',
  need_two_lots: '请至少选择两条批次。',
  lot_not_found: '批次不存在。',
  stale_lot: '并发冲突：层状态刚被其他操作改变，请刷新后重试。',
}

async function load() {
  groups.value = await api('/merge/candidates')
  lots.value = await api('/fridge')
  const ids = new Set(lots.value.map(x => x.id))
  picked.value = picked.value.filter(id => ids.has(id))
}

function explain(e) {
  try {
    const d = JSON.parse(e.message)
    let msg = REASON_CN[d.reason] || ('合并被拒绝：' + d.reason)
    if (d.expiries) msg += '（' + d.expiries.join(' / ') + '）'
    if (d.data_qualities) msg += '（' + d.data_qualities.join(' / ') + '）'
    if (d.lot_ids) msg += ' 相关批次：#' + d.lot_ids.join('、#')
    return msg
  } catch {
    return e.message
  }
}

async function preview(ids) {
  plan.value = null; error.value = ''
  if (ids.length < 2) { error.value = REASON_CN.need_two_lots; return }
  try { plan.value = await api('/merge/preview', { method: 'POST', body: JSON.stringify({ lot_ids: ids }) }) }
  catch (e) { error.value = explain(e) }
}

async function confirm(ids) {
  plan.value = null; error.value = ''
  if (ids.length < 2) { error.value = REASON_CN.need_two_lots; return }
  try {
    plan.value = await api('/merge/confirm', { method: 'POST', body: JSON.stringify({ lot_ids: ids }) })
    await load()
  } catch (e) { error.value = explain(e); await load() }
}

onMounted(load)
</script>
<style scoped>
.card { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; margin-top: 14px; }
.card.ok { border-color: var(--teal); }
.card.err { border-color: var(--alert); background: #fff4ec; color: var(--alert); }
.group { display: flex; align-items: center; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
.group small { display: block; color: #5a7a7e; }
button.ghost { background: #fff; color: var(--teal); border: 1px solid var(--teal); }
.pick { display: block; margin: 6px 0; }
.pick input { width: auto; max-width: none; margin: 0 8px 0 0; }
em { font-style: normal; font-size: 12px; border-radius: 6px; padding: 0 6px; margin-left: 4px; }
em.clean { background: #e1f2e3; color: #2c6e33; }
em.dirty { background: #f3e1e3; color: #8a2f3a; }
</style>

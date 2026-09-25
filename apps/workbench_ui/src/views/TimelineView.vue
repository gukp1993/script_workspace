<script setup lang="ts">
/**
 * 时间轴视图（UI-014）。
 *
 * - 横向事件带：四行泳道（感知 / 状态 / 意图与策略 / 执行），事件点按
 *   ts_monotonic 归一化横坐标放置；
 * - 事件点可点击（button 原生元素，键盘可达）-> 详情面板显示 payload
 *   JSON；payload.frame_ref 存在时经 traces 帧端点取 PNG 显示；
 * - 数据来自 /traces 与 /traces/{name}/events 端点；过滤参数
 *   since/until/types/state/limit 支持就地调整后重查。
 */
import { onMounted, ref, watch } from 'vue'

import { fetchTraceFrame, type TraceEventRecord } from '../api/client'
import { laneForEvent, useTracesStore } from '../stores/traces'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()
const store = useTracesStore()

const frameUrl = ref<string | null>(null)
const frameError = ref<string | null>(null)

const TYPE_OPTIONS = [
  'perception_snapshot',
  'state_transition',
  'intent_issued',
  'policy_decision',
  'executed',
  'estop',
  'anomaly',
]

function colorFor(event: TraceEventRecord): string {
  switch (laneForEvent(event.type)) {
    case 'perception':
      return '#3b82f6'
    case 'state':
      return '#a78bfa'
    case 'policy':
      return '#f59e0b'
    default:
      return '#10b981'
  }
}

function select(event: TraceEventRecord): void {
  store.selectEvent(event.seq)
}

async function loadFrame(projectId: string, trace: string, ref_: string): Promise<void> {
  frameUrl.value = null
  frameError.value = null
  try {
    const blob = await fetchTraceFrame(projectId, trace, ref_)
    frameUrl.value = URL.createObjectURL(blob)
  } catch (exc) {
    frameError.value = exc instanceof Error ? exc.message : String(exc)
  }
}

watch(
  () => store.selectedEvent,
  (event) => {
    // 切换选中事件时刷新帧图（frame_ref 存在才取）
    const pid = projects.currentProjectId
    if (event && pid) {
      const ref_ = event.payload.frame_ref
      if (typeof ref_ === 'string' && ref_) {
        void loadFrame(pid, store.selectedTrace ?? '', ref_)
        return
      }
    }
    frameUrl.value = null
    frameError.value = null
  },
)

async function reload(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid) return
  await store.loadTraces(pid)
  await store.loadEvents()
}

onMounted(async () => {
  await projects.loadProjects()
  await reload()
})
</script>

<template>
  <section class="timeline" aria-label="事件时间轴">
    <header class="bar">
      <h2>事件时间轴</h2>
      <select
        :value="store.selectedTrace ?? ''"
        aria-label="选择轨迹"
        @change="store.selectedTrace = ($event.target as HTMLSelectElement).value; reload()"
      >
        <option v-for="t in store.traces" :key="t.name" :value="t.name">{{ t.name }}</option>
      </select>
      <span v-if="store.total > 0" class="meta">
        命中 {{ store.matched }} / 共 {{ store.total }} 条
        <span v-if="store.truncatedTail" class="warn">⚠ 尾部损坏已截断</span>
      </span>
    </header>

    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>

    <template v-else>
      <!-- 过滤条 -->
      <div class="filters">
        <label>
          类型
          <select v-model="store.filters.types" aria-label="事件类型过滤">
            <option value="">全部</option>
            <option v-for="t in TYPE_OPTIONS" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <label>
          状态
          <input v-model="store.filters.state" placeholder="from/to 匹配" />
        </label>
        <label>
          since
          <input v-model.number="store.filters.since" type="number" step="0.5" placeholder="—" />
        </label>
        <label>
          until
          <input v-model.number="store.filters.until" type="number" step="0.5" placeholder="—" />
        </label>
        <label>
          limit
          <input v-model.number="store.filters.limit" type="number" step="50" min="1" max="5000" />
        </label>
        <button @click="reload">应用过滤</button>
        <button
          @click="
            store.setFilter({ since: undefined, until: undefined, types: '', state: '' });
            reload()
          "
        >
          清空
        </button>
      </div>

      <p v-if="store.error" class="error" role="alert">
        ⚠ {{ store.error }}
        <button @click="reload">重试</button>
      </p>
      <p v-else-if="store.events.length === 0 && !store.loadingEvents" class="empty">
        暂无事件 — 运行一次会话（或调整过滤条件）后，轨迹事件将显示为四行泳道
      </p>

      <!-- 四行泳道 -->
      <div v-else class="lanes" data-testid="lanes">
        <div v-for="lane in store.lanes" :key="lane.key" class="lane">
          <span class="lane-label">{{ lane.label }}</span>
          <div class="lane-track">
            <template v-if="lane.items.length === 0">
              <span class="lane-empty">（无事件）</span>
            </template>
            <button
              v-for="item in lane.items"
              :key="item.event.seq"
              class="dot"
              :class="{ selected: store.selectedSeq === item.event.seq }"
              :style="{ left: `${Math.min(99, Math.max(0, item.ratio * 100))}%`, background: colorFor(item.event) }"
              :title="`#${item.event.seq} ${item.event.type} @ ${item.event.ts_monotonic.toFixed(2)}s`"
              :aria-label="`事件 #${item.event.seq} ${item.event.type}`"
              @click="select(item.event)"
            />
          </div>
        </div>
        <p class="axis meta">时间轴方向：左旧右新（按 ts_monotonic 归一化）</p>
      </div>

      <!-- 详情面板 -->
      <div v-if="store.selectedEvent" class="detail" data-testid="event-detail">
        <h3>
          事件 #{{ store.selectedEvent.seq }} · {{ store.selectedEvent.type }}
          <button class="mini" @click="store.selectEvent(null)">关闭</button>
        </h3>
        <p class="meta">
          ts {{ store.selectedEvent.ts_monotonic }} · session {{ store.selectedEvent.session_id || '—' }} · correlation
          {{ store.selectedEvent.correlation_id || '—' }}
        </p>
        <div class="detail-body">
          <pre class="payload">{{ JSON.stringify(store.selectedEvent.payload, null, 2) }}</pre>
          <div v-if="frameUrl || frameError" class="frame-box">
            <img v-if="frameUrl" :src="frameUrl" alt="事件关联帧" />
            <p v-else class="meta">帧读取失败：{{ frameError }}</p>
          </div>
          <p v-else-if="typeof store.selectedEvent.payload.frame_ref === 'string'" class="meta">帧加载中…</p>
          <p v-else class="meta">该事件未关联帧（payload.frame_ref 不存在）</p>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
h3 {
  margin: 0;
  font-size: 14px;
  display: flex;
  align-items: center;
  gap: 10px;
}
select,
input {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 4px 8px;
}
.filters {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}
.filters label {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  color: #94a3b8;
}
.filters input {
  width: 110px;
}
.meta {
  color: #64748b;
  font-size: 12px;
}
.warn {
  color: #fbbf24;
}
.lanes {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.lane {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 10px;
  align-items: center;
}
.lane-label {
  color: #94a3b8;
  font-size: 12px;
  text-align: right;
}
.lane-track {
  position: relative;
  height: 28px;
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
}
.lane-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #334155;
  font-size: 11px;
}
.dot {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid #0f172a;
  padding: 0;
}
.dot.selected {
  outline: 2px solid #e2e8f0;
}
.axis {
  margin: 0;
  text-align: right;
}
.detail {
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 10px 12px;
  background: #111c31;
}
.detail-body {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 12px;
  margin-top: 8px;
}
.payload {
  margin: 0;
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow: auto;
  max-height: 260px;
}
.frame-box img {
  max-width: 100%;
  border-radius: 6px;
}
.mini {
  padding: 2px 8px;
}
.error {
  color: #f87171;
}
.empty {
  color: #64748b;
}
</style>

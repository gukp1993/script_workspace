<script setup lang="ts">
/**
 * 状态图（UI-011，纯 SVG 只读）。
 *
 * - 数据来自 machine store 数据源（项目 machines 领域对象，选中后转为
 *   表单模型），domain/stateGraph 自动布局：状态=圆角矩形（环形布局），
 *   迁移=带箭头贝塞尔曲线 + when 标签；
 * - 不可达状态红色描边 + "不可达"文字标识（可达性由前端 BFS 计算，
 *   与后端静态分析 state_unreachable 同语义）；
 * - 目标状态未定义的悬空迁移画向画布底部并标注"?"。
 */
import { computed, onMounted, ref } from 'vue'

import { formToGraphMachine, normalizeMachine } from '../domain/machineYaml'
import { layoutStateGraph } from '../domain/stateGraph'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()
const selectedId = ref('')

const machines = computed(() => projects.objects['machines'] ?? [])

/** 选中状态机 -> 布局（自动包含可达性标记） */
const layout = computed(() => {
  const found = machines.value.find((m) => (m as { machine_id?: string }).machine_id === selectedId.value)
  if (!found) return null
  return layoutStateGraph(formToGraphMachine(normalizeMachine(found as Record<string, unknown>)))
})

const unreachableCount = computed(() => layout.value?.nodes.filter((n) => n.unreachable).length ?? 0)

function selectFirst(): void {
  if (selectedId.value === '' && machines.value.length > 0) {
    selectedId.value = String((machines.value[0] as { machine_id?: string }).machine_id)
  }
}

onMounted(async () => {
  await projects.loadProjects()
  if (projects.currentProjectId) {
    await projects.loadObjects('machines')
    selectFirst()
  }
})
</script>

<template>
  <section class="state-graph" aria-label="状态图">
    <header class="bar">
      <h2>状态图</h2>
      <select v-model="selectedId" aria-label="选择状态机">
        <option v-for="m in machines" :key="String(m.machine_id)" :value="String(m.machine_id)">
          {{ m.machine_id }}
        </option>
      </select>
      <span v-if="layout" class="meta">{{ layout.nodes.length }} 状态 · {{ layout.edges.length }} 迁移</span>
      <span v-if="unreachableCount > 0" class="warn" role="alert">⚠ {{ unreachableCount }} 个不可达状态</span>
    </header>

    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>
    <p v-else-if="!layout" class="empty">当前项目没有状态机 — 在「状态机编辑器」创建后自动出图</p>

    <div v-else class="canvas">
      <svg :viewBox="`0 0 ${layout.width} ${layout.height}`" role="img" aria-label="状态机只读图形">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
          </marker>
        </defs>

        <!-- 迁移曲线（先画，节点覆盖其上） -->
        <g v-for="(edge, i) in layout.edges" :key="`e${i}`">
          <path :d="edge.path" fill="none" stroke="#94a3b8" stroke-width="1.4" marker-end="url(#arrow)" />
          <text :x="edge.labelX" :y="edge.labelY - 4" text-anchor="middle" class="edge-label">
            {{ edge.when || '(无条件)' }}
          </text>
        </g>

        <!-- 状态节点：圆角矩形；不可达红描边 + 文字 -->
        <g v-for="node in layout.nodes" :key="node.name">
          <rect
            :x="node.x - 52"
            :y="node.y - 22"
            width="104"
            height="44"
            rx="10"
            :stroke="node.unreachable ? '#ef4444' : '#64748b'"
            :stroke-width="node.unreachable ? 2.5 : 1.4"
            :fill="node.isInitial ? '#1d4ed855' : '#111c31'"
          />
          <text :x="node.x" :y="node.y + (node.unreachable ? -2 : 5)" text-anchor="middle" class="node-label">
            {{ node.name }}
          </text>
          <text v-if="node.unreachable" :x="node.x" :y="node.y + 14" text-anchor="middle" class="node-warn">
            ⚠ 不可达
          </text>
          <text v-if="node.isInitial" :x="node.x" :y="node.y - 28" text-anchor="middle" class="node-tag">初始</text>
          <text v-else-if="node.terminal" :x="node.x" :y="node.y - 28" text-anchor="middle" class="node-tag">终态</text>
        </g>
      </svg>

      <p class="legend">
        图例：<span class="legend-initial">■ 初始状态</span> ·
        <span class="legend-normal">□ 普通状态</span> ·
        <span class="legend-warn">⚠ 红描边 = 从初始不可达（静态分析会报 state_unreachable）</span>
      </p>
    </div>
  </section>
</template>

<style scoped>
.state-graph {
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
select {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.meta {
  color: #94a3b8;
  font-size: 12px;
}
.warn {
  color: #f87171;
  font-size: 13px;
}
.canvas svg {
  width: 100%;
  height: auto;
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 8px;
}
.edge-label {
  fill: #cbd5e1;
  font-size: 11px;
  paint-order: stroke;
  stroke: #020617;
  stroke-width: 3px;
}
.node-label {
  fill: #e2e8f0;
  font-size: 13px;
  font-weight: 600;
}
.node-warn {
  fill: #fca5a5;
  font-size: 10px;
}
.node-tag {
  fill: #93c5fd;
  font-size: 10px;
}
.legend {
  margin: 0;
  color: #64748b;
  font-size: 12px;
}
.legend-initial {
  color: #93c5fd;
}
.legend-normal {
  color: #94a3b8;
}
.legend-warn {
  color: #fca5a5;
}
.empty {
  color: #64748b;
}
</style>

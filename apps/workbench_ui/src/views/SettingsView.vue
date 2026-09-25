<script setup lang="ts">
/**
 * 设置页（UI-016，CTL-008）。
 *
 * - 读改 /settings：模式默认值 / 轨迹保留期 / 无人值守调度；
 * - 高风险项（默认模式改为 real_input、开启无人值守调度）：红色警示 +
 *   勾选二次确认后才允许保存（store 层再拦一道）；
 * - 固定说明：受保护目标硬锁不可由 UI 解锁（控制面 409 兜底）。
 */
import { computed, onMounted, ref } from 'vue'

import { MODE_OPTIONS, modeRisk } from '../domain/modes'
import { useSettingsStore } from '../stores/settings'

const settings = useSettingsStore()

const selectedMode = ref('shadow')
const retentionDays = ref(7)
const schedule = ref('disabled')
/** 高风险二次确认勾选 */
const highRiskConfirmed = ref(false)

const pendingHighRisk = computed(
  () => selectedMode.value === 'real_input' || schedule.value === 'enabled',
)

const selectedModeRisk = computed(() => modeRisk(selectedMode.value))

/** 补丁与当前值的差异 */
function buildPatch(): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  if (selectedMode.value !== settings.settings.default_mode) patch.default_mode = selectedMode.value
  if (retentionDays.value !== settings.settings.trace_retention_days) {
    patch.trace_retention_days = retentionDays.value
  }
  if (schedule.value !== settings.settings.unattended_schedule) {
    patch.unattended_schedule = schedule.value
  }
  return patch
}

const hasChanges = computed(() => Object.keys(buildPatch()).length > 0)

async function save(): Promise<void> {
  const patch = buildPatch()
  if (Object.keys(patch).length === 0) return
  // 勾选确认 -> 解锁 store 的一次性高风险放行
  if (pendingHighRisk.value && highRiskConfirmed.value) settings.armHighRisk()
  const ok = await settings.save(patch)
  if (ok) highRiskConfirmed.value = false
}

onMounted(async () => {
  await settings.load()
  selectedMode.value = settings.settings.default_mode
  retentionDays.value = settings.settings.trace_retention_days
  schedule.value = settings.settings.unattended_schedule
})
</script>

<template>
  <section class="settings" aria-label="工作区设置">
    <header class="bar">
      <h2>工作区设置</h2>
      <button :disabled="settings.loading" @click="settings.load()">刷新</button>
    </header>

    <p v-if="settings.error" class="error" role="alert">⚠ {{ settings.error }}</p>
    <p v-if="settings.notice" role="status">{{ settings.notice }}</p>
    <p v-if="settings.loading && !settings.loaded" class="empty">加载设置中…</p>

    <!-- loaded 后才渲染表单：避免异步加载回填覆盖用户正在编辑的值 -->
    <template v-if="settings.loaded">
      <h3>默认执行模式（新会话缺省）</h3>
    <div class="modes" role="radiogroup" aria-label="默认执行模式">
      <button
        v-for="mode in MODE_OPTIONS"
        :key="mode"
        role="radio"
        :aria-checked="selectedMode === mode"
        class="mode"
        :class="{ active: selectedMode === mode }"
        :style="{ borderColor: modeRisk(mode).color }"
        @click="selectedMode = mode"
      >
        <span class="dot" :style="{ background: modeRisk(mode).color }" aria-hidden="true" />
        <span>{{ modeRisk(mode).label }}</span>
        <span class="mode-desc">{{ modeRisk(mode).description }}</span>
      </button>
    </div>

    <!-- 高风险红色警示 + 二次确认（不只靠颜色：文字 + ⚠ 图标 + 勾选交互） -->
    <div v-if="pendingHighRisk" class="high-risk" role="alert">
      <p>
        ⚠ 高风险变更：默认模式为
        <strong>{{ selectedModeRisk.label }}</strong>
        （{{ selectedModeRisk.description }}）。新会话将默认以高权限启动，必须逐会话人工确认。
      </p>
      <label class="confirm">
        <input v-model="highRiskConfirmed" type="checkbox" />
        我已理解风险，确认要保存该高风险默认值
      </label>
    </div>

    <h3>轨迹保留期</h3>
    <label class="field">
      保留天数（1~365）
      <input v-model.number="retentionDays" type="number" min="1" max="365" step="1" />
    </label>

    <h3>无人值守调度</h3>
    <label class="field">
      调度开关
      <select v-model="schedule">
        <option value="disabled">disabled（关闭，安全默认）</option>
        <option value="enabled">enabled（开启，高风险）</option>
      </select>
    </label>

    <div class="actions">
      <button
        :disabled="!hasChanges || settings.saving || (pendingHighRisk && !highRiskConfirmed)"
        title="高风险变更需先勾选二次确认"
        @click="save"
      >
        保存设置
      </button>
      <span v-if="pendingHighRisk && !highRiskConfirmed" class="muted">高风险变更需先勾选二次确认</span>
    </div>

    <!-- 固定说明：受保护目标硬锁 -->
    <div class="hard-lock">
      <h3>安全硬锁（不可由 UI 解锁）</h3>
      <p>
        🔒 工作区存在受保护在线目标（target.protected_online = true）时：把默认模式改为 real_input、
        或开启无人值守调度会被控制面直接拒绝（HTTP 409）。该硬锁只能通过修改目标档案解除，
        配置面永远不能成为绕过目标级保护的通道（CTL-008）。
      </p>
    </div>
    </template>
  </section>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 760px;
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
  margin: 6px 0;
  font-size: 13px;
  color: #94a3b8;
}
.modes {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.mode {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px;
  border-width: 2px;
  text-align: left;
}
.mode.active {
  background: #1e293b;
  outline: 2px solid #64748b;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.mode-desc {
  font-size: 11px;
  color: #94a3b8;
}
.high-risk {
  border: 1px solid #ef4444;
  background: #7f1d1d33;
  border-radius: 8px;
  padding: 10px 14px;
  color: #fca5a5;
}
.high-risk p {
  margin: 0 0 8px;
}
.confirm {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #e2e8f0;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: #94a3b8;
  max-width: 260px;
}
input,
select {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.muted {
  color: #64748b;
  font-size: 12px;
}
.hard-lock {
  border: 1px dashed #f59e0b;
  border-radius: 8px;
  padding: 10px 14px;
}
.hard-lock p {
  margin: 0;
  color: #fbbf24;
  font-size: 13px;
}
.error {
  color: #f87171;
  margin: 0;
}
</style>

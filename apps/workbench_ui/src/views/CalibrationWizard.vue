<script setup lang="ts">
/**
 * 标定向导（UI-008 简化版）：三步表单。
 *
 * 1. 分辨率 / DPI / ui_scale（+ 标定 ID）；
 * 2. 锚点数值输入（归一化 0~1 坐标，可增删行）；
 * 3. 生成 CalibrationProfile JSON 预览 + 保存到项目（POST calibrations）。
 * 校验错误就地显示；保存成功后提示并保持预览。
 */
import { computed, ref } from 'vue'

import { apiFetch } from '../api/client'
import { calibrationToDict, validateCalibrationForm, type CalibrationForm } from '../domain/machineYaml'
import { useProjectStore } from '../stores/project'

const projects = useProjectStore()

const step = ref(1)
const saving = ref(false)
const saved = ref(false)
const error = ref<string | null>(null)

const form = ref<CalibrationForm>({
  calibrationId: '',
  resolutionWidth: 1920,
  resolutionHeight: 1080,
  dpiPercent: 100,
  uiScale: 1,
  anchors: [{ name: 'primary_button', x: 0.5, y: 0.5 }],
})

const issues = computed(() => validateCalibrationForm(form.value))
const stepIssues = computed(() => {
  if (step.value === 1) return issues.value.filter((i) => ['calibrationId', 'resolutionWidth', 'resolutionHeight', 'dpiPercent', 'uiScale'].includes(i.field))
  if (step.value === 2) return issues.value.filter((i) => i.field === 'anchors')
  return issues.value
})

/** 步骤内联 JSON 预览（保存的就是这个对象） */
const previewJson = computed(() => JSON.stringify(calibrationToDict(form.value), null, 2))

function addAnchor(): void {
  form.value.anchors.push({ name: '', x: 0.5, y: 0.5 })
}

function removeAnchor(index: number): void {
  form.value.anchors.splice(index, 1)
}

function next(): void {
  if (stepIssues.value.length === 0) step.value = Math.min(3, step.value + 1)
}

function back(): void {
  step.value = Math.max(1, step.value - 1)
}

async function save(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid || issues.value.length > 0) return
  saving.value = true
  error.value = null
  saved.value = false
  try {
    await apiFetch(`/api/v1/projects/${pid}/calibrations`, {
      method: 'POST',
      body: calibrationToDict(form.value),
    })
    saved.value = true
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="calibration" aria-label="标定向导">
    <h2>标定向导</h2>
    <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择一个项目</p>

    <template v-else>
      <ol class="steps">
        <li :class="{ current: step === 1, done: step > 1 }">1. 分辨率与缩放</li>
        <li :class="{ current: step === 2, done: step > 2 }">2. 锚点数值</li>
        <li :class="{ current: step === 3 }">3. 预览与保存</li>
      </ol>

      <!-- 步骤 1：分辨率/DPI/ui_scale -->
      <div v-if="step === 1" class="step-body">
        <label>
          标定 ID（惯例如 1920x1080-100）
          <input v-model="form.calibrationId" placeholder="1920x1080-100" />
        </label>
        <label>
          分辨率宽度（像素）
          <input v-model.number="form.resolutionWidth" type="number" step="1" min="320" max="16384" />
        </label>
        <label>
          分辨率高度（像素）
          <input v-model.number="form.resolutionHeight" type="number" step="1" min="240" max="16384" />
        </label>
        <label>
          Windows DPI 缩放（%）
          <input v-model.number="form.dpiPercent" type="number" step="25" min="50" max="500" />
        </label>
        <label>
          目标程序 UI 缩放系数
          <input v-model.number="form.uiScale" type="number" step="0.05" min="0.5" max="4" />
        </label>
      </div>

      <!-- 步骤 2：锚点数值输入 -->
      <div v-if="step === 2" class="step-body">
        <p class="hint">锚点为归一化坐标（0~1）：在目标分辨率下实际像素 = 归一化值 × 分辨率。</p>
        <div v-for="(anchor, i) in form.anchors" :key="i" class="anchor-row">
          <input v-model="anchor.name" placeholder="锚点名（如 primary_button）" aria-label="锚点名称" />
          <input v-model.number="anchor.x" type="number" step="0.005" min="0" max="1" aria-label="锚点 X（0~1）" />
          <input v-model.number="anchor.y" type="number" step="0.005" min="0" max="1" aria-label="锚点 Y（0~1）" />
          <button type="button" :disabled="form.anchors.length <= 1" @click="removeAnchor(i)">删除</button>
        </div>
        <button type="button" @click="addAnchor">添加锚点</button>
      </div>

      <!-- 步骤 3：JSON 预览 + 保存 -->
      <div v-if="step === 3" class="step-body">
        <h3>CalibrationProfile JSON 预览</h3>
        <pre class="preview" data-testid="calibration-json">{{ previewJson }}</pre>
        <div class="actions">
          <button :disabled="issues.length > 0 || saving" @click="save">保存到项目</button>
          <span v-if="saved" role="status">✔ 已保存（{{ form.calibrationId }}）</span>
          <span v-if="error" class="issue" role="alert">⚠ {{ error }}</span>
        </div>
      </div>

      <!-- 就地校验错误（不只靠颜色：带 ⚠ 图标） -->
      <ul v-if="stepIssues.length > 0" class="issues" role="alert">
        <li v-for="issue in stepIssues" :key="issue.field + issue.message">⚠ {{ issue.message }}</li>
      </ul>

      <div class="nav">
        <button :disabled="step === 1" @click="back">上一步</button>
        <button v-if="step < 3" :disabled="stepIssues.length > 0" @click="next">下一步</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.calibration {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 640px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
h3 {
  margin: 0 0 6px;
  font-size: 14px;
}
.steps {
  display: flex;
  gap: 12px;
  list-style: none;
  margin: 0;
  padding: 0;
  color: #64748b;
  font-size: 13px;
}
.steps .current {
  color: #e2e8f0;
  font-weight: 600;
}
.steps .done {
  color: #6ee7b7;
}
.step-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: #94a3b8;
}
input {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.anchor-row {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr auto;
  gap: 8px;
}
.hint {
  margin: 0;
  color: #64748b;
  font-size: 12px;
}
.preview {
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow-x: auto;
  margin: 0;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.issues {
  margin: 0;
  padding-left: 4px;
  list-style: none;
  color: #f87171;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav {
  display: flex;
  gap: 10px;
}
.empty {
  color: #64748b;
}
</style>

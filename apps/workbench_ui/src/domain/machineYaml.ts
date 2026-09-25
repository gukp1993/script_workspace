/**
 * 状态机表单 <-> YAML 双向同步（UI-009/010，纯函数便于单测）。
 *
 * 约定：
 * - 表单模型（MachineForm）是唯一数据源：表单改动 -> formToYaml 生成只读
 *   预览；YAML 编辑区粘贴 -> parseMachineYaml 解析，成功才回灌表单，
 *   失败只就地报错、**绝不覆盖表单**（errorState 原样返回）；
 * - YAML 走 js-yaml safe_load/safe_dump（禁任意对象反序列化）；
 * - 序列化字段与 domain_model.parse_machine 对齐：schema_version/machine_id/
 *   initial/states；状态内 transitions/entry/exit/timeout_seconds/terminal。
 *   迁移上的 priority/timeout_seconds 是编辑器扩展字段（解析器容忍未知键，
 *   随 YAML 持久化，供人读与后续管线消费）。
 */

import { dump, load } from 'js-yaml'

import type { GraphMachine } from './stateGraph'

/** 单条迁移的表单模型 */
export interface TransitionForm {
  when: string
  to: string
  priority: number
  /** UI 扩展：超时（秒）；空表示不设置 */
  timeoutSeconds: number | null
}

/** 单个状态的表单模型 */
export interface StateForm {
  name: string
  terminal: boolean
  timeoutSeconds: number | null
  transitions: TransitionForm[]
  /** entry/exit 动作（JSON 文本域解析产物；解析失败保留上次合法值并报错） */
  entry: unknown[]
  exit: unknown[]
}

/** 状态机表单模型 */
export interface MachineForm {
  machineId: string
  initial: string
  states: StateForm[]
}

export type MachineParseResult =
  | { ok: true; form: MachineForm }
  | { ok: false; error: string }

/** 空白状态机（新建用） */
export function emptyMachineForm(): MachineForm {
  return {
    machineId: 'main',
    initial: 'idle',
    states: [{ name: 'idle', terminal: true, timeoutSeconds: null, transitions: [], entry: [], exit: [] }],
  }
}

/** 后端领域对象 -> 表单模型（容错：缺字段补默认值） */
export function normalizeMachine(data: Record<string, unknown>): MachineForm {
  const statesRaw = (data.states ?? {}) as Record<string, Record<string, unknown>>
  const states: StateForm[] = Object.entries(statesRaw).map(([name, body]) => ({
    name,
    terminal: body?.terminal === true,
    timeoutSeconds: typeof body?.timeout_seconds === 'number' ? (body.timeout_seconds as number) : null,
    transitions: Array.isArray(body?.transitions)
      ? (body.transitions as Record<string, unknown>[]).map((tr) => ({
          when: String(tr?.when ?? ''),
          to: String(tr?.to ?? ''),
          priority: typeof tr?.priority === 'number' ? (tr.priority as number) : 0,
          timeoutSeconds: typeof tr?.timeout_seconds === 'number' ? (tr.timeout_seconds as number) : null,
        }))
      : [],
    entry: Array.isArray(body?.entry) ? (body.entry as unknown[]) : [],
    exit: Array.isArray(body?.exit) ? (body.exit as unknown[]) : [],
  }))
  return {
    machineId: String(data.machine_id ?? 'main'),
    initial: String(data.initial ?? states[0]?.name ?? ''),
    states,
  }
}

/** 表单模型 -> 后端领域对象（与 parse_machine 字段对齐） */
export function formToMachineDict(form: MachineForm): Record<string, unknown> {
  const states: Record<string, unknown> = {}
  for (const state of form.states) {
    const body: Record<string, unknown> = {}
    if (state.transitions.length > 0) {
      body.transitions = state.transitions.map((tr) => {
        const item: Record<string, unknown> = {
          when: tr.when,
          to: tr.to,
          priority: Number.isFinite(tr.priority) ? tr.priority : 0,
        }
        if (tr.timeoutSeconds !== null && Number.isFinite(tr.timeoutSeconds)) {
          item.timeout_seconds = tr.timeoutSeconds
        }
        return item
      })
    }
    if (state.entry.length > 0) body.entry = state.entry
    if (state.exit.length > 0) body.exit = state.exit
    if (state.timeoutSeconds !== null && Number.isFinite(state.timeoutSeconds)) {
      body.timeout_seconds = state.timeoutSeconds
    }
    if (state.terminal) body.terminal = true
    states[state.name] = body
  }
  return {
    schema_version: 1,
    machine_id: form.machineId,
    initial: form.initial,
    states,
  }
}

/** 表单 -> YAML 文本（只读预览用；dump 失败返回错误说明） */
export function formToYaml(form: MachineForm): string {
  return dump(formToMachineDict(form), { noRefs: true, sortKeys: false })
}

/** YAML 文本 -> 表单模型；任何解析/形状错误都以 {ok:false,error} 返回（不抛异常） */
export function parseMachineYaml(text: string): MachineParseResult {
  let data: unknown
  try {
    // js-yaml v4 默认 schema 已剔除不安全类型（js/function 等），safe 等价
    data = load(text)
  } catch (exc) {
    return { ok: false, error: exc instanceof Error ? exc.message : String(exc) }
  }
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    return { ok: false, error: 'YAML 顶层必须是映射（machine_id/initial/states）' }
  }
  const obj = data as Record<string, unknown>
  if (!obj.machine_id || typeof obj.machine_id !== 'string') {
    return { ok: false, error: '缺少 machine_id（字符串）' }
  }
  if (!obj.initial || typeof obj.initial !== 'string') {
    return { ok: false, error: '缺少 initial（字符串，初始状态名）' }
  }
  const statesRaw = obj.states
  if (statesRaw === null || typeof statesRaw !== 'object' || Array.isArray(statesRaw)) {
    return { ok: false, error: 'states 必须是非空映射（状态名 -> 定义）' }
  }
  const entries = Object.entries(statesRaw as Record<string, unknown>)
  if (entries.length === 0) {
    return { ok: false, error: 'states 不能为空：至少定义一个状态' }
  }
  for (const [name, body] of entries) {
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      return { ok: false, error: `状态 ${name} 的定义必须是映射` }
    }
    const transitions = (body as Record<string, unknown>).transitions
    if (transitions !== undefined && !Array.isArray(transitions)) {
      return { ok: false, error: `状态 ${name} 的 transitions 必须是列表` }
    }
    for (const tr of (transitions ?? []) as unknown[]) {
      if (tr === null || typeof tr !== 'object' || Array.isArray(tr)) {
        return { ok: false, error: `状态 ${name} 的迁移必须是映射（when/to）` }
      }
      const item = tr as Record<string, unknown>
      if (typeof item.when !== 'string' || !item.when) {
        return { ok: false, error: `状态 ${name} 存在缺少 when 表达式的迁移` }
      }
      if (typeof item.to !== 'string' || !item.to) {
        return { ok: false, error: `状态 ${name} 存在缺少 to 目标的迁移` }
      }
    }
  }
  if (!(statesRaw as Record<string, unknown>)[obj.initial]) {
    return { ok: false, error: `initial 状态 ${obj.initial} 未在 states 中定义` }
  }
  return { ok: true, form: normalizeMachine(obj) }
}

/** 表单 -> 图形布局输入（避免多余转换层） */
export function formToGraphMachine(form: MachineForm): GraphMachine {
  return {
    machineId: form.machineId,
    initial: form.initial,
    states: form.states.map((s) => ({
      name: s.name,
      terminal: s.terminal,
      timeoutSeconds: s.timeoutSeconds,
      transitions: s.transitions.map((t) => ({
        when: t.when,
        to: t.to,
        priority: t.priority,
        timeoutSeconds: t.timeoutSeconds,
      })),
      entry: s.entry,
      exit: s.exit,
    })),
  }
}

// ---------------------------------------------------------------------------
// 检测器表单校验（UI-007，纯函数）
// ---------------------------------------------------------------------------

/** 检测器表单模型 */
export interface DetectorForm {
  detectorId: string
  type: string
  /** roi: [x, y, w, h] 归一化 */
  roi: [number, number, number, number]
  threshold: number
  stableFrames: number
  fieldName: string
  template: string
}

export interface FormIssue {
  field: string
  message: string
}

/** 检测器类型可选项（vision_core.registry 支持的类型） */
export const DETECTOR_TYPES: ReadonlyArray<string> = ['color_bar_ratio', 'template_match']

/** 后端检测器领域对象 -> 表单模型（容错：缺字段补默认值） */
export function normalizeDetector(data: Record<string, unknown>): DetectorForm {
  const roiRaw = Array.isArray(data.roi) ? (data.roi as unknown[]) : []
  const num = (v: unknown, fallback: number): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback)
  return {
    detectorId: String(data.detector_id ?? ''),
    type: String(data.type ?? 'color_bar_ratio'),
    roi: [num(roiRaw[0], 0), num(roiRaw[1], 0), num(roiRaw[2], 0.2), num(roiRaw[3], 0.1)],
    threshold: num(data.threshold, 0.5),
    stableFrames: Math.max(1, Math.trunc(num(data.stable_frames, 1))),
    fieldName: String(data.field_name ?? ''),
    template: String(data.template ?? ''),
  }
}

/** 检测器表单校验：返回问题列表（空列表 = 通过） */
export function validateDetectorForm(form: DetectorForm): FormIssue[] {
  const issues: FormIssue[] = []
  if (!/^[a-z][a-z0-9_-]*$/.test(form.detectorId)) {
    issues.push({ field: 'detectorId', message: '检测器 ID 须为小写字母开头的 kebab-case' })
  }
  if (!DETECTOR_TYPES.includes(form.type)) {
    issues.push({ field: 'type', message: `类型只允许 ${DETECTOR_TYPES.join(' / ')}` })
  }
  if (!Number.isFinite(form.threshold) || form.threshold < 0 || form.threshold > 1) {
    issues.push({ field: 'threshold', message: '阈值必须在 0~1 之间' })
  }
  if (!Number.isInteger(form.stableFrames) || form.stableFrames < 1) {
    issues.push({ field: 'stableFrames', message: '稳定帧数必须是不小于 1 的整数' })
  }
  const [x, y, w, h] = form.roi
  for (const v of [x, y, w, h]) {
    if (!Number.isFinite(v) || v < 0 || v > 1) {
      issues.push({ field: 'roi', message: 'ROI 各分量必须是 0~1 的归一化数值' })
      break
    }
  }
  if (Number.isFinite(w) && Number.isFinite(x) && x + w > 1.000001) {
    issues.push({ field: 'roi', message: 'ROI 越界：x + w 不能超过 1' })
  }
  if (Number.isFinite(h) && Number.isFinite(y) && y + h > 1.000001) {
    issues.push({ field: 'roi', message: 'ROI 越界：y + h 不能超过 1' })
  }
  if (!form.fieldName.trim()) {
    issues.push({ field: 'fieldName', message: '感知字段名不能为空' })
  }
  if (form.type === 'template_match' && !form.template.trim()) {
    issues.push({ field: 'template', message: '模板匹配必须指定模板资产路径' })
  }
  return issues
}

/** 阈值效果文案（无实时管线时的"离线校验"占位说明，UI-007） */
export function thresholdHint(form: DetectorForm): string {
  if (form.type === 'color_bar_ratio') {
    return `离线校验：比率 ≥ ${form.threshold} 判为命中，连续 ${form.stableFrames} 帧稳定后写入字段 ${form.fieldName}`
  }
  return `离线校验：匹配度 ≥ ${form.threshold} 判为命中，连续 ${form.stableFrames} 帧稳定后写入字段 ${form.fieldName}`
}

// ---------------------------------------------------------------------------
// 标定向导校验（UI-008 简化，纯函数）
// ---------------------------------------------------------------------------

export interface AnchorForm {
  name: string
  x: number
  y: number
}

export interface CalibrationForm {
  calibrationId: string
  resolutionWidth: number
  resolutionHeight: number
  dpiPercent: number
  uiScale: number
  anchors: AnchorForm[]
}

/** 标定表单 -> 后端领域对象（parse_calibration 字段对齐） */
export function calibrationToDict(form: CalibrationForm): Record<string, unknown> {
  const anchors: Record<string, [number, number]> = {}
  for (const a of form.anchors) {
    anchors[a.name] = [a.x, a.y]
  }
  return {
    schema_version: 1,
    calibration_id: form.calibrationId,
    resolution: `${form.resolutionWidth}x${form.resolutionHeight}`,
    dpi_percent: form.dpiPercent,
    ui_scale: form.uiScale,
    anchors,
  }
}

/** 标定表单校验：返回问题列表（空列表 = 通过） */
export function validateCalibrationForm(form: CalibrationForm): FormIssue[] {
  const issues: FormIssue[] = []
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(form.calibrationId)) {
    issues.push({ field: 'calibrationId', message: '标定 ID 非法（字母/数字开头，可含 . - _）' })
  }
  if (!Number.isInteger(form.resolutionWidth) || form.resolutionWidth < 320 || form.resolutionWidth > 16384) {
    issues.push({ field: 'resolutionWidth', message: '宽度须为 320~16384 的整数像素' })
  }
  if (!Number.isInteger(form.resolutionHeight) || form.resolutionHeight < 240 || form.resolutionHeight > 16384) {
    issues.push({ field: 'resolutionHeight', message: '高度须为 240~16384 的整数像素' })
  }
  if (!Number.isInteger(form.dpiPercent) || form.dpiPercent < 50 || form.dpiPercent > 500) {
    issues.push({ field: 'dpiPercent', message: 'DPI 缩放须为 50~500 的整数百分比' })
  }
  if (!Number.isFinite(form.uiScale) || form.uiScale < 0.5 || form.uiScale > 4) {
    issues.push({ field: 'uiScale', message: 'UI 缩放系数须为 0.5~4 的数值' })
  }
  if (form.anchors.length === 0) {
    issues.push({ field: 'anchors', message: '至少提供一个锚点' })
  }
  const seen = new Set<string>()
  for (const a of form.anchors) {
    if (!a.name.trim()) {
      issues.push({ field: 'anchors', message: '锚点名称不能为空' })
    } else if (seen.has(a.name)) {
      issues.push({ field: 'anchors', message: `锚点名称重复：${a.name}` })
    }
    seen.add(a.name)
    for (const v of [a.x, a.y]) {
      if (!Number.isFinite(v) || v < 0 || v > 1) {
        issues.push({ field: 'anchors', message: `锚点 ${a.name || '(未命名)'} 坐标必须是 0~1 的归一化数值` })
        break
      }
    }
  }
  return issues
}

// ---------------------------------------------------------------------------
// 资产删除保护（UI-006，纯函数）
// ---------------------------------------------------------------------------

/** 资产清单条目（control_plane.assets） */
export interface AssetEntry {
  asset_id: string
  path: string
  sha256: string
  version: number
  kind: string
  [key: string]: unknown
}

/** 统计引用指定资产路径的检测器数量（detector.template === asset.path） */
export function detectorsReferencing(asset: AssetEntry, detectors: Array<Record<string, unknown>>): string[] {
  const ids: string[] = []
  for (const d of detectors) {
    if (d && typeof d === 'object' && d.template === asset.path) {
      if (typeof d.detector_id === 'string') ids.push(d.detector_id)
    }
  }
  return ids
}

/** 资产 sha 前 8 位（展示用） */
export function shortSha(asset: AssetEntry): string {
  const sha = typeof asset.sha256 === 'string' ? asset.sha256 : ''
  return sha.slice(0, 8)
}

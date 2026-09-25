import { describe, expect, it } from 'vitest'

import { isKnownMode, modeRisk, MODE_OPTIONS, MODE_RISKS } from '../src/domain/modes'

describe('模式风险分级（UI-003 四色）', () => {
  it('四种模式给出 0~3 四级风险与四色', () => {
    expect(modeRisk('observe').level).toBe(0)
    expect(modeRisk('shadow').level).toBe(1)
    expect(modeRisk('dry_run').level).toBe(2)
    expect(modeRisk('real_input').level).toBe(3)
    const colors = new Set(Object.values(MODE_RISKS).map((r) => r.color))
    expect(colors.size).toBe(4) // 四色互不相同
  })

  it('只有 real_input 要求显式人工确认', () => {
    for (const mode of ['observe', 'shadow', 'dry_run']) {
      expect(modeRisk(mode).requiresConfirm).toBe(false)
    }
    expect(modeRisk('real_input').requiresConfirm).toBe(true)
  })

  it('未知模式按最高风险兜底（不过度放行）', () => {
    const risk = modeRisk('yolo_mode')
    expect(risk.level).toBe(3)
    expect(risk.requiresConfirm).toBe(true)
    expect(risk.label).toContain('yolo_mode')
  })

  it('isKnownMode 类型守卫与选项集一致', () => {
    expect(MODE_OPTIONS).toEqual(['observe', 'shadow', 'dry_run', 'real_input'])
    for (const mode of MODE_OPTIONS) expect(isKnownMode(mode)).toBe(true)
    expect(isKnownMode('nope')).toBe(false)
  })

  it('每种模式都有非空中文标签与说明（不只靠颜色表达，UI-017 预留）', () => {
    for (const risk of Object.values(MODE_RISKS)) {
      expect(risk.label.length).toBeGreaterThan(0)
      expect(risk.description.length).toBeGreaterThan(0)
    }
  })
})

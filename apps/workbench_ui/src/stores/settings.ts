/**
 * 工作区设置 store（UI-016，CTL-008）。
 *
 * 高风险变更（default_mode -> real_input / unattended_schedule -> enabled）
 * 的二次确认逻辑放在 store：`armHighRisk()` 置 armed 位、`save()` 在涉及
 * 高风险补丁且未 armed 时直接拒绝（不发请求），确认后落盘并自动解除。
 * 受保护目标硬锁不可由 UI 解锁（后端 409 兜底），此处只透出错误信息。
 */

import { defineStore } from 'pinia'

import { apiFetch, type WorkbenchSettings } from '../api/client'

type Fetcher = typeof apiFetch

/** 判断补丁是否涉及高风险项 */
export function isHighRiskPatch(patch: Partial<WorkbenchSettings>): boolean {
  return patch.default_mode === 'real_input' || patch.unattended_schedule === 'enabled'
}

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: {
      default_mode: 'shadow',
      trace_retention_days: 7,
      unattended_schedule: 'disabled',
    } as WorkbenchSettings,
    loaded: false,
    loading: false,
    saving: false,
    /** 高风险补丁已获二次确认（一次有效） */
    highRiskArmed: false,
    /** 最近一次保存的提示（成功/降级说明） */
    notice: null as string | null,
    error: null as string | null,
  }),
  getters: {
    /** 当前默认模式是否为高风险（real_input） */
    isHighRiskMode(state): boolean {
      return state.settings.default_mode === 'real_input'
    },
  },
  actions: {
    async load(fetch: Fetcher = apiFetch): Promise<void> {
      this.loading = true
      this.error = null
      try {
        this.settings = await fetch<WorkbenchSettings>('/api/v1/settings')
        this.loaded = true
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
      } finally {
        this.loading = false
      }
    },
    /** 二次确认：仅对紧接着的一次高风险 save 生效 */
    armHighRisk(): void {
      this.highRiskArmed = true
    },
    disarmHighRisk(): void {
      this.highRiskArmed = false
    },
    /**
     * 保存补丁：高风险补丁未确认时拒绝（返回 false，不发请求）；
     * 成功后以服务端返回值刷新本地并解除 armed。
     */
    async save(patch: Partial<WorkbenchSettings>, fetch: Fetcher = apiFetch): Promise<boolean> {
      if (isHighRiskPatch(patch) && !this.highRiskArmed) {
        this.error = '高风险变更需要二次确认：请先勾选确认提示后再保存'
        return false
      }
      this.error = null
      this.saving = true
      try {
        this.settings = await fetch<WorkbenchSettings>('/api/v1/settings', {
          method: 'PUT',
          body: patch,
        })
        this.notice = '设置已保存'
        this.highRiskArmed = false
        return true
      } catch (exc) {
        this.error = exc instanceof Error ? exc.message : String(exc)
        return false
      } finally {
        this.saving = false
      }
    },
  },
})

/**
 * 实时预览 store（UI-004 基础版）。
 *
 * 轮询 GET /api/v1/preview/shot（M1 主屏 JPEG；M2 换目标窗口采集），
 * 维护：状态（idle/streaming/error）、帧率（EMA 平滑）、错误信息与
 * 当前帧 objectURL。轮询卡顿不影响控制条（急停/停止走独立通道，
 * UI-012 的停止按钮不依赖预览状态）。
 */

import { defineStore } from 'pinia'

import { fetchPreviewShot } from '../api/client'

export type PreviewStatus = 'idle' | 'streaming' | 'error'

type ShotFetcher = typeof fetchPreviewShot
type NowFn = () => number

/** 轮询定时器：模块级保存，不进响应式状态 */
let pollTimer: ReturnType<typeof setInterval> | null = null

export const usePreviewStore = defineStore('preview', {
  state: () => ({
    status: 'idle' as PreviewStatus,
    fps: 0,
    frameUrl: '',
    error: null as string | null,
    /** 轮询间隔（ms） */
    intervalMs: 250,
    /** 请求缩放宽（省带宽；undefined 表示原始尺寸） */
    maxWidth: 1280 as number | undefined,
    /** 最近一次成功抓帧时间戳（ms，性能计数） */
    lastFrameAt: 0,
    frameCount: 0,
  }),
  getters: {
    isStreaming(state): boolean {
      return state.status === 'streaming'
    },
    statusLabel(state): string {
      switch (state.status) {
        case 'streaming':
          return state.fps > 0 ? `推流中（${state.fps.toFixed(1)} fps）` : '推流中'
        case 'error':
          return '预览错误'
        default:
          return '未开启'
      }
    },
  },
  actions: {
    /**
     * 抓一帧：成功则刷新 frameUrl 与 fps（EMA），失败进入 error 态。
     * fetch 与时钟均可注入（测试确定性）。
     */
    async captureOnce(fetch: ShotFetcher = fetchPreviewShot, now: NowFn = () => performance.now()): Promise<boolean> {
      try {
        const blob = await fetch('/api/v1/preview/shot', {
          params: { max_width: this.maxWidth },
        })
        if (this.frameUrl) URL.revokeObjectURL(this.frameUrl)
        this.frameUrl = URL.createObjectURL(blob)

        const ts = now()
        if (this.frameCount > 0) {
          // 用 frameCount 判断是否有上一帧（lastFrameAt 可能为 0 时刻）
          const delta = ts - this.lastFrameAt
          if (delta > 0) {
            const instant = 1000 / delta
            this.fps = this.fps > 0 ? this.fps * 0.7 + instant * 0.3 : instant
          }
        }
        this.lastFrameAt = ts
        this.frameCount += 1
        this.status = 'streaming'
        this.error = null
        return true
      } catch (exc) {
        this.status = 'error'
        this.error = exc instanceof Error ? exc.message : String(exc)
        return false
      }
    },
    /** 启动轮询（幂等）；返回是否实际启动 */
    startPolling(fetch: ShotFetcher = fetchPreviewShot, now: NowFn = () => performance.now()): boolean {
      if (pollTimer !== null) return false
      this.status = 'streaming'
      pollTimer = setInterval(() => {
        void this.captureOnce(fetch, now)
      }, this.intervalMs)
      void this.captureOnce(fetch, now) // 立即出首帧
      return true
    },
    stopPolling(): void {
      if (pollTimer !== null) {
        clearInterval(pollTimer)
        pollTimer = null
      }
      if (this.status !== 'error') this.status = 'idle'
    },
    /** 测试辅助：确认轮询定时器是否存活 */
    get pollingActive(): boolean {
      return pollTimer !== null
    },
    clearError(): void {
      this.error = null
      if (this.status === 'error') this.status = 'idle'
    },
  },
})

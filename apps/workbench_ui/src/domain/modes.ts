/**
 * 模式风险分级（UI-003）。
 *
 * 四种执行模式对应四种颜色与四级风险，用于目标选择与会话控制条的
 * 明显区分（不得只靠颜色——同时给出文字标签与确认要求）：
 * - observe    0 蓝色：只观察，不注入任何输入；
 * - shadow     1 绿色：影子模式，只记录不干预；
 * - dry_run    2 橙色：演练模式，策略全跑但不真正注入；
 * - real_input 3 红色：真实输入，必须显式人工确认后才可启动。
 */

export type ExecutionMode = 'observe' | 'shadow' | 'dry_run' | 'real_input'

export interface ModeRisk {
  /** 风险等级 0~3（数字越大风险越高） */
  level: 0 | 1 | 2 | 3
  /** 主色（tailwind 调色板取值，用于徽标/描边） */
  color: string
  /** 中文标签 */
  label: string
  /** 一句话说明 */
  description: string
  /** 启动前是否必须显式人工确认 */
  requiresConfirm: boolean
}

export const MODE_RISKS: Readonly<Record<ExecutionMode, ModeRisk>> = {
  observe: {
    level: 0,
    color: '#3b82f6',
    label: '观察',
    description: '只采集画面与窗口信息，不注入任何输入',
    requiresConfirm: false,
  },
  shadow: {
    level: 1,
    color: '#10b981',
    label: '影子',
    description: '完整执行检测与策略，只记录建议动作，不实际注入',
    requiresConfirm: false,
  },
  dry_run: {
    level: 2,
    color: '#f59e0b',
    label: '演练',
    description: '除最终输入注入外全链路执行（策略/限流/急停都生效）',
    requiresConfirm: false,
  },
  real_input: {
    level: 3,
    color: '#ef4444',
    label: '真实输入',
    description: '会向前台窗口注入真实输入，启动前必须人工确认',
    requiresConfirm: true,
  },
}

/** 后端会话接口可能出现的模式全集（含未知兜底） */
const KNOWN_MODES: ReadonlySet<string> = new Set(Object.keys(MODE_RISKS))

/**
 * 任意字符串 -> 风险分级；未知模式按最高风险兜底（宁可过度保守）。
 */
export function modeRisk(mode: string): ModeRisk {
  if (KNOWN_MODES.has(mode)) {
    return MODE_RISKS[mode as ExecutionMode]
  }
  return {
    level: 3,
    color: '#ef4444',
    label: `未知模式：${mode}`,
    description: '无法识别的执行模式，按最高风险处理',
    requiresConfirm: true,
  }
}

/** 是否为已知模式 */
export function isKnownMode(mode: string): mode is ExecutionMode {
  return KNOWN_MODES.has(mode)
}

/** 模式选择器可选项（低 -> 高风险） */
export const MODE_OPTIONS: ReadonlyArray<ExecutionMode> = ['observe', 'shadow', 'dry_run', 'real_input']

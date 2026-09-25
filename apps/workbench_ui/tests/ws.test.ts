import { describe, expect, it, vi } from 'vitest'

import { buildWsUrl, VawSocket, type WsEvent, type WsLike } from '../src/api/client'

/** 可手动驱动事件的 fake WebSocket */
function fakeSocket() {
  const sockets: Array<WsLike & { url: string }> = []
  const factory = vi.fn((url: string): WsLike => {
    const socket: WsLike & { url: string } = {
      url,
      close: vi.fn(),
      send: vi.fn(),
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
    }
    sockets.push(socket)
    return socket
  })
  return { factory, sockets }
}

function wsEvent(seq: number, type = 'session_started'): WsEvent {
  return { seq, type, payload: {}, session_id: null, ts: '2026-09-25T00:00:00Z' }
}

describe('buildWsUrl（CTL-007）', () => {
  it('http 转 ws，令牌与 last_seq 进 query', () => {
    const url = buildWsUrl('http://127.0.0.1:17653', 'tok', 42)
    expect(url).toBe('ws://127.0.0.1:17653/api/v1/ws?token=tok&last_seq=42')
  })

  it('last_seq 负数被钳到 0', () => {
    expect(buildWsUrl('http://localhost:1', 't', -5)).toContain('last_seq=0')
  })
})

describe('VawSocket 重连与续传', () => {
  it('open 时从 getLastSeq 取游标拼 URL，事件帧派发给 onEvent', () => {
    const { factory, sockets } = fakeSocket()
    const onEvent = vi.fn()
    const socket = new VawSocket({
      getLastSeq: () => 7,
      onEvent,
      socketFactory: factory,
    })
    socket.open()
    expect(sockets[0].url).toContain('last_seq=7')
    expect(sockets[0].url).toContain('token=')

    // 服务端推送一条事件 -> 派发；控制帧（pong）不派发
    sockets[0].onmessage?.({ data: JSON.stringify(wsEvent(8)) })
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'pong', seq: 8 }) })
    sockets[0].onmessage?.({ data: 'not-json' })
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent.mock.calls[0][0]).toMatchObject({ seq: 8, type: 'session_started' })
    socket.close()
  })

  it('异常断开后按延迟重连，且重连 URL 携带更新后的 last_seq', () => {
    vi.useFakeTimers()
    try {
      const { factory, sockets } = fakeSocket()
      let latest = 10
      const socket = new VawSocket({
        getLastSeq: () => latest,
        onEvent: (e) => {
          latest = e.seq
        },
        socketFactory: factory,
        reconnectDelayMs: 100,
      })
      socket.open()

      // 收到事件 -> 游标前进到 11
      sockets[0].onmessage?.({ data: JSON.stringify(wsEvent(11)) })
      // 连接被服务端断开 -> 触发重连调度
      sockets[0].onclose?.({})
      expect(factory).toHaveBeenCalledOnce()

      vi.advanceTimersByTime(120)
      expect(factory).toHaveBeenCalledTimes(2)
      expect(sockets[1].url).toContain('last_seq=11') // 续传游标
      socket.close()
      expect(sockets[1].close).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('resync 帧触发 onResync 回调（上层走 REST 兜底），不派发为事件', () => {
    const { factory, sockets } = fakeSocket()
    const onEvent = vi.fn()
    const onResync = vi.fn()
    const socket = new VawSocket({
      getLastSeq: () => 3,
      onEvent,
      onResync,
      socketFactory: factory,
    })
    socket.open()
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'resync', seq: 99 }) })
    expect(onResync).toHaveBeenCalledWith(99)
    expect(onEvent).not.toHaveBeenCalled()
    socket.close()
  })

  it('close 后不再重连（closedByUser 闩存）', () => {
    vi.useFakeTimers()
    try {
      const { factory, sockets } = fakeSocket()
      const socket = new VawSocket({ getLastSeq: () => 0, onEvent: () => {}, socketFactory: factory })
      socket.open()
      socket.close()
      sockets[0].onclose?.({})
      vi.advanceTimersByTime(10_000)
      expect(factory).toHaveBeenCalledOnce() // 没有第二次连接
    } finally {
      vi.useRealTimers()
    }
  })
})

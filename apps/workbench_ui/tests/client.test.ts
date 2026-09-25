import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  apiFetch,
  resolveToken,
  TOKEN_HEADER,
  TOKEN_STORAGE_KEY,
  tokenFromQuery,
} from '../src/api/client'

/** 内存 storage 替身 */
function fakeStorage() {
  const map = new Map<string, string>()
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  }
}

/** 捕获请求的 fetch 替身 */
function fakeFetch(response: Partial<Response> & { ok?: boolean; json?: () => Promise<unknown> } = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  const impl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: url.toString(), init: init ?? {} })
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      ...response,
    } as Response
  })
  return { impl, calls }
}

afterEach(() => {
  localStorage.removeItem(TOKEN_STORAGE_KEY)
})

describe('令牌解析（CTL-001）', () => {
  it('URL query 中的令牌优先并被持久化到 localStorage', () => {
    const storage = fakeStorage()
    const token = resolveToken('?token=abc123&foo=bar', storage)
    expect(token).toBe('abc123')
    expect(storage.getItem(TOKEN_STORAGE_KEY)).toBe('abc123')
  })

  it('query 缺失时回退 localStorage，都没有返回空串', () => {
    const storage = fakeStorage()
    expect(resolveToken('', storage)).toBe('')
    storage.setItem(TOKEN_STORAGE_KEY, 'stored-token')
    expect(resolveToken('', storage)).toBe('stored-token')
    // query 优先于 storage
    expect(resolveToken('?token=fresh', storage)).toBe('fresh')
  })

  it('tokenFromQuery 提取 token 字段', () => {
    expect(tokenFromQuery('?token=t1')).toBe('t1')
    expect(tokenFromQuery('?a=b')).toBeNull()
  })
})

describe('apiFetch 令牌注入与错误处理', () => {
  it('自动携带 X-VAW-Token 请求头（令牌来自注入的 query）', async () => {
    const { impl, calls } = fakeFetch()
    await apiFetch('/api/v1/health', {
      tokenQuery: '?token=secret-token',
      storage: fakeStorage(),
      fetchImpl: impl as unknown as typeof fetch,
    })
    expect(impl).toHaveBeenCalledOnce()
    expect(calls[0].init.headers).toMatchObject({ [TOKEN_HEADER]: 'secret-token' })
    expect(calls[0].url).toContain('/api/v1/health')
  })

  it('params 序列化为查询串，body 自动 JSON 化', async () => {
    const { impl, calls } = fakeFetch()
    await apiFetch('/api/v1/preview/shot', {
      tokenQuery: '?token=t',
      storage: fakeStorage(),
      fetchImpl: impl as unknown as typeof fetch,
      params: { max_width: 640, missing: undefined },
      body: { hello: '世界' },
      method: 'POST',
    })
    expect(calls[0].url).toContain('max_width=640')
    expect(calls[0].url).not.toContain('missing')
    expect(calls[0].init.method).toBe('POST')
    expect(calls[0].init.headers).toMatchObject({ 'Content-Type': 'application/json' })
    expect(calls[0].init.body).toBe(JSON.stringify({ hello: '世界' }))
  })

  it('非 2xx 抛出 ApiError 并解析统一错误体（401 不回显令牌）', async () => {
    const { impl } = fakeFetch({
      ok: false,
      status: 401,
      json: async () => ({ detail: { error: 'unauthorized', message: '缺少或错误的访问令牌' } }),
    })
    const promise = apiFetch('/api/v1/projects', {
      tokenQuery: '?token=wrong',
      storage: fakeStorage(),
      fetchImpl: impl as unknown as typeof fetch,
    })
    await expect(promise).rejects.toMatchObject({
      status: 401,
      code: 'unauthorized',
      message: '缺少或错误的访问令牌',
    })
  })

  it('错误体不是 JSON 时也给出结构化 ApiError（不抛原始异常）', async () => {
    const { impl } = fakeFetch({
      ok: false,
      status: 500,
      json: async () => {
        throw new SyntaxError('not json')
      },
    })
    await expect(
      apiFetch('/api/v1/x', {
        tokenQuery: '?token=t',
        storage: fakeStorage(),
        fetchImpl: impl as unknown as typeof fetch,
      }),
    ).rejects.toMatchObject({ status: 500, code: 'http_500' })
  })
})

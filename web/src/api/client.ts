// Same-origin JSON client. The backend's boundary middleware only accepts
// same-origin browser mutations and attributes every action to the
// authenticated principal — the dashboard never sends an actor for approvals.

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' }
  if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  // Bound reads only. A timed-out mutation may already have committed and must
  // never invite an automatic retry.
  const controller = method === 'GET' ? new AbortController() : null
  if (controller) init.signal = controller.signal
  const timer = controller ? setTimeout(() => controller.abort(), 5000) : null
  let response: Response
  let text: string
  try {
    response = await fetch(path, init)
    text = await response.text()
  } catch (error) {
    if (controller?.signal.aborted) throw new Error('The request took too long. Check your connection and retry.')
    throw error
  } finally {
    if (timer !== null) clearTimeout(timer)
  }
  let data: unknown = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }
  if (!response.ok) {
    const detail = typeof data === 'object' && data && 'detail' in data
      ? String((data as { detail: unknown }).detail)
      : response.statusText || `HTTP ${response.status}`
    throw new ApiError(response.status, detail)
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  delete: <T>(path: string, body?: unknown) => request<T>('DELETE', path, body),
}

export const enc = encodeURIComponent

/** Resolve to null instead of throwing for an expected 404. */
export async function optional<T>(promise: Promise<T>): Promise<T | null> {
  try { return await promise } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

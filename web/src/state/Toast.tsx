import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

interface ToastState { notify: (message: string, kind?: 'ok' | 'bad') => void }
const Ctx = createContext<ToastState>({ notify: () => {} })

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ message: string; kind: 'ok' | 'bad' } | null>(null)
  const timer = useRef<number | null>(null)
  const notify = useCallback((message: string, kind: 'ok' | 'bad' = 'ok') => {
    setToast({ message, kind })
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setToast(null), kind === 'bad' ? 6000 : 3200)
  }, [])
  const value = useMemo(() => ({ notify }), [notify])
  return (
    <Ctx.Provider value={value}>
      {children}
      {toast && <div className={`toast ${toast.kind}`} role="status">{toast.message}</div>}
    </Ctx.Provider>
  )
}

export const useToast = () => useContext(Ctx)

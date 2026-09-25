import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type Job } from './api'

/** Poll a job until it reaches a terminal state. Status comes from the server; nothing is simulated. */
export function useJob(jobId: string | null, onFinish?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const finishRef = useRef(onFinish)
  finishRef.current = onFinish

  useEffect(() => {
    if (!jobId) {
      setJob(null)
      return
    }
    let stopped = false
    let timer: number | undefined
    const tick = async () => {
      try {
        const j = await api.job(jobId)
        if (stopped) return
        setJob(j)
        setError(null)
        if (j.active) timer = window.setTimeout(tick, 1200)
        else finishRef.current?.(j)
      } catch (e) {
        if (stopped) return
        setError(e as ApiError)
        timer = window.setTimeout(tick, 3000)
      }
    }
    tick()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
    }
  }, [jobId])

  return { job, error }
}

export type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'

/** Debounced autosave. `save` receives the latest value; errors are surfaced, edits are kept. */
export function useAutosave<T>(value: T, save: (v: T) => Promise<void>, delay = 700, enabled = true) {
  const [state, setState] = useState<SaveState>('idle')
  const [error, setError] = useState<ApiError | null>(null)
  const first = useRef(true)
  const saveRef = useRef(save)
  saveRef.current = save
  const pending = useRef<number | undefined>(undefined)
  const latest = useRef(value)
  latest.current = value

  const flush = useCallback(async () => {
    if (pending.current) {
      window.clearTimeout(pending.current)
      pending.current = undefined
    }
    setState('saving')
    try {
      await saveRef.current(latest.current)
      setState('saved')
      setError(null)
    } catch (e) {
      setState('error')
      setError(e as ApiError)
    }
  }, [])

  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    if (!enabled) return
    setState('dirty')
    if (pending.current) window.clearTimeout(pending.current)
    pending.current = window.setTimeout(flush, delay)
    return () => {
      if (pending.current) window.clearTimeout(pending.current)
    }
  }, [value, delay, enabled, flush])

  return { state, error, flush }
}

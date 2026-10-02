/**
 * Server-only helpers used by React Server Components.
 *
 * These run on the Next.js server, so they may read non-public environment
 * variables (see `INTERNAL_API_BASE_URL`) and must never be imported from a
 * client component.
 */

import 'server-only'

import { serverApiBaseUrl, apiRoutes } from '@/lib/env'
import type { HealthResponse, ModelListResponse } from '@/lib/types'

export interface BackendStatus {
  reachable: boolean
  health: HealthResponse | null
  models: ModelListResponse | null
  error: string | null
}

async function getJson<T>(path: string, timeoutMs = 8_000): Promise<T | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(`${serverApiBaseUrl}${path}`, {
      cache: 'no-store',
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) {
      return null
    }
    return (await response.json()) as T
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Probe the FastAPI control plane and its view of the Token Factory model
 * inventory. Returns `reachable: false` instead of throwing so the landing
 * page can render even while the backend is still starting up.
 */
export async function getBackendStatus(): Promise<BackendStatus> {
  const health = await getJson<HealthResponse>(apiRoutes.health)

  if (!health) {
    return {
      reachable: false,
      health: null,
      models: null,
      error: `No response from ${serverApiBaseUrl}`,
    }
  }

  const models = await getJson<ModelListResponse>(apiRoutes.models)
  return { reachable: true, health, models, error: null }
}

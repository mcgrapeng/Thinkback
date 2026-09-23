/** TanStack Query hooks：overview 30s 轮询（设计方案 P1），tasks 随筛选。 */

import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export type MemoryFilters = {
  user_id?: string;
  memory_scope_id?: string;
  statuses?: string;
  page: number;
};

export function useOverview() {
  return useQuery({
    queryKey: ["admin", "overview"],
    queryFn: api.overview,
    refetchInterval: 30_000,
  });
}

export function useTasks(statuses: string | undefined) {
  return useQuery({
    queryKey: ["admin", "tasks", statuses ?? "all"],
    queryFn: () => api.tasks({ statuses, limit: 50 }),
    refetchInterval: 30_000,
  });
}


const PAGE_SIZE = 50;

export function useMemories(filters: MemoryFilters) {
  return useQuery({
    queryKey: ["admin", "memories", filters],
    queryFn: () =>
      api.memories({
        user_id: filters.user_id || undefined,
        memory_scope_id: filters.memory_scope_id || undefined,
        statuses: filters.statuses || undefined,
        limit: PAGE_SIZE,
        offset: (filters.page - 1) * PAGE_SIZE,
      }),
  });
}

export function useMemorySource(memoryId: string | null) {
  return useQuery({
    queryKey: ["admin", "memory-source", memoryId],
    queryFn: () => api.memorySource(memoryId as string),
    enabled: memoryId !== null,
  });
}

export function useAudit(action: string | undefined) {
  return useQuery({
    queryKey: ["admin", "audit", action ?? "all"],
    queryFn: () => api.audit({ action: action || undefined, limit: 100 }),
  });
}

export function useConfig() {
  return useQuery({ queryKey: ["admin", "config"], queryFn: api.config, staleTime: 300_000 });
}

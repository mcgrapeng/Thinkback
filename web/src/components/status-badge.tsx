/** 任务域状态徽章：语义色 token + 文字（状态永不只靠颜色，AA 4.1.1/1.4.1）。
 *
 * 状态收敛映射（2026-09 共识）：有效/已完成→success，运行中→info，
 * 失败→error，死信→violet，其余（已取代/已删除/已抑制/等待中）→neutral；
 * 已删除额外加删除线与中性档区分。
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "有效",
  DELETED: "已删除",
  SUPERSEDED: "已取代",
  SUPPRESSED: "已抑制",
  pending: "等待中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  dead_letter: "死信",
};

type Tone = "success" | "error" | "info" | "neutral" | "violet";

const STATUS_TONES: Record<string, Tone> = {
  ACTIVE: "success",
  completed: "success",
  running: "info",
  failed: "error",
  dead_letter: "violet",
  SUPERSEDED: "neutral",
  DELETED: "neutral",
  SUPPRESSED: "neutral",
  pending: "neutral",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONES[status] ?? "neutral";
  return (
    <Badge
      variant={tone}
      className={cn(status === "DELETED" && "line-through decoration-neutral/60")}
    >
      {STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

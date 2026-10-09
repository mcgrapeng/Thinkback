/** 首次引导:产品 tour + 记忆治理知识卡。
 *
 * 首次访问(localStorage 无标记)自动弹出;点击任意按钮后关闭并记住。
 * 也可通过 sidebar 的 ? 按钮重新打开。 */

import { useEffect, useState } from "react";
import {
  ArrowRight,
  Brain,
  Database,
  FileSearch,
  ListTodo,
  Settings,
  ShieldAlert,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";

const STORAGE_KEY = "thinkback-onboarding-done";

const PAGES = [
  {
    icon: Brain,
    title: "总览",
    desc: "一目了然看系统健康:有效记忆、失败任务、队列水位。",
    tip: "KPI 旁的 sparkline 是过去 24h 趋势;下方热力图是 5min 粒度。",
  },
  {
    icon: Database,
    title: "记忆浏览器",
    desc: "搜索、筛选、查看每条记忆的来源反链(journal 原文)。",
    tip: "点击任意记忆行打开详情;ⓘ 图标解释每个字段含义。",
  },
  {
    icon: ListTodo,
    title: "任务监控",
    desc: "查看后台任务:运行中/失败/死信,展开行看错误。",
    tip: "失败任务的重试次数 5 次后自动进入死信队列。",
  },
  {
    icon: ShieldAlert,
    title: "治理操作",
    desc: "删除/更新/重建记忆。所有操作审计留痕。",
    tip: "用户全局删除需要复述 user_id 确认,不可逆。",
  },
  {
    icon: FileSearch,
    title: "审计日志",
    desc: "所有治理操作的完整时间轴记录,只读。",
    tip: "每条记录可展开查看操作详情 JSON。",
  },
  {
    icon: Settings,
    title: "系统配置",
    desc: "运行时配置只读视图,敏感值已脱敏。",
    tip: "搜索支持别名:搜「数据库」命中 PostgreSQL。",
  },
];

const KNOWLEDGE = [
  {
    q: "什么是「记忆」?",
    a: "AI 从对话中自动抽取的用户事实、偏好或事件。每条记忆有生命周期(有效/已取代/已删除/已抑制)。",
  },
  {
    q: "「来源反链」是什么?",
    a: "每条记忆都可以追溯到生成它的原始对话轮次,确保记忆有据可查。",
  },
  {
    q: "「冲突槽位」怎么理解?",
    a: "同类偏好归类位置(如 dietary=饮食偏好)。新记忆会替换槽位中的旧记忆,旧的标为「已取代」。",
  },
  {
    q: "「L2 / L3」是什么?",
    a: "L2 = 记忆的浓缩摘要(供模型快速理解上下文);L3 = 长期向量索引(供语义检索)。",
  },
];

export function OnboardingDialog() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (!localStorage.getItem(STORAGE_KEY)) {
      setOpen(true);
    }
  }, []);

  const close = () => {
    setOpen(false);
    localStorage.setItem(STORAGE_KEY, "1");
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      onClick={close}
    >
      <div
        className="relative max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-3xl border border-[#ebe7df] bg-background shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          onClick={close}
          aria-label="关闭引导"
          className="absolute right-4 top-4 rounded-full p-1.5 text-foreground-muted hover:bg-background-muted transition-colors"
        >
          <X className="size-4" />
        </button>

        <div className="p-8 pb-4">
          <p className="section-label">WELCOME TO THINKBACK</p>
          <h2 className="mt-2 text-3xl font-semibold tracking-tight">
            记忆治理台 · 快速上手
          </h2>
          <p className="mt-2 text-sm text-foreground-muted leading-relaxed">
            Thinkback 帮你管理 AI 从对话中自动抽取的「记忆」。以下是 6 个核心页面和你需要知道的概念。
          </p>
        </div>

        {/* 步骤 0:页面导览 */}
        {step === 0 ? (
          <div className="px-8 pb-6">
            <div className="grid grid-cols-2 gap-3">
              {PAGES.map((page) => (
                <div
                  key={page.title}
                  className="rounded-2xl border border-[#ebe7df] p-4 space-y-2"
                >
                  <div className="flex items-center gap-2">
                    <page.icon aria-hidden="true" className="size-4 text-foreground-emphasis" />
                    <p className="text-sm font-semibold text-foreground-intense">{page.title}</p>
                  </div>
                  <p className="text-xs text-foreground-muted leading-relaxed">{page.desc}</p>
                  <p className="text-[11px] text-foreground-soft leading-relaxed border-l-2 border-[#e8e2d4] pl-2">
                    {page.tip}
                  </p>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {/* 步骤 1:知识卡 */}
        {step === 1 ? (
          <div className="px-8 pb-6 space-y-3">
            {KNOWLEDGE.map((k) => (
              <div key={k.q} className="rounded-2xl border border-[#ebe7df] p-4">
                <p className="text-sm font-semibold text-foreground-intense">{k.q}</p>
                <p className="mt-1 text-xs text-foreground-muted leading-relaxed">{k.a}</p>
              </div>
            ))}
          </div>
        ) : null}

        {/* 步骤 2:快捷键 */}
        {step === 2 ? (
          <div className="px-8 pb-6 space-y-3">
            <div className="rounded-2xl border border-[#ebe7df] p-4">
              <p className="text-sm font-semibold text-foreground-intense mb-2">键盘快捷键</p>
              <div className="space-y-1.5 text-xs text-foreground-muted">
                {[
                  ["⌘K / Ctrl+K", "命令面板（搜索 + 跳转）"],
                  ["?", "打开快捷键列表"],
                  ["j / ↓", "列表中下移"],
                  ["k / ↑", "列表中上移"],
                  ["Enter", "打开选中项详情"],
                  ["x / Space", "选中/取消选中（记忆浏览器）"],
                  ["Esc", "关闭弹窗/清除选中"],
                ].map(([key, desc]) => (
                  <div key={key} className="flex items-center gap-3">
                    <kbd className="inline-flex min-w-[80px] justify-center rounded-md border border-[#ebe7df] bg-background-muted px-2 py-0.5 font-mono text-[10px]">
                      {key}
                    </kbd>
                    <span>{desc}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {/* 底部操作 */}
        <div className="flex items-center justify-between border-t border-[#ebe7df] px-8 py-4">
          <div className="flex gap-1.5">
            {[0, 1, 2].map((i) => (
              <button
                key={i}
                onClick={() => setStep(i)}
                aria-label={`步骤 ${i + 1}`}
                className={`h-1.5 rounded-full transition-colors ${
                  i === step ? "w-6 bg-foreground-intense" : "w-1.5 bg-border"
                }`}
              />
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={close}>
              跳过
            </Button>
            {step < 2 ? (
              <Button size="sm" onClick={() => setStep((s) => s + 1)}>
                下一步 <ArrowRight aria-hidden="true" className="size-3.5" />
              </Button>
            ) : (
              <Button size="sm" onClick={close}>
                开始使用
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** 在 layout 中调用:重新打开引导 */
export function openOnboarding() {
  localStorage.removeItem(STORAGE_KEY);
  window.dispatchEvent(new CustomEvent("open-onboarding"));
}

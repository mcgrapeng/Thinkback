import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StaleIndicator } from "./stale-indicator";

afterEach(() => {
  vi.useRealTimers();
});

describe("StaleIndicator", () => {
  it("无时间戳不渲染", () => {
    const { container } = render(<StaleIndicator updatedAt={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("30 秒内为新鲜态，超时转 warning", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-04T10:00:00Z"));
    render(<StaleIndicator updatedAt={Date.now()} />);

    expect(screen.getByText(/just now/)).toBeInTheDocument();
    expect(screen.getByText(/just now/)).toHaveClass("text-foreground-muted");

    act(() => {
      vi.advanceTimersByTime(31_000);
    });

    const stale = screen.getByText(/31s ago/);
    expect(stale).toHaveClass("text-warning");
  });

  it("自定义前缀", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-04T10:00:00Z"));
    render(<StaleIndicator updatedAt={Date.now()} prefix="updated" />);
    expect(screen.getByText(/updated/)).toBeInTheDocument();
  });
});

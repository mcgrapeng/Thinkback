import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EmptyState } from "./empty-state";

describe("EmptyState", () => {
  it("渲染标题与描述", () => {
    render(<EmptyState title="还没有数据" description="先去创建一条" />);
    expect(screen.getByRole("heading", { name: "还没有数据" })).toBeInTheDocument();
    expect(screen.getByText("先去创建一条")).toBeInTheDocument();
  });

  it("error 变体展示可折叠详情文本", () => {
    render(
      <EmptyState variant="error" title="加载失败" detail="HTTP 500 from /admin/api/keys" />,
    );
    expect(screen.getByText("HTTP 500 from /admin/api/keys")).toHaveClass("text-error");
    expect(screen.getByRole("heading", { name: "加载失败" })).toBeInTheDocument();
  });

  it("操作按钮触发回调", () => {
    const onClick = vi.fn();
    render(<EmptyState title="空" action={{ label: "重试", onClick }} />);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

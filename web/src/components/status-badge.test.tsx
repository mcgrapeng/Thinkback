import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "./status-badge";

describe("StatusBadge", () => {
  it("状态映射中文标签与语义色", () => {
    render(<StatusBadge status="ACTIVE" />);
    expect(screen.getByText("有效")).toBeInTheDocument();

    render(<StatusBadge status="running" />);
    expect(screen.getByText("运行中")).toBeInTheDocument();
  });

  it("DELETED 加删除线区分", () => {
    render(<StatusBadge status="DELETED" />);
    const badge = screen.getByText("已删除");
    expect(badge).toHaveClass("line-through");
  });

  it("未知状态原样透传", () => {
    render(<StatusBadge status="mystery" />);
    expect(screen.getByText("mystery")).toBeInTheDocument();
  });
});

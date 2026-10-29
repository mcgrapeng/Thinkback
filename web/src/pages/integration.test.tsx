/** 集成页冒烟：当前契约参考是**只读**的，不再有任何 API Key 管理写操作。
 *  断言重点：业务/管理端点清单、信任边界说明、异步写入与擦除语义都在页面上。 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { IntegrationPage } from "./integration";

function renderPage(): void {
  render(<IntegrationPage />);
}

describe("IntegrationPage", () => {
  it("展示当前业务端点，且不再出现已下线的 v1 接口", () => {
    renderPage();

    expect(screen.getByText("业务端点")).toBeInTheDocument();
    expect(screen.getAllByText("/v2/memory/events").length).toBeGreaterThan(0);
    expect(screen.getAllByText("/v2/memory/recall").length).toBeGreaterThan(0);
    expect(screen.getAllByText("/v2/memory/erase").length).toBeGreaterThan(0);
    expect(screen.queryByText("/v1/memory/append")).not.toBeInTheDocument();
    expect(screen.queryByText("API Keys")).not.toBeInTheDocument();
  });

  it("说明信任边界是网络边界，管理端点同样免鉴权", () => {
    renderPage();

    expect(screen.getAllByText("业务面免鉴权").length).toBeGreaterThan(0);
    expect(screen.getAllByText("管理面免鉴权").length).toBeGreaterThan(0);
    expect(screen.getAllByText("信任边界 = 网络边界").length).toBeGreaterThan(0);
    expect(screen.getByText("管理端点")).toBeInTheDocument();
    expect(screen.getAllByText("/admin/api/overview").length).toBeGreaterThan(0);
    expect(screen.queryByText("ADMIN_TOKEN")).not.toBeInTheDocument();
  });

  it("写明异步写入返回 accepted、擦除后同一 round 不可重写", () => {
    renderPage();

    expect(screen.getByText("写入语义与幂等")).toBeInTheDocument();
    expect(screen.getByText('status="accepted"')).toBeInTheDocument();
    expect(screen.getByText("擦除后不可重写")).toBeInTheDocument();
    expect(screen.getByText("409")).toBeInTheDocument();
  });

  it("列出标准化错误码", () => {
    renderPage();

    expect(screen.getByText("TB-1005")).toBeInTheDocument();
    expect(screen.getByText("TB-1007")).toBeInTheDocument();
    expect(screen.queryByText("TB-1002")).not.toBeInTheDocument();
  });
});

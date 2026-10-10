import { describe, expect, it } from "vitest";

import { formatTime } from "./utils";

describe("formatTime", () => {
  it("格式化有效时间戳为 zh-CN 展示", () => {
    const out = formatTime("2026-05-04T10:00:03Z");
    expect(out).toContain("2026");
    expect(out).not.toBe("—");
  });

  it("接受 Date 与毫秒时间戳", () => {
    expect(formatTime(new Date("2026-05-04T10:00:03Z"))).not.toBe("—");
    expect(formatTime(1778000000000)).not.toBe("—");
  });

  it("无效输入统一返回占位符", () => {
    expect(formatTime(null)).toBe("—");
    expect(formatTime(undefined)).toBe("—");
    expect(formatTime("")).toBe("—");
    expect(formatTime("not-a-date")).toBe("—");
  });
});

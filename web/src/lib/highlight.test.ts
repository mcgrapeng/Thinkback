import { describe, expect, it } from "vitest";

import { findHighlightSegments, longestCommonSpan } from "./highlight";

function joinSegments(segments: { value: string }[]): string {
  return segments.map((segment) => segment.value).join("");
}

describe("longestCommonSpan", () => {
  it("返回中英混合最长公共连续段", () => {
    expect(longestCommonSpan("我叫阿鹏，住在沈阳", "用户叫阿鹏")).toBe("叫阿鹏");
    expect(longestCommonSpan("hello world", "say hello")).toBe("hello");
  });

  it("无公共片段返回空串", () => {
    expect(longestCommonSpan("abc", "xyz")).toBe("");
  });
});

describe("findHighlightSegments", () => {
  it("单命中：match 夹在 text 段之间且文本拼接完整", () => {
    const segments = findHighlightSegments("我不喜欢被催睡觉", "催睡觉不好");
    expect(joinSegments(segments)).toBe("我不喜欢被催睡觉");
    expect(segments.some((segment) => segment.kind === "match")).toBe(true);
  });

  it("命中文本不重复（split-with-capture 回归）", () => {
    const content = "催睡觉催睡觉";
    const segments = findHighlightSegments(content, "催睡觉");
    expect(joinSegments(segments)).toBe(content);
  });

  it("零匹配返回单个 text 段", () => {
    const segments = findHighlightSegments("完全无关的内容", "xyz");
    expect(segments).toEqual([{ kind: "text", value: "完全无关的内容" }]);
  });

  it("空原文返回空 text 段", () => {
    expect(findHighlightSegments("", "记忆")).toEqual([{ kind: "text", value: "" }]);
  });
});

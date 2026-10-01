/** highlight 算法自检：纯 node + node:assert，零依赖。
 *  跑：node --experimental-strip-types web/src/lib/highlight.check.ts
 *
 *  覆盖三类回归：
 *  1) 单命中：mark 在 text/text 之间，文本拼接完整
 *  2) 修复前 bug 回归：split-with-capture 旧实现会把匹配文本重复两次
 *  3) 零匹配：返回单个 text 段（不应有 mark）
 *  4) 空原文 / 空记忆文本：边界
 */
import assert from "node:assert/strict";
import { findHighlightSegments } from "./highlight.ts";

function join(segments: { value: string }[]): string {
  return segments.map((s) => s.value).join("");
}

// 1) 中文单命中
{
  const r = findHighlightSegments("用户喜欢吃苹果和香蕉", "苹果");
  assert.equal(join(r), "用户喜欢吃苹果和香蕉");
  assert.equal(r.filter((s) => s.kind === "match").length, 1);
  assert.equal(r.find((s) => s.kind === "match")?.value, "苹果");
}

// 2) 原 bug 回归：旧实现会把命中值放进 parts 与 match 各一份 → 重复 + 错位
{
  const r = findHighlightSegments("abc 123 def", "123");
  assert.equal(join(r), "abc 123 def", "no duplication");
  const matches = r.filter((s) => s.kind === "match");
  assert.equal(matches.length, 1, "exactly one match segment");
  assert.equal(matches[0]?.value, "123");
}

// 3) 无匹配
{
  const r = findHighlightSegments("hello world", "xyz");
  assert.equal(r.length, 1);
  assert.equal(r[0]?.kind, "text");
  assert.equal(r[0]?.value, "hello world");
}

// 4) 边界：空 memoryText（应跳过公共子串，token 全空 → 单段 text）
{
  const r = findHighlightSegments("hello world", "");
  assert.equal(r.length, 1);
  assert.equal(r[0]?.kind, "text");
}

// 5) 边界：空 content
{
  const r = findHighlightSegments("", "something");
  assert.equal(r.length, 1);
  assert.equal(r[0]?.kind, "text");
  assert.equal(r[0]?.value, "");
}

// 6) 早停优化正确性：200 字记忆 + 2k 字原文，含一处 5 字符公共段
{
  const mem = "用户".repeat(100);                                  // 200 chars, 全是「用户」
  const content = ("用户喜欢吃苹果 " + "无关".repeat(200)).slice(0, 2000);
  const r = findHighlightSegments(content, mem);
  assert.equal(r.map((s) => s.value).join(""), content, "text roundtrip");
  const matches = r.filter((s) => s.kind === "match");
  assert.ok(matches.length > 0, "找到至少一处匹配");
  assert.ok(matches.some((m) => m.value.includes("用户")), "匹配包含「用户」");
}

console.log("highlight selfcheck: 6/6 OK");

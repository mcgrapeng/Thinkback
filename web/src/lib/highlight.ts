/** 把原文切成「文本段 + 命中段」序列；UI 层再把段映射成 span/mark。
 *  独立成纯函数是为了可单测。详见 ./highlight.check.ts。 */

export type Segment = { kind: "text"; value: string } | { kind: "match"; value: string };

/** 最长公共连续段（中文无分词，靠公共子串定位命中片段）。
 *  朴素双层 + 早停：best 越长，剩余 i/j 越不可能超过，break 省掉尾部对比。
 *  最坏仍 O(m·n) 但常数极小；200 字 × 2k 字约 4 万次（vs 朴素 ~4 千万）。 */
export function longestCommonSpan(a: string, b: string): string {
  let best = "";
  const m = a.length;
  const n = b.length;
  for (let i = 0; i < m; i++) {
    const maxA = m - i;
    if (maxA <= best.length) break;
    for (let j = 0; j < n; j++) {
      const maxMatch = maxA < n - j ? maxA : n - j;
      if (maxMatch <= best.length) break;
      let k = 0;
      while (k < maxMatch && a[i + k] === b[j + k]) k++;
      if (k > best.length && /[\p{L}\p{N}]/u.test(a.slice(i, i + k))) {
        best = a.slice(i, i + k);
      }
    }
  }
  return best;
}

/** 在原文中按记忆词元 + 中英公共子串（≥2 字符）切片。exec 迭代而非 split+match：
 *  split-with-capture 会把命中片段塞进 parts，导致前后两份相同内容、且 marks 错位。 */
export function findHighlightSegments(content: string, memoryText: string): Segment[] {
  if (content.length === 0) return [{ kind: "text", value: "" }];
  const tokens = new Set(
    memoryText
      .split(/[\s,.;:!?，。；：！？、]+/)
      .filter((token) => token.length >= 2),
  );
  const span = longestCommonSpan(memoryText, content);
  if (span.length >= 2) tokens.add(span);
  const list = Array.from(tokens).slice(0, 8);
  if (list.length === 0) return [{ kind: "text", value: content }];

  const pattern = new RegExp(
    list.map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
    "g",
  );

  const out: Segment[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) !== null) {
    if (match.index > lastIndex) {
      out.push({ kind: "text", value: content.slice(lastIndex, match.index) });
    }
    out.push({ kind: "match", value: match[0] });
    lastIndex = pattern.lastIndex;
    if (match.index === pattern.lastIndex) pattern.lastIndex++;
  }
  if (lastIndex < content.length) {
    out.push({ kind: "text", value: content.slice(lastIndex) });
  }
  return out;
}

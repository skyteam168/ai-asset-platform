// 从源码收集 t("…") 的键（供字典覆盖测试与补译脚本使用；不进入构建产物）
export const T_CALL = /\bt\(("(?:[^"\\]|\\.)*")/g;

export function collectKeys(sources: string[]): string[] {
  const keys = new Set<string>();
  for (const src of sources) {
    for (const m of src.matchAll(T_CALL)) keys.add(JSON.parse(m[1]) as string);
  }
  return [...keys].sort();
}

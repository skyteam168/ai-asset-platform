import { App } from "antd";
import { useCallback, useEffect, useState } from "react";
import { describeError } from "./api";

/** 加载一个接口；返回数据、加载状态、错误与刷新函数。 */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    loader()
      .then(setData)
      .catch((e) => setError(describeError(e)))
      .finally(() => setLoading(false));
  }, deps); // 依赖由调用方显式给出
  useEffect(reload, [reload]);
  return { data, loading, error, reload };
}

/** 执行一个操作：成功提示、失败显示后端给出的原因。 */
export function useAction() {
  const { message } = App.useApp();
  return useCallback(
    async <T,>(fn: () => Promise<T>, success?: string): Promise<T | undefined> => {
      try {
        const res = await fn();
        if (success) message.success(success);
        return res;
      } catch (e) {
        message.error(describeError(e), 6);
        return undefined;
      }
    },
    [message],
  );
}

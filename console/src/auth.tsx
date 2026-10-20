import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, get, session, setUnauthorizedHandler } from "./api";
import type { Me, Role } from "./types";

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (loginName: string, password: string, totp: string) => Promise<void>;
  logout: () => Promise<void>;
  has: (...roles: Role[]) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(Boolean(session.get()));

  const clear = useCallback(() => {
    session.clear();
    setMe(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(clear);
    if (!session.get()) return;
    get<Me>("/me")
      .then(setMe)
      .catch(clear)
      .finally(() => setLoading(false));
  }, [clear]);

  const login = useCallback(async (loginName: string, password: string, totp: string) => {
    const res = await api<{ session_token: string }>("POST", "/auth/login", {
      login_name: loginName,
      password,
      totp,
    });
    session.set(res.session_token);
    setMe(await get<Me>("/me"));
  }, []);

  const logout = useCallback(async () => {
    try {
      await api("POST", "/auth/logout");
    } finally {
      clear();
    }
  }, [clear]);

  const value = useMemo<AuthState>(
    () => ({
      me,
      loading,
      login,
      logout,
      has: (...roles) => Boolean(me?.roles.some((r) => roles.includes(r.role))),
    }),
    [me, loading, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

import { useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { api, refreshSession, setAccessToken, setAuthRealm, setSessionExpiredHandler, unwrap, type Me } from '../api/client';

export type Capability =
  | 'school.view' | 'school.manage' | 'staff.view' | 'staff.manage' | 'audit.view'
  | 'students.view' | 'students.manage' | 'fees.view' | 'fees.manage' | 'invoices.issue'
  | 'payments.view' | 'payments.record' | 'payments.approve' | 'payments.reverse' | 'payments.setup'
  | 'reports.view' | 'books.reopen' | 'debts.write_off' | 'messaging.manage' | 'platform.manage';

interface AuthState {
  user: Me | null;
  loading: boolean;
  /** Returns an MFA ticket when a second step is needed, otherwise null. */
  login: (email: string, password: string) => Promise<string | null>;
  completeMfa: (ticket: string, code: string, recovery?: boolean) => Promise<void>;
  refreshUser: () => Promise<void>;
  /** Head-office staff of a school group: work in another branch. Nothing cached from the last one is kept. */
  switchBranch: (schoolId: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (capability: Capability) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const queryClient = useQueryClient();

  const signOutLocally = useCallback(() => {
    setAccessToken(null);
    setUser(null);
    queryClient.clear(); // nothing from one user's session may show for the next
  }, [queryClient]);

  useEffect(() => {
    setAuthRealm('staff');
    setSessionExpiredHandler(signOutLocally);
    // Restore the session from the refresh cookie after a page reload
    refreshSession()
      .then((session) => setUser(session?.user ?? null))
      .finally(() => setLoading(false));
  }, [signOutLocally]);

  const login = useCallback(async (email: string, password: string) => {
    const result = unwrap(await api.POST('/api/v1/auth/login', { body: { email, password } }));
    if (result.status === 'mfa_required') return result.mfa_ticket;
    setAccessToken(result.access_token);
    setUser(result.user);
    return null;
  }, []);

  const completeMfa = useCallback(async (ticket: string, code: string, recovery = false) => {
    const body = recovery ? { mfa_ticket: ticket, recovery_code: code } : { mfa_ticket: ticket, totp_code: code };
    const result = unwrap(await api.POST('/api/v1/auth/login/mfa', { body }));
    setAccessToken(result.access_token);
    setUser(result.user);
  }, []);

  const refreshUser = useCallback(async () => {
    setUser(unwrap(await api.GET('/api/v1/auth/me')));
  }, []);

  const switchBranch = useCallback(
    async (schoolId: string) => {
      const me = unwrap(await api.POST('/api/v1/auth/branch', { body: { school_id: schoolId } }));
      queryClient.clear(); // one branch's figures must never show under another's name
      setUser(me);
    },
    [queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await api.POST('/api/v1/auth/logout');
    } finally {
      signOutLocally();
    }
  }, [signOutLocally]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      login,
      completeMfa,
      refreshUser,
      switchBranch,
      logout,
      can: (capability) => !!user?.capabilities.includes(capability),
    }),
    [user, loading, login, completeMfa, refreshUser, switchBranch, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}

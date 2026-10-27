import {
  Banknote,
  Building2,
  CalendarDays,
  ChartColumn,
  CreditCard,
  FileText,
  GraduationCap,
  History,
  Landmark,
  Layers,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  Network,
  Plug,
  Receipt,
  ReceiptText,
  Search,
  TriangleAlert,
  Tags,
  UserCog,
  Users,
  Wallet,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';

import { useAuth, type Capability } from '../auth/AuthContext';
import { BRAND } from '../lib/brand';
import { ROLE_LABELS } from '../lib/format';
import { Loading } from '../lib/ui';
import { Account } from './Account';
import { AuditLog } from './AuditLog';
import { Login } from './auth/Login';
import { ForgotPassword, SetPassword } from './auth/PasswordPages';
import { Billing } from './billing/Billing';
import { InvoiceDetail } from './billing/InvoiceDetail';
import { Messages } from './messages/Messages';
import { Reports } from './reports/Reports';
import { Dashboard } from './Dashboard';
import { FeeSetup } from './fees/FeeSetup';
import { GetStarted } from './GetStarted';
import { Expenses } from './expenses/Expenses';
import { CashBook } from './payments/CashBook';
import { Payments } from './payments/Payments';
import { PaymentSettings } from './payments/PaymentSettings';
import { BranchSwitcher, ChooseBranch } from './Branch';
import { PlatformBilling } from './platform/Billing';
import { PlatformGroupDetail, PlatformGroups } from './platform/Groups';
import { PlatformLookup } from './platform/Lookup';
import { PlatformOverview } from './platform/Overview';
import { PlatformPlans } from './platform/Plans';
import { PlatformProblems } from './platform/Problems';
import { PlatformProviders } from './platform/Providers';
import { PlatformSchoolDetail } from './platform/SchoolDetail';
import { PlatformSchools } from './platform/Schools';
import { SchoolProfile } from './SchoolProfile';
import { Calendar } from './setup/Calendar';
import { Classes } from './setup/Classes';
import { ImportStudents } from './students/ImportStudents';
import { StudentDetail } from './students/StudentDetail';
import { Students } from './students/Students';
import { StaffPage } from './Staff';
import { Subscription } from './Subscription';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  capability: Capability;
  setup?: boolean; // listed under "Setup" in the sidebar
}

const SCHOOL_NAV: NavItem[] = [
  { to: '/console', label: 'Overview', icon: LayoutDashboard, capability: 'school.view' },
  { to: '/console/students', label: 'Students', icon: Users, capability: 'students.view' },
  { to: '/console/billing', label: 'Billing', icon: FileText, capability: 'fees.view' },
  { to: '/console/payments', label: 'Payments', icon: Wallet, capability: 'payments.view' },
  { to: '/console/cash-book', label: 'Cash book', icon: Banknote, capability: 'payments.view' },
  { to: '/console/expenses', label: 'Expenses', icon: ReceiptText, capability: 'payments.view' },
  { to: '/console/reports', label: 'Reports', icon: ChartColumn, capability: 'reports.view' },
  { to: '/console/messages', label: 'Messages', icon: MessageSquare, capability: 'messaging.manage' },
  { to: '/console/fees', label: 'Fees', icon: Tags, capability: 'fees.view', setup: true },
  { to: '/console/classes', label: 'Classes', icon: GraduationCap, capability: 'school.view', setup: true },
  { to: '/console/calendar', label: 'Calendar', icon: CalendarDays, capability: 'school.view', setup: true },
  { to: '/console/staff', label: 'Staff', icon: UserCog, capability: 'staff.view', setup: true },
  { to: '/console/school', label: 'School profile', icon: Building2, capability: 'school.view', setup: true },
  { to: '/console/payment-settings', label: 'Payment settings', icon: Landmark, capability: 'payments.view', setup: true },
  { to: '/console/subscription', label: 'Subscription', icon: CreditCard, capability: 'reports.view', setup: true },
  { to: '/console/audit', label: 'Activity log', icon: History, capability: 'audit.view', setup: true },
];

const PLATFORM_NAV: NavItem[] = [
  { to: '/console/platform', label: 'Overview', icon: LayoutDashboard, capability: 'platform.manage' },
  { to: '/console/platform/schools', label: 'Schools', icon: Building2, capability: 'platform.manage' },
  { to: '/console/platform/groups', label: 'School groups', icon: Network, capability: 'platform.manage' },
  { to: '/console/platform/billing', label: 'Billing', icon: Receipt, capability: 'platform.manage' },
  { to: '/console/platform/plans', label: 'Plans', icon: Layers, capability: 'platform.manage' },
  { to: '/console/platform/problems', label: 'Problems', icon: TriangleAlert, capability: 'platform.manage' },
  { to: '/console/platform/providers', label: 'Providers', icon: Plug, capability: 'platform.manage' },
  { to: '/console/platform/lookup', label: 'Support lookup', icon: Search, capability: 'platform.manage' },
  { to: '/console/platform/audit', label: 'Activity log', icon: History, capability: 'platform.manage' },
];

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join('');
}

export function ConsoleApp() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route path="forgot-password" element={<ForgotPassword />} />
      <Route path="set-password" element={<SetPassword />} />
      <Route element={<RequireSignIn />}>
        <Route element={<Layout />}>
          <Route element={<RequireBranch />}>
          <Route index element={<Home />} />
          <Route path="account" element={<Account />} />
          <Route path="get-started" element={<Guard capability="school.manage"><GetStarted /></Guard>} />
          <Route path="students" element={<Guard capability="students.view"><Students /></Guard>} />
          <Route path="students/import" element={<Guard capability="students.manage"><ImportStudents /></Guard>} />
          <Route path="students/:studentId" element={<Guard capability="students.view"><StudentDetail /></Guard>} />
          <Route path="billing" element={<Guard capability="fees.view"><Billing /></Guard>} />
          <Route path="invoices/:invoiceId" element={<Guard capability="fees.view"><InvoiceDetail /></Guard>} />
          <Route path="payments" element={<Guard capability="payments.view"><Payments /></Guard>} />
          <Route path="cash-book" element={<Guard capability="payments.view"><CashBook /></Guard>} />
          <Route path="expenses" element={<Guard capability="payments.view"><Expenses /></Guard>} />
          <Route path="reports" element={<Guard capability="reports.view"><Reports /></Guard>} />
          <Route path="messages" element={<Guard capability="messaging.manage"><Messages /></Guard>} />
          <Route path="payment-settings" element={<Guard capability="payments.view"><PaymentSettings /></Guard>} />
          <Route path="fees" element={<Guard capability="fees.view"><FeeSetup /></Guard>} />
          <Route path="classes" element={<Guard capability="school.view"><Classes /></Guard>} />
          <Route path="calendar" element={<Guard capability="school.view"><Calendar /></Guard>} />
          <Route path="staff" element={<Guard capability="staff.view"><StaffPage /></Guard>} />
          <Route path="audit" element={<Guard capability="audit.view"><AuditLog /></Guard>} />
          <Route path="school" element={<Guard capability="school.view"><SchoolProfile /></Guard>} />
          <Route path="subscription" element={<Guard capability="reports.view"><Subscription /></Guard>} />
          <Route path="platform" element={<Guard capability="platform.manage"><PlatformOverview /></Guard>} />
          <Route path="platform/schools" element={<Guard capability="platform.manage"><PlatformSchools /></Guard>} />
          <Route path="platform/schools/:schoolId" element={<Guard capability="platform.manage"><PlatformSchoolDetail /></Guard>} />
          <Route path="platform/billing" element={<Guard capability="platform.manage"><PlatformBilling /></Guard>} />
          <Route path="platform/plans" element={<Guard capability="platform.manage"><PlatformPlans /></Guard>} />
          <Route path="platform/providers" element={<Guard capability="platform.manage"><PlatformProviders /></Guard>} />
          <Route path="platform/problems" element={<Guard capability="platform.manage"><PlatformProblems /></Guard>} />
          <Route path="platform/lookup" element={<Guard capability="platform.manage"><PlatformLookup /></Guard>} />
          <Route path="platform/audit" element={<Guard capability="platform.manage"><AuditLog platform /></Guard>} />
          <Route path="platform/groups" element={<Guard capability="platform.manage"><PlatformGroups /></Guard>} />
          <Route path="platform/groups/:groupId" element={<Guard capability="platform.manage"><PlatformGroupDetail /></Guard>} />
          <Route path="*" element={<Navigate to="/console" replace />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  );
}

function RequireSignIn() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Loading />;
  if (!user) return <Navigate to="/console/login" replace state={{ from: location.pathname }} />;
  // Roles that must use two-factor sign-in can only reach their account page until they enrol
  if (user.mfa_enrolment_required && location.pathname !== '/console/account') {
    return <Navigate to="/console/account" replace />;
  }
  return <Outlet />;
}

/** Head-office staff work in one branch at a time: until they have one, only the chooser and their account. */
function RequireBranch() {
  const { user } = useAuth();
  const location = useLocation();
  if (user?.group && !user.school && location.pathname !== '/console/account') return <ChooseBranch />;
  return <Outlet />;
}

function Guard({ capability, children }: { capability: Capability; children: JSX.Element }) {
  const { can } = useAuth();
  return can(capability) ? children : <Navigate to="/console" replace />;
}

function Home() {
  const { can } = useAuth();
  return can('platform.manage') ? <Navigate to="/console/platform" replace /> : <Dashboard />;
}

function Layout() {
  const { user, can, logout } = useAuth();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => setNavOpen(false), [location.pathname]);
  if (!user) return null;
  const nav = [...SCHOOL_NAV, ...PLATFORM_NAV].filter((item) => can(item.capability));
  const groups = [
    { label: '', items: nav.filter((item) => !item.setup) },
    { label: 'Setup', items: nav.filter((item) => item.setup) },
  ].filter((group) => group.items.length > 0);
  const role = ROLE_LABELS[user.role] ?? user.role;
  return (
    <div className={`console${navOpen ? ' nav-open' : ''}`}>
      <div className="mobile-bar">
        <button type="button" className="icon-button" aria-label="Open the menu" onClick={() => setNavOpen(true)}>
          <Menu size={20} />
        </button>
        <strong>{user.school?.name ?? BRAND}</strong>
      </div>
      <div className="sidebar-backdrop" onClick={() => setNavOpen(false)} />
      <aside className="sidebar">
        <Link to="/console" className="side-brand">
          <span className="side-logo" aria-hidden="true">
            {BRAND[0]}
          </span>
          <span className="side-brand-text">
            <strong>{BRAND}</strong>
            {user.school && <span>{user.group ? user.group.name : user.school.name}</span>}
          </span>
        </Link>
        <BranchSwitcher />
        <nav aria-label="Main" className="side-nav">
          {!user.mfa_enrolment_required &&
            groups.map((group) => (
              <div key={group.label || 'main'}>
                {group.label && <div className="side-group-label">{group.label}</div>}
                {group.items.map(({ to, label, icon: Icon }) => (
                  <NavLink key={to} to={to} className="side-link" end={to === '/console' || to === '/console/platform'}>
                    <Icon size={18} strokeWidth={1.9} aria-hidden="true" />
                    {label}
                  </NavLink>
                ))}
              </div>
            ))}
        </nav>
        <div className="side-user">
          <NavLink to="/console/account" title="Your account">
            <span className="avatar" aria-hidden="true">
              {initials(user.full_name)}
            </span>
            <span className="side-user-text">
              <strong>{user.full_name}</strong>
              <span>{role}</span>
            </span>
          </NavLink>
          <button type="button" className="icon-button" aria-label="Sign out" title="Sign out" onClick={() => void logout()}>
            <LogOut size={18} />
          </button>
        </div>
      </aside>
      <main className="page">
        <Outlet />
      </main>
    </div>
  );
}

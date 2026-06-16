import { Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, FileText, Library, History, Settings, Hexagon, PanelLeftClose } from 'lucide-react';

export default function Sidebar({ isOpen, toggleSidebar }) {
  const location = useLocation();

  const navItems = [
    { label: 'Dashboard', path: '/', icon: LayoutDashboard },
    { label: 'Active Bids', path: '/bids', icon: FileText },
    { label: 'Capability Library', path: '/capabilities', icon: Library },
    { label: 'Bidding History', path: '/bid-history', icon: History },
    { label: 'Settings', path: '/settings', icon: Settings },
  ];

  if (!isOpen) return null;

  return (
    <div className="w-[240px] h-full bg-brand-surface border-r border-brand-border flex flex-col shrink-0 transition-all duration-300">
      <div className="p-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-brand-primary/20 flex items-center justify-center text-brand-primary">
            <Hexagon className="w-5 h-5 fill-current" />
          </div>
          <span className="text-brand-heading font-bold text-lg tracking-tight">BidForge AI</span>
        </div>
        <button 
          onClick={toggleSidebar} 
          className="text-brand-muted hover:text-brand-heading transition-colors"
          title="Collapse Sidebar"
        >
          <PanelLeftClose className="w-5 h-5" />
        </button>
      </div>

      <nav className="flex-1 px-4 space-y-1">
        {navItems.map((item) => {
          const isActive = location.pathname === item.path;
          return (
            <Link
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
                isActive 
                  ? 'bg-brand-primary/10 text-brand-primary relative before:absolute before:left-0 before:top-2 before:bottom-2 before:w-1 before:bg-brand-primary before:rounded-full' 
                  : 'text-brand-muted hover:text-brand-body hover:bg-brand-elevated'
              }`}
            >
              <item.icon className={`w-5 h-5 ${isActive ? 'text-brand-primary' : 'text-brand-muted'}`} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="p-6">
        <div className="text-xs font-medium text-brand-muted bg-brand-elevated px-3 py-2 rounded-lg border border-brand-border text-center">
          CUST Hackathon 2026
        </div>
      </div>
    </div>
  );
}

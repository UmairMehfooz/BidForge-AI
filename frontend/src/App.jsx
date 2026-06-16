import { useState } from 'react';
import { Routes, Route } from 'react-router-dom';
import { Menu } from 'lucide-react';
import Sidebar from './components/Sidebar';
import Dashboard from './pages/Dashboard';
import Workspace from './pages/Workspace';
import ProposalEditor from './pages/ProposalEditor';
import BidHistory from './pages/BidHistory';
import ActiveBids from './pages/ActiveBids';
import CapabilityLibrary from './pages/CapabilityLibrary';
import Settings from './pages/Settings';

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);

  return (
    <div className="flex h-screen w-full bg-brand-bg text-brand-body overflow-hidden font-sans relative">
      <Sidebar isOpen={sidebarOpen} toggleSidebar={() => setSidebarOpen(!sidebarOpen)} />
      
      <div className="flex-1 overflow-y-auto relative">
        {/* Hamburger button when sidebar is closed */}
        {!sidebarOpen && (
          <button 
            onClick={() => setSidebarOpen(true)}
            className="absolute top-4 left-4 z-50 w-10 h-10 bg-brand-surface border border-brand-border rounded-lg flex items-center justify-center text-brand-muted hover:text-brand-heading hover:border-brand-primary/50 transition-colors shadow-lg"
          >
            <Menu className="w-5 h-5" />
          </button>
        )}
        
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/workspace/:id" element={<Workspace />} />
          <Route path="/workspace/:id/proposal" element={<ProposalEditor />} />
          <Route path="/bids" element={<ActiveBids />} />
          <Route path="/bid-history" element={<BidHistory />} />
          <Route path="/capabilities" element={<CapabilityLibrary />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </div>
    </div>
  );
}

export default App;

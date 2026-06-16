export const MOCK_WORKSPACES = [
  {
    id: "ws-1",
    name: "Ministry of IT — Cloud Services RFP 2025",
    type: "IT Services",
    status: "Draft Ready",
    compliance: 82,
    score: 73,
    decision: "GO",
  },
  {
    id: "ws-2",
    name: "NHA Road Construction Tender Q2 2025",
    type: "Construction",
    status: "Matched",
    compliance: 61,
    score: 54,
    decision: "CONDITIONAL",
  },
  {
    id: "ws-3",
    name: "Pakistan Post Logistics Modernization RFQ",
    type: "Logistics",
    status: "Exported",
    compliance: 91,
    score: 88,
    decision: "GO",
  }
];

export const MOCK_REQUIREMENTS = [
  { id: "req-1", ref: "4.1", text: "Vendor must hold valid PPRA registration", type: "Mandatory" },
  { id: "req-2", ref: "4.2", text: "Minimum 5 years experience in cloud infrastructure", type: "Mandatory" },
  { id: "req-3", ref: "4.3", text: "ISO 27001 certification required", type: "Mandatory" },
  { id: "req-4", ref: "5.1", text: "Technical proposal max 50 pages", type: "Submission" },
  { id: "req-5", ref: "5.2", text: "Submission deadline: 30 June 2025", type: "Deadline", chip: "30 June 2025" },
  { id: "req-6", ref: "6.1", text: "Describe your cloud migration methodology", type: "Question" },
  { id: "req-7", ref: "6.2", text: "Provide 3 reference projects from government sector", type: "Question" },
];

export const MOCK_COMPLIANCE = [
  { reqId: "req-1", status: "PASS", note: "Matched: PITB Digital Infrastructure 2022" },
  { reqId: "req-2", status: "PASS", note: "Matched: FBR Tax System Modernization 2021, 7 years exp" },
  { reqId: "req-3", status: "FAIL", gap: "Gap: No ISO 27001 found. Closest: ISO 9001 from 2020" },
  { reqId: "req-4", status: "PASS", note: "Acknowledged." },
  { reqId: "req-5", status: "PASS", note: "Deadline noted." },
  { reqId: "req-6", status: "PASS", note: "Matched: Agile cloud migration methodology, PITB 2022" },
  { reqId: "req-7", status: "PARTIAL", gap: "Only 2 government references found, 3 required" },
];

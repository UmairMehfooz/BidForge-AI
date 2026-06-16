"""Append 10 software-company capability records (CAP-051..CAP-060) to both
capability_library.json and capability_library_enriched.json. Idempotent —
skips ids that already exist."""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "app" / "data"
FILES = [DATA_DIR / "capability_library.json",
         DATA_DIR / "capability_library_enriched.json"]

NEW_CAPS = [
    {
        "id": "CAP-051",
        "domain": "Custom Software Development",
        "project_title": "National Tax Filing Portal",
        "summary": "Designed and built a custom web-based tax filing portal for a federal government revenue authority using Python/Django and React, handling 1.2 million registered filers with peak loads of 40,000 concurrent sessions during filing deadlines. Delivered full SDLC including requirements workshops, UAT, and a 2-year support contract, reducing average filing time from 45 to 12 minutes.",
        "certification": "ISO 9001",
        "year_completed": 2024,
        "contract_value": "PKR 95M",
        "duration_months": 18,
        "client_type": "Federal Govt",
    },
    {
        "id": "CAP-052",
        "domain": "Mobile App Development",
        "project_title": "Citizen Services Mobile App",
        "summary": "Developed cross-platform iOS and Android citizen services application (Flutter) for a provincial government, integrating 14 departmental services including license renewal, bill payment, and complaint tracking. Achieved 800,000 downloads in the first year with a 4.4-star average rating and biometric login compliant with national digital identity standards.",
        "certification": None,
        "year_completed": 2023,
        "contract_value": "PKR 38M",
        "duration_months": 12,
        "client_type": "Provincial Govt",
    },
    {
        "id": "CAP-053",
        "domain": "Cloud Migration",
        "project_title": "Banking Core Systems Cloud Migration",
        "summary": "Migrated a private bank's core workloads from on-premises data centers to a hybrid AWS/Azure cloud architecture: 220 virtual machines, 14 databases, and a containerized microservices middleware layer with zero data loss and under 4 hours of cumulative downtime. Implemented infrastructure-as-code (Terraform), cutting infrastructure costs by 34% annually.",
        "certification": "ISO 27001",
        "year_completed": 2024,
        "contract_value": "PKR 120M",
        "duration_months": 14,
        "client_type": "Private Sector",
    },
    {
        "id": "CAP-054",
        "domain": "DevOps & Automation",
        "project_title": "Enterprise CI/CD Platform Rollout",
        "summary": "Implemented an enterprise DevOps platform (GitLab CI, Kubernetes, ArgoCD) for an international telecom software vendor, standardizing build and release pipelines across 35 development teams. Reduced average release cycle from 6 weeks to 4 days and production rollback incidents by 71% through automated testing gates and blue-green deployments.",
        "certification": "CMMI L3",
        "year_completed": 2025,
        "contract_value": "PKR 55M",
        "duration_months": 10,
        "client_type": "International",
    },
    {
        "id": "CAP-055",
        "domain": "Data Analytics & BI",
        "project_title": "National Health Data Warehouse & Dashboards",
        "summary": "Built a national health data warehouse and business-intelligence layer (PostgreSQL, Apache Airflow, Power BI) for a federal health ministry, consolidating reporting from 600+ facilities into automated daily dashboards. Enabled disease-surveillance reporting that cut outbreak response time from 14 days to 48 hours.",
        "certification": "ISO 27001",
        "year_completed": 2023,
        "contract_value": "PKR 65M",
        "duration_months": 16,
        "client_type": "Federal Govt",
    },
    {
        "id": "CAP-056",
        "domain": "AI & Machine Learning",
        "project_title": "Document Intelligence & OCR Automation",
        "summary": "Delivered an AI document-processing system for an international insurance group: OCR plus transformer-based extraction over 25 document types in English and Urdu, processing 90,000 documents per month at 96.2% field-level accuracy. Reduced manual data-entry headcount requirements by 60% and claim turnaround from 9 days to 36 hours.",
        "certification": "ISO 9001",
        "year_completed": 2025,
        "contract_value": "PKR 48M",
        "duration_months": 11,
        "client_type": "International",
    },
    {
        "id": "CAP-057",
        "domain": "E-Commerce Platforms",
        "project_title": "B2B Marketplace Platform Build",
        "summary": "Engineered a B2B e-commerce marketplace (microservices on Node.js/React, Stripe and local payment-gateway integration) for a private retail conglomerate, onboarding 2,400 wholesale vendors and processing PKR 1.8B in gross merchandise value in its first year. Included vendor portal, logistics integration, and real-time inventory sync across 3 warehouses.",
        "certification": None,
        "year_completed": 2024,
        "contract_value": "PKR 72M",
        "duration_months": 15,
        "client_type": "Private Sector",
    },
    {
        "id": "CAP-058",
        "domain": "Fintech Solutions",
        "project_title": "Digital Wallet & Payments Backend",
        "summary": "Built the core payments backend for a licensed digital wallet operator: double-entry ledger, PCI-DSS-aligned card tokenization, 1LINK and Raast integration, and fraud-scoring rules engine sustaining 1,100 transactions per second in load testing. Platform now serves 2.5 million wallet accounts with 99.95% uptime since launch.",
        "certification": "ISO 27001",
        "year_completed": 2025,
        "contract_value": "PKR 140M",
        "duration_months": 20,
        "client_type": "Private Sector",
    },
    {
        "id": "CAP-059",
        "domain": "SaaS Product Engineering",
        "project_title": "Multi-Tenant HR & Payroll SaaS",
        "summary": "Developed and operate a multi-tenant HR and payroll SaaS product used by 180 corporate customers across the Gulf region, covering attendance, payroll runs with country-specific tax rules, and employee self-service. Single-instance architecture with tenant data isolation, SOC 2-aligned controls, and 99.9% measured availability over 24 months.",
        "certification": "ISO 9001",
        "year_completed": 2024,
        "contract_value": "PKR 85M",
        "duration_months": 24,
        "client_type": "International",
    },
    {
        "id": "CAP-060",
        "domain": "IT Managed Services",
        "project_title": "24/7 Managed IT Services & Helpdesk",
        "summary": "Provide 24/7 managed IT services and ITIL-based helpdesk for a federal government agency: 3,500 end users, 40 branch offices, SLA-backed incident response (P1 under 30 minutes), patch management, and endpoint security administration. Sustained 97.8% first-contact resolution within SLA across a 3-year contract with annual third-party audits.",
        "certification": "ISO 27001",
        "year_completed": 2025,
        "contract_value": "PKR 110M",
        "duration_months": 36,
        "client_type": "Federal Govt",
    },
]


def main():
    for path in FILES:
        if not path.exists():
            print(f"SKIP (missing): {path}")
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        existing = {r.get("id") for r in records}
        added = [c for c in NEW_CAPS if c["id"] not in existing]
        if not added:
            print(f"OK (already present): {path.name} — {len(records)} records")
            continue
        records.extend(added)
        path.write_text(json.dumps(records, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        print(f"ADDED {len(added)} -> {path.name} — now {len(records)} records")


if __name__ == "__main__":
    main()

"""Reference data shared by validation and the options endpoint."""

from app.core.permissions import Role

DOCUMENT_CATEGORIES: dict[str, str] = {
    "business_license": "Business licenses",
    "insurance_policy": "Insurance policies",
    "employee_certification": "Employee certifications",
    "safety_certificate": "Safety certificates",
    "equipment_inspection": "Equipment inspection certificates",
    "supplier_certification": "Supplier certifications",
    "maintenance_contract": "Maintenance contracts",
    "permit": "Permits",
    "other": "Other business documents",
}

INDUSTRIES: dict[str, str] = {
    "facility_management": "Facility management",
    "manufacturing": "Manufacturing",
    "staffing": "Staffing and recruitment",
    "equipment_rental": "Equipment rental",
    "construction": "Construction and contracting",
    "procurement": "Procurement and supply chain",
    "logistics": "Logistics and transport",
    "healthcare": "Healthcare",
    "professional_services": "Professional services",
    "other": "Other",
}

COMPANY_SIZES: dict[str, str] = {
    "1-10": "1 to 10 people",
    "11-50": "11 to 50 people",
    "51-200": "51 to 200 people",
    "201-500": "201 to 500 people",
    "501+": "More than 500 people",
}

ENTITY_TYPES: dict[str, str] = {
    "employee": "Employees",
    "supplier": "Suppliers",
    "equipment": "Equipment",
    "location": "Locations",
    "department": "Departments",
}

ROLE_LABELS: dict[Role, str] = {
    Role.OWNER: "Owner",
    Role.ADMIN: "Admin",
    Role.COMPLIANCE_MANAGER: "Compliance Manager",
    Role.MEMBER: "Member",
    Role.VIEWER: "Viewer",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.OWNER: "Full control, including billing and deleting the organization.",
    Role.ADMIN: "Manages members, settings and all documents.",
    Role.COMPLIANCE_MANAGER: "Verifies documents, manages renewals and exports reports.",
    Role.MEMBER: "Uploads and edits documents and records.",
    Role.VIEWER: "Read-only access.",
}

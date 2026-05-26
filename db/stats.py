from dataclasses import dataclass, field


@dataclass
class ImportStats:
    created_urzedy: int = 0
    created_links: int = 0
    skipped_links: int = 0
    shifted_link_dates: int = 0
    created_urzedy_ids: list[int] | None = None
    created_link_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_urzedy_ids is None:
            self.created_urzedy_ids = []
        if self.created_link_ids is None:
            self.created_link_ids = []


@dataclass
class UndoStats:
    deleted_links: int = 0
    deleted_urzedy: int = 0
    skipped_urzedy: int = 0
    deleted_addresses: int = 0
    deleted_employees: int = 0
    deleted_contracts: int = 0
    deleted_insurance_rows: int = 0


@dataclass
class StatusUpdateStats:
    updated: int = 0
    not_found: int = 0
    unchanged: int = 0


@dataclass
class EmployeeImportStats:
    created_employees: int = 0
    created_addresses: int = 0
    created_urzedy: int = 0
    created_links: int = 0
    skipped_duplicates: int = 0
    created_employee_ids: list[int] | None = None
    created_address_ids: list[int] | None = None
    created_urzedy_ids: list[int] | None = None
    created_link_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_employee_ids is None:
            self.created_employee_ids = []
        if self.created_address_ids is None:
            self.created_address_ids = []
        if self.created_urzedy_ids is None:
            self.created_urzedy_ids = []
        if self.created_link_ids is None:
            self.created_link_ids = []


@dataclass
class EmployeeAddressImportStats:
    created_addresses: int = 0
    updated_addresses: int = 0
    missing_employees: int = 0
    shifted_address_dates: int = 0
    created_address_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_address_ids is None:
            self.created_address_ids = []


@dataclass
class UmowyImportStats:
    created_contracts: int = 0
    skipped_duplicates: int = 0
    missing_employees: int = 0
    created_contract_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_contract_ids is None:
            self.created_contract_ids = []


@dataclass
class UbezpieczeniaImportStats:
    created_insurance_rows: int = 0
    skipped_duplicates: int = 0
    skipped_existing_year: int = 0
    missing_employees: int = 0
    missing_type1_contract: int = 0
    created_insurance_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_insurance_ids is None:
            self.created_insurance_ids = []


@dataclass
class UmowaFieldDelta:
    """Rozbieżność między wartością przechowywaną w BD a wartością przeliczoną."""
    field: str       # Nazwa kolumny BD, np. "KWOTA_DO_WYPLATY"
    stored: float    # Wartość z BD
    expected: float  # Wartość przeliczona algorytmem
    delta: float     # stored − expected (dodatnia = BD zawyżona)


@dataclass
class UmowaVerificationIssue:
    """Opis niezgodności finansowej jednej umowy cywilnoprawnej."""
    identyfikator: int
    employee_id: int
    numer_umowy: str
    brutto: float
    rodzaj_umowy: str          # "1" = zlecenie, "2" = dzieło
    deltas: list[UmowaFieldDelta] = field(default_factory=list)
    rate_warnings: list[str] = field(default_factory=list)
    # Zestawienie kluczowych stawek przechowywanych w BD
    stored_emerytalne_proc: float = 0.0
    stored_zdrowotne_proc: float = 0.0
    stored_stawka_pit: float = 0.0


@dataclass
class UmowyVerificationReport:
    """Podsumowanie weryfikacji całej tabeli GANG_UMOWY_CYWILNO_PRAWNE."""
    checked: int = 0
    ok: int = 0
    with_issues: int = 0
    issues: list[UmowaVerificationIssue] = field(default_factory=list)

    @property
    def pass_rate_pct(self) -> float:
        return round(100.0 * self.ok / self.checked, 1) if self.checked else 0.0


@dataclass
class PrzeprowadzkiImportStats:
    created_addresses: int = 0
    shifted_address_dates: int = 0
    created_urzedy: int = 0
    created_links: int = 0
    shifted_link_dates: int = 0
    missing_employees: int = 0
    created_address_ids: list[int] | None = None
    created_urzedy_ids: list[int] | None = None
    created_link_ids: list[int] | None = None

    def __post_init__(self) -> None:
        if self.created_address_ids is None:
            self.created_address_ids = []
        if self.created_urzedy_ids is None:
            self.created_urzedy_ids = []
        if self.created_link_ids is None:
            self.created_link_ids = []

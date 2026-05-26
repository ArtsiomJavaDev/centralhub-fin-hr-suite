from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportProfile:
    key: str
    label: str
    required_fields: tuple[str, ...]
    address_fields: tuple[str, ...]
    urzad_field: str


EMPLOYEE_IMPORT_PROFILE = ImportProfile(
    key="employees",
    label="Импорт сотрудников",
    required_fields=(
        "Nazwisko",
        "Imie",
        "Data urodzenia",
        "PESEL",
        "Nr dowodu",
        "Nr paszportu",
        "telefon",
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
        "nazwa Urząd Skarbowy",
        "Od dnia",
    ),
    address_fields=(
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
    ),
    urzad_field="nazwa Urząd Skarbowy",
)


LEGACY_URZEDY_PROFILE = ImportProfile(
    key="urzedy_link",
    label="Импорт привязки urzedu по табельному номеру",
    required_fields=("Kod US", "Nazwa", "Nr Ewidencyjny", "Od dnia"),
    address_fields=(),
    urzad_field="Nazwa",
)


EMPLOYEE_ADDRESS_IMPORT_PROFILE = ImportProfile(
    key="employee_addresses",
    label="Импорт адресов сотрудников",
    required_fields=(
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
        "NR Ewidencyjny",
        "PESEL",
        "Od dnia",
    ),
    address_fields=(
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
    ),
    urzad_field="",
)


UMOWY_DZIELO_IMPORT_PROFILE = ImportProfile(
    key="umowy_dzielo",
    label="Импорт UMOWY — umowa o dzieło",
    required_fields=(
        "PESEL",
        "NR Ewidencyjny",
        "номер умовы",
        "номер рахунка",
        "Дата выплаты",
        "Дата умовы",
        "Форма податка",
        "Kwota brutto",
        "KOSZTY UZYSKANIA PRZYCHODU %",
    ),
    address_fields=(),
    urzad_field="",
)


UMOWY_MIXED_IMPORT_PROFILE = ImportProfile(
    key="umowy_mixed",
    label="Импорт UMOWY — zlecenie + o dzieło (jeden plik)",
    required_fields=(
        "PESEL",
        "NR Ewidencyjny",
        "номер умовы",
        "номер рахунка",
        "Тип умовы",
        "Дата выплаты",
        "Дата умовы",
        "Форма податка",
        "Kwota brutto",
        "KOSZTY UZYSKANIA PRZYCHODU %",
        "Skł.na ub.emerytal.[%]",
        "Składka ub.rent. U [%]",
        "Składka ub.rent. P [%]",
        "Składka ub.chorob.[%]",
        "Składka ub.wypadk.[%]",
        "Składka ub.zdrowotne[%]",
        "FP [%]",
        "FGŚP [%]",
    ),
    address_fields=(),
    urzad_field="",
)


UMOWY_IMPORT_PROFILE = ImportProfile(
    key="umowy",
    label="Импорт UMOWY",
    required_fields=(
        "PESEL",
        "NR Ewidencyjny",
        "номер умовы",
        "номер рахунка",
        "Тип умовы",
        "Дата выплаты",
        "Дата умовы",
        "Форма податка",
        "Kwota brutto",
        "KOSZTY UZYSKANIA PRZYCHODU %",
        "Skł.na ub.emerytal.[%]",
        "Składka ub.rent. U [%]",
        "Składka ub.rent. P [%]",
        "Składka ub.chorob.[%]",
        "Składka ub.wypadk.[%]",
        "Składka ub.zdrowotne[%]",
        "FP [%]",
        "FGŚP [%]",
    ),
    address_fields=(),
    urzad_field="",
)


UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE = ImportProfile(
    key="ubezpieczenia_obowiazkowe",
    label="Импорт Ubezpieczenie obowiązkowe",
    required_fields=(
        "PESEL",
        "NR Ewidencyjny",
        "Номер умовы",
        "Typ ubezpieczenia",
        "Data powstania obowiazku ubezpieczenia",
        "Osoba podlega ubezpieczeniu Emerytalnemu",
        "Osoba podlega ubezpieczeniu Rentowemu",
        "Osoba podlega ubezpieczeniu Wypadkowemu",
        "Osoba podlega ubezpieczeniu Chorobowemu",
    ),
    address_fields=(),
    urzad_field="",
)


PRZEPROWADZKI_IMPORT_PROFILE = ImportProfile(
    key="przeprowadzki",
    label="Импорт для переехавших",
    required_fields=(
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
        "nazwa Urząd Skarbowy",
        "NR Ewidencyjny",
        "PESEL",
        "Od dnia",
    ),
    address_fields=(
        "Kraj",
        "Wojewodztwo",
        "Powiat",
        "Gmina",
        "Ulica",
        "Numer Domu",
        "Numer lokalu",
        "Miejscowosc",
        "Kod pocztowy",
        "Poczta",
    ),
    urzad_field="nazwa Urząd Skarbowy",
)


AVAILABLE_PROFILES: dict[str, ImportProfile] = {
    EMPLOYEE_IMPORT_PROFILE.key: EMPLOYEE_IMPORT_PROFILE,
    LEGACY_URZEDY_PROFILE.key: LEGACY_URZEDY_PROFILE,
    EMPLOYEE_ADDRESS_IMPORT_PROFILE.key: EMPLOYEE_ADDRESS_IMPORT_PROFILE,
    UMOWY_DZIELO_IMPORT_PROFILE.key: UMOWY_DZIELO_IMPORT_PROFILE,
    UMOWY_MIXED_IMPORT_PROFILE.key: UMOWY_MIXED_IMPORT_PROFILE,
    UMOWY_IMPORT_PROFILE.key: UMOWY_IMPORT_PROFILE,
    UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key: UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE,
    PRZEPROWADZKI_IMPORT_PROFILE.key: PRZEPROWADZKI_IMPORT_PROFILE,
}


def _effective_required_fields(profile: ImportProfile, employee_lookup_mode: str) -> tuple[str, ...]:
    fields = list(profile.required_fields)
    if profile.key == EMPLOYEE_IMPORT_PROFILE.key:
        return tuple(fields)

    mode = (employee_lookup_mode or "nr").lower()
    remove_field = "PESEL" if mode == "nr" else "NR Ewidencyjny"
    if profile.key == LEGACY_URZEDY_PROFILE.key and mode == "pesel":
        fields.append("PESEL")
        remove_field = "Nr Ewidencyjny"
    fields = [field for field in fields if field != remove_field]
    return tuple(dict.fromkeys(fields))

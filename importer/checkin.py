from __future__ import annotations

import re
from typing import Any

import pandas as pd

from database import DatabaseService

from .profiles import (
    EMPLOYEE_ADDRESS_IMPORT_PROFILE,
    EMPLOYEE_IMPORT_PROFILE,
    ImportProfile,
    LEGACY_URZEDY_PROFILE,
    PRZEPROWADZKI_IMPORT_PROFILE,
    UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE,
    UMOWY_DZIELO_IMPORT_PROFILE,
    UMOWY_IMPORT_PROFILE,
    UMOWY_MIXED_IMPORT_PROFILE,
)
from .types import CheckInResult, RowStatus, ValidationRow
from .umowy_ppk_pairs import merge_ppk_companion_rows_mapped
from .utils import (
    ADDRESS_FIELD_LIMITS,
    _address_key_to_field_name,
    _clarion_year,
    _normalize_typ_ubezpieczenia,
    _resolve_data_od,
    _to_bool_int,
    _to_clarion_date,
    _to_float,
    _to_int,
    load_urzedy_reference_entries,
    resolve_urzad_reference_entry,
)


def _normalize_kup_percent(value: float | None) -> float | None:
    """Normalize KUP to percentage scale (0..100), not decimal fractions.

    Excel percentage-formatted cells are often read as fractions
    (e.g. 50% -> 0.5). Business logic expects plain percent values
    (e.g. 50), so convert non-zero values in [-1, 1] to *100.
    """
    if value is None:
        return None
    if value == 0:
        return 0.0
    if -1.0 <= value <= 1.0:
        return value * 100.0
    return value


def check_in(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    dry_run: bool = True,
    data_od: int = 0,
    profile: ImportProfile = LEGACY_URZEDY_PROFILE,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    if profile.key == EMPLOYEE_IMPORT_PROFILE.key:
        return check_in_employees(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
        )
    if profile.key == EMPLOYEE_ADDRESS_IMPORT_PROFILE.key:
        return check_in_employee_addresses(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
        )
    if profile.key == UMOWY_IMPORT_PROFILE.key:
        return check_in_umowy(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
        )
    if profile.key == UMOWY_MIXED_IMPORT_PROFILE.key:
        return check_in_umowy(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
            allow_mixed_contract_types=True,
        )
    if profile.key == UMOWY_DZIELO_IMPORT_PROFILE.key:
        return check_in_umowy_dzielo(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
        )
    if profile.key == UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key:
        return check_in_ubezpieczenia_obowiazkowe(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
        )
    if profile.key == PRZEPROWADZKI_IMPORT_PROFILE.key:
        return check_in_przeprowadzki(
            mapped_df,
            db_service=db_service,
            default_data_od=data_od,
            strict_od_dnia=strict_od_dnia,
            employee_lookup_mode=employee_lookup_mode,
        )
    return check_in_urzedy_links(
        mapped_df,
        db_service=db_service,
        dry_run=dry_run,
        data_od=data_od,
        strict_od_dnia=strict_od_dnia,
        employee_lookup_mode=employee_lookup_mode,
    )


def _register_missing_urzad(missing: set[str], urzad_name: str) -> None:
    name = str(urzad_name or "").strip()
    if name:
        missing.add(name)


def check_in_employees(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
) -> CheckInResult:
    urzedy_reference_entries = load_urzedy_reference_entries()
    rows: list[ValidationRow] = []
    to_create_urzedy = 0
    errors = 0
    importable_rows: list[dict[str, Any]] = []
    seen_pesels_in_file: set[str] = set()
    missing_urzedy: set[str] = set()

    for idx, row in mapped_df.iterrows():
        full_name = row["full_name"]
        urzad_name = row["urzad_name"]
        postal_code = row["postal_code"]
        pesel_value = str(row.get("pesel", "")).strip()

        if pesel_value:
            if pesel_value in seen_pesels_in_file:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"Дубликат PESEL '{pesel_value}' в файле. "
                            "Строка будет пропущена."
                        ),
                        field_name="PESEL",
                    )
                )
                continue
            seen_pesels_in_file.add(pesel_value)
            if db_service is not None and db_service.employee_id_by_pesel(pesel_value) is not None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"Сотрудник с PESEL '{pesel_value}' уже существует в базе. "
                            "Строка будет пропущена при импорте."
                        ),
                        field_name="PESEL",
                    )
                )
                continue

        if not full_name:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Brak pola Nazwisko lub Imie.",
                    field_name="Nazwisko/Imie",
                )
            )
            errors += 1
            continue

        if not urzad_name:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Brak pola nazwa Urząd Skarbowy.",
                    field_name="nazwa Urząd Skarbowy",
                )
            )
            errors += 1
            continue

        row_data_od = _resolve_data_od(
            row.get("data_od_source"),
            default_data_od,
            strict_mode=strict_od_dnia,
        )
        if row_data_od is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=(
                        "Поле 'Od dnia' не заполнено или имеет неверный формат."
                        if strict_od_dnia
                        else "Не заполнено поле 'Od dnia' или неверный формат даты."
                    ),
                    field_name="Od dnia",
                )
            )
            errors += 1
            continue

        if postal_code and len(postal_code) < 5:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=f"Kod pocztowy '{postal_code}' wyglada na niepelny.",
                    field_name="Kod pocztowy",
                )
            )
        elif db_service and not db_service.urzad_exists_by_name(urzad_name):
            ref_name, code_from_reference = resolve_urzad_reference_entry(
                urzad_name,
                urzedy_reference_entries,
            )
            resolved_name = ref_name or urzad_name
            if code_from_reference:
                if db_service.urzad_exists(code_from_reference):
                    rows.append(
                        ValidationRow(
                            index=idx,
                            status=RowStatus.WARNING,
                            message=(
                                f"Urząd '{urzad_name}' nie znaleziony po nazwie, "
                                f"ale kod {code_from_reference} istnieje w bazie."
                            ),
                        )
                    )
                else:
                    rows.append(
                        ValidationRow(
                            index=idx,
                            status=RowStatus.WARNING,
                            message=(
                                f"Urząd '{resolved_name}' nie istnieje - zostanie dodany "
                                f"z kodem {code_from_reference}."
                            ),
                        )
                    )
                    to_create_urzedy += 1
            else:
                _register_missing_urzad(missing_urzedy, urzad_name)
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=(
                            f"Urząd '{urzad_name}' nie istnieje w bazie i brak go w "
                            "lokalnym slowniku urzedow. Dodaj go do urzedy_reference.json."
                        ),
                    )
                )
                errors += 1
                continue
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=f"OK: {full_name}, urząd: {urzad_name}.",
                )
            )

        row_dict = row.to_dict()
        ref_name, ref_code = resolve_urzad_reference_entry(urzad_name, urzedy_reference_entries)
        row_dict["urzad_name_from_reference"] = ref_name
        row_dict["urzad_code_from_reference"] = ref_code
        row_dict["data_od"] = row_data_od
        importable_rows.append(row_dict)

    return CheckInResult(
        rows=rows,
        to_create_urzedy=to_create_urzedy,
        to_create_links=0,
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
        missing_urzedy=sorted(missing_urzedy),
    )


def check_in_urzedy_links(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    dry_run: bool = True,
    data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    rows: list[ValidationRow] = []
    to_create_urzedy = 0
    to_create_links = 0
    skipped_links = 0
    errors = 0
    importable_rows: list[dict[str, Any]] = []

    for idx, row in mapped_df.iterrows():
        kod_us = row["kod_us"]
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "Nr Ewidencyjny"

        if not kod_us.isdigit():
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Kod US '{kod_us}' nie jest liczba.",
                    field_name="Kod US",
                )
            )
            errors += 1
            continue

        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Поле '{lookup_field}' не заполнено.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue

        row_data_od = _resolve_data_od(
            row.get("data_od_source"),
            data_od,
            strict_mode=strict_od_dnia,
        )
        if row_data_od is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнено поле 'Od dnia' или неверный формат даты.",
                    field_name="Od dnia",
                )
            )
            errors += 1
            continue

        if db_service is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message="Dry-run bez bazy: walidacja syntaktyczna OK.",
                )
            )
            to_create_links += 1
            importable_rows.append(
                {
                    "employee_id": -1,
                    "kod_us": kod_us,
                    "nazwa": str(row["nazwa"]),
                    "employee_lookup_value": employee_lookup_value,
                    "employee_lookup_mode": employee_lookup_mode,
                    "data_od": row_data_od,
                }
            )
            continue

        employee_id = (
            db_service.employee_id_by_pesel(employee_lookup_value)
            if employee_lookup_mode == "pesel"
            else db_service.employee_id_by_nr(employee_lookup_value)
        )
        if employee_id is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue

        urzad_id = db_service.urzad_id_by_kod(kod_us)
        if urzad_id is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=f"Urzad {kod_us} nie istnieje. Zostanie utworzony.",
                    field_name="Kod US",
                )
            )
            to_create_urzedy += 1
            to_create_links += 1
            importable_rows.append(
                {
                    "employee_id": employee_id,
                    "kod_us": kod_us,
                    "nazwa": str(row["nazwa"]),
                    "employee_lookup_value": employee_lookup_value,
                    "employee_lookup_mode": employee_lookup_mode,
                }
            )
            continue

        link_exists = db_service.link_exists(employee_id, urzad_id, row_data_od)
        if link_exists:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=(
                        f"Relacja pracownik {employee_lookup_value} -> US {kod_us} "
                        f"na DATA_OD={row_data_od} juz istnieje. "
                        "Zostanie dodana nowa relacja z nastepna wolna data."
                    ),
                    field_name=lookup_field,
                )
            )
            to_create_links += 1
            importable_rows.append(
                {
                    "employee_id": employee_id,
                    "kod_us": kod_us,
                    "nazwa": str(row["nazwa"]),
                    "employee_lookup_value": employee_lookup_value,
                    "employee_lookup_mode": employee_lookup_mode,
                    "data_od": row_data_od,
                }
            )
            continue

        rows.append(
            ValidationRow(
                index=idx,
                status=RowStatus.OK,
                message=f"OK: pracownik {employee_lookup_value}, urzad {kod_us}.",
                field_name=lookup_field,
            )
        )
        to_create_links += 1
        importable_rows.append(
            {
                "employee_id": employee_id,
                "kod_us": kod_us,
                "nazwa": str(row["nazwa"]),
                "employee_lookup_value": employee_lookup_value,
                "employee_lookup_mode": employee_lookup_mode,
                "data_od": row_data_od,
            }
        )

    return CheckInResult(
        rows=rows,
        to_create_urzedy=to_create_urzedy,
        to_create_links=to_create_links,
        skipped_links=skipped_links,
        errors=errors,
        importable_rows=importable_rows,
    )


def check_in_employee_addresses(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    rows: list[ValidationRow] = []
    errors = 0
    importable_rows: list[dict[str, Any]] = []

    for idx, row in mapped_df.iterrows():
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Не заполнен {lookup_field}.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue

        row_data_od = _resolve_data_od(
            row.get("data_od_source"),
            default_data_od,
            strict_mode=strict_od_dnia,
        )
        if row_data_od is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнено поле 'Od dnia' или неверный формат даты.",
                    field_name="Od dnia",
                )
            )
            errors += 1
            continue

        employee_id = None
        if db_service is not None:
            employee_id = (
                db_service.employee_id_by_pesel(employee_lookup_value)
                if employee_lookup_mode == "pesel"
                else db_service.employee_id_by_nr(employee_lookup_value)
            )
            if employee_id is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                        field_name=lookup_field,
                    )
                )
                errors += 1
                continue

        too_long = []
        for field_key, max_len in ADDRESS_FIELD_LIMITS.items():
            value = str(row.get(field_key, "") or "").strip()
            if value and len(value) > max_len:
                too_long.append((field_key, len(value), max_len))

        if too_long:
            first = too_long[0]
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=(
                        f"Поле '{_address_key_to_field_name(first[0])}' слишком длинное "
                        f"({first[1]}>{first[2]}). Проверьте сопоставление колонок."
                    ),
                    field_name=_address_key_to_field_name(first[0]),
                )
            )
            errors += 1
            continue

        house_no = str(row.get("house_no", "") or "").strip()
        if house_no and re.match(r"^\d{4}-\d{2}-\d{2}", house_no):
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=(
                        "Поле 'Numer Domu' похоже на дату: "
                        f"'{house_no}'. Проверьте сопоставление колонок."
                    ),
                    field_name="Numer Domu",
                )
            )
            errors += 1
            continue

        if row["postal_code"] and len(str(row["postal_code"])) < 5:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=f"Почтовый индекс '{row['postal_code']}' выглядит неполным.",
                    field_name="Kod pocztowy",
                )
            )
        elif db_service is not None and db_service.address_exists_on_date(int(employee_id), row_data_od):
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=(
                        f"У сотрудника уже есть адрес на DATA_OD={row_data_od}. "
                        "Будет добавлен новый адрес с ближайшей свободной датой."
                    ),
                    field_name="Od dnia",
                )
            )
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=f"OK: адрес для {lookup_field}={employee_lookup_value}.",
                    field_name=lookup_field,
                )
            )

        payload = row.to_dict()
        payload["employee_id"] = employee_id
        payload["data_od"] = row_data_od
        importable_rows.append(payload)

    return CheckInResult(
        rows=rows,
        to_create_urzedy=0,
        to_create_links=len(importable_rows),
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
    )


def check_in_umowy(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
    allow_mixed_contract_types: bool = False,
) -> CheckInResult:
    rows: list[ValidationRow] = []
    errors = 0
    importable_rows: list[dict[str, Any]] = []
    seen_batch_keys: set[tuple[str, str, int, int, str]] = set()

    mapped_df = merge_ppk_companion_rows_mapped(mapped_df)

    for idx, row in mapped_df.iterrows():
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
        numer_umowy = str(row.get("numer_umowy", "")).strip()
        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Не заполнен {lookup_field}.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue
        if not numer_umowy:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнен номер договора.",
                    field_name="номер умовы",
                )
            )
            errors += 1
            continue

        data_umowy = _to_clarion_date(row.get("data_umowy_source"))
        data_wyplaty = _to_clarion_date(row.get("data_wyplaty_source"))
        if data_umowy is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверная 'Дата умовы'.",
                    field_name="Дата умовы",
                )
            )
            errors += 1
            continue
        if data_wyplaty is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверная 'Дата выплаты'.",
                    field_name="Дата выплаты",
                )
            )
            errors += 1
            continue

        numer_rachunku = str(row.get("numer_rachunku", "")).strip()[:100]
        data_umowy_year = _clarion_year(data_umowy)
        if data_umowy_year is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не удалось определить год по 'Дата умовы'.",
                    field_name="Дата умовы",
                )
            )
            errors += 1
            continue

        batch_key = (employee_lookup_value, numer_umowy, data_umowy, data_wyplaty, numer_rachunku)
        if batch_key in seen_batch_keys:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=(
                        f"Дубликат в файле: договор '{numer_umowy}' с DATA_UMOWY={data_umowy}, "
                        f"DATA_WYPLATY={data_wyplaty} и NUMER_RACHUNKU='{numer_rachunku}' "
                        f"для {lookup_field}={employee_lookup_value}. Строка будет пропущена."
                    ),
                    field_name="номер умовы",
                )
            )
            continue
        seen_batch_keys.add(batch_key)

        brutto = _to_float(row.get("wynagrodzenie_brutto_source"))
        koszty_proc = _normalize_kup_percent(_to_float(row.get("koszty_proc_source")))
        stawka_podatku_proc = _to_float(row.get("stawka_podatku_proc"))
        emerytalne_proc = _to_float(row.get("emerytalne_proc_source"))
        rentowe_u_proc = _to_float(row.get("rentowe_u_proc_source"))
        rentowe_p_proc = _to_float(row.get("rentowe_p_proc_source"))
        chorobowe_proc = _to_float(row.get("chorobowe_proc_source"))
        wypadkowe_proc = _to_float(row.get("wypadkowe_proc_source"))
        zdrowotne_proc = _to_float(row.get("zdrowotne_proc_source"))
        fp_proc = _to_float(row.get("fp_proc_source"))
        fgsp_proc = _to_float(row.get("fgsp_proc_source"))
        typ_umowy_no = _to_int(row.get("typ_umowy"))
        if brutto is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверное значение 'Kwota brutto'.",
                    field_name="Kwota brutto",
                )
            )
            errors += 1
            continue
        if koszty_proc is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверное значение 'KOSZTY UZYSKANIA PRZYCHODU %'.",
                    field_name="KOSZTY UZYSKANIA PRZYCHODU %",
                )
            )
            errors += 1
            continue
        if typ_umowy_no not in (1, 2):
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Поле 'Тип умовы' должно быть 1 или 2.",
                    field_name="Тип умовы",
                )
            )
            errors += 1
            continue
        if not allow_mixed_contract_types and typ_umowy_no == 2:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=(
                        "Тип умовы = 2 (o dzieło): для этого файла выберите профиль "
                        "«UMOWY — o dzieło» или «UMOWY — zlecenie + o dzieło (jeden plik)»."
                    ),
                    field_name="Тип умовы",
                )
            )
            errors += 1
            continue

        dzielo_relaxed = allow_mixed_contract_types and typ_umowy_no == 2
        if dzielo_relaxed:
            emerytalne_proc = 0.0 if emerytalne_proc is None else float(emerytalne_proc)
            rentowe_u_proc = 0.0 if rentowe_u_proc is None else float(rentowe_u_proc)
            rentowe_p_proc = 0.0 if rentowe_p_proc is None else float(rentowe_p_proc)
            chorobowe_proc = 0.0 if chorobowe_proc is None else float(chorobowe_proc)
            wypadkowe_proc = 0.0 if wypadkowe_proc is None else float(wypadkowe_proc)
            zdrowotne_proc = 0.0 if zdrowotne_proc is None else float(zdrowotne_proc)
            fp_proc = 0.0 if fp_proc is None else float(fp_proc)
            if fgsp_proc is None:
                fgsp_proc = 0.0
        else:
            if emerytalne_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Skł.na ub.emerytal.[%]'.",
                        field_name="Skł.na ub.emerytal.[%]",
                    )
                )
                errors += 1
                continue
            if rentowe_u_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Składka ub.rent. U [%]'.",
                        field_name="Składka ub.rent. U [%]",
                    )
                )
                errors += 1
                continue
            if rentowe_p_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Składka ub.rent. P [%]'.",
                        field_name="Składka ub.rent. P [%]",
                    )
                )
                errors += 1
                continue
            if chorobowe_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Składka ub.chorob.[%]'.",
                        field_name="Składka ub.chorob.[%]",
                    )
                )
                errors += 1
                continue
            if wypadkowe_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Składka ub.wypadk.[%]'.",
                        field_name="Składka ub.wypadk.[%]",
                    )
                )
                errors += 1
                continue
            if zdrowotne_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'Składka ub.zdrowotne[%]'.",
                        field_name="Składka ub.zdrowotne[%]",
                    )
                )
                errors += 1
                continue
            if fp_proc is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message="Неверное значение 'FP [%]'.",
                        field_name="FP [%]",
                    )
                )
                errors += 1
                continue
            if fgsp_proc is None:
                fgsp_proc = 0.0

        employee_id = None
        if db_service is not None:
            employee_id = (
                db_service.employee_id_by_pesel(employee_lookup_value)
                if employee_lookup_mode == "pesel"
                else db_service.employee_id_by_nr(employee_lookup_value)
            )
            if employee_id is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                        field_name=lookup_field,
                    )
                )
                errors += 1
                continue
            if db_service.umowa_exists(
                employee_id=employee_id,
                numer_umowy=numer_umowy,
                data_umowy=data_umowy,
                data_wyplaty=data_wyplaty,
                numer_rachunku=numer_rachunku,
            ):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"Договор '{numer_umowy}' на DATA_UMOWY={data_umowy} c DATA_WYPLATY="
                            f"{data_wyplaty} и NUMER_RACHUNKU='{numer_rachunku}' уже существует "
                            "у этого сотрудника и будет пропущен."
                        ),
                        field_name="номер умовы",
                    )
                )
            else:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.OK,
                        message=(
                            f"OK: UMOWA '{numer_umowy}' для {lookup_field}={employee_lookup_value}."
                        ),
                        field_name="номер умовы",
                    )
                )
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=f"OK: UMOWA '{numer_umowy}' для {lookup_field}={employee_lookup_value}.",
                    field_name="номер умовы",
                )
            )

        importable_rows.append(
            {
                "employee_id": employee_id,
                "employee_lookup_value": employee_lookup_value,
                "employee_lookup_mode": employee_lookup_mode,
                "numer_umowy": numer_umowy,
                "numer_rachunku": numer_rachunku,
                "typ_umowy": str(row.get("typ_umowy", "")),
                "typ_umowy_no": typ_umowy_no,
                "forma_podatka": str(row.get("forma_podatka", "")),
                "data_wyplaty": data_wyplaty,
                "data_umowy": data_umowy,
                "wynagrodzenie_brutto": brutto,
                "koszty_proc": koszty_proc,
                "stawka_podatku_proc": stawka_podatku_proc,
                "emerytalne_proc": emerytalne_proc,
                "rentowe_u_proc": rentowe_u_proc,
                "rentowe_p_proc": rentowe_p_proc,
                "chorobowe_proc": chorobowe_proc,
                "wypadkowe_proc": wypadkowe_proc,
                "zdrowotne_proc": zdrowotne_proc,
                "fp_proc": fp_proc,
                "fgsp_proc": 0.0,
                "ppk_pracownika_kwota": float(row.get("ppk_pracownika_kwota", 0) or 0),
            }
        )

    return CheckInResult(
        rows=rows,
        to_create_urzedy=0,
        to_create_links=len(importable_rows),
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
    )


def check_in_umowy_dzielo(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    """Walidacja importu umów o dzieło (bez kolumn ze składkami; zapis jako typ 2).

    Numer umowy w pliku często ma postać ewidencyjną (np. „802/UD/…”),
    nie „z dnia DD.MM.YYYY” jak czasem przy zleceniach — więc nie sprawdzamy
    zgodności z pojedynczym NUMER_UMOWY z bazy dla danego roku, ani nie
    wymagamy jednego numeru dla pracownika w całym roku w obrębie pliku (wiele umów dzieło rocznie jest OK).
    """
    rows: list[ValidationRow] = []
    errors = 0
    importable_rows: list[dict[str, Any]] = []
    seen_batch_keys: set[tuple[str, str, int, int, str]] = set()

    for idx, row in mapped_df.iterrows():
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
        numer_umowy = str(row.get("numer_umowy", "")).strip()
        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Не заполнен {lookup_field}.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue
        if not numer_umowy:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнен номер договора.",
                    field_name="номер умовы",
                )
            )
            errors += 1
            continue

        data_umowy = _to_clarion_date(row.get("data_umowy_source"))
        data_wyplaty = _to_clarion_date(row.get("data_wyplaty_source"))
        if data_umowy is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверная 'Дата умовы'.",
                    field_name="Дата умовы",
                )
            )
            errors += 1
            continue
        if data_wyplaty is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверная 'Дата выплаты'.",
                    field_name="Дата выплаты",
                )
            )
            errors += 1
            continue

        numer_rachunku = str(row.get("numer_rachunku", "")).strip()[:100]
        data_umowy_year = _clarion_year(data_umowy)
        if data_umowy_year is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не удалось определить год по 'Дата умовы'.",
                    field_name="Дата умовы",
                )
            )
            errors += 1
            continue

        batch_key = (employee_lookup_value, numer_umowy, data_umowy, data_wyplaty, numer_rachunku)
        if batch_key in seen_batch_keys:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.WARNING,
                    message=(
                        f"Дубликат в файле: договор '{numer_umowy}' с DATA_UMOWY={data_umowy}, "
                        f"DATA_WYPLATY={data_wyplaty} и NUMER_RACHUNKU='{numer_rachunku}' "
                        f"для {lookup_field}={employee_lookup_value}. Строка будет пропущена."
                    ),
                    field_name="номер умовы",
                )
            )
            continue
        seen_batch_keys.add(batch_key)

        brutto = _to_float(row.get("wynagrodzenie_brutto_source"))
        koszty_proc = _normalize_kup_percent(_to_float(row.get("koszty_proc_source")))
        stawka_podatku_proc = _to_float(row.get("stawka_podatku_proc"))

        if brutto is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверное значение 'Kwota brutto'.",
                    field_name="Kwota brutto",
                )
            )
            errors += 1
            continue
        if koszty_proc is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверное значение 'KOSZTY UZYSKANIA PRZYCHODU %'.",
                    field_name="KOSZTY UZYSKANIA PRZYCHODU %",
                )
            )
            errors += 1
            continue

        employee_id = None
        if db_service is not None:
            employee_id = (
                db_service.employee_id_by_pesel(employee_lookup_value)
                if employee_lookup_mode == "pesel"
                else db_service.employee_id_by_nr(employee_lookup_value)
            )
            if employee_id is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                        field_name=lookup_field,
                    )
                )
                errors += 1
                continue
            if db_service.umowa_exists(
                employee_id=employee_id,
                numer_umowy=numer_umowy,
                data_umowy=data_umowy,
                data_wyplaty=data_wyplaty,
                numer_rachunku=numer_rachunku,
            ):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"Договор '{numer_umowy}' на DATA_UMOWY={data_umowy} c DATA_WYPLATY="
                            f"{data_wyplaty} и NUMER_RACHUNKU='{numer_rachunku}' уже существует "
                            "у этого сотрудника и будет пропущен."
                        ),
                        field_name="номер умовы",
                    )
                )
            else:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.OK,
                        message=(
                            f"OK: UMOWA o dzieło '{numer_umowy}' dla {lookup_field}={employee_lookup_value}."
                        ),
                        field_name="номер умовы",
                    )
                )
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=(
                        f"OK: UMOWA o dzieło '{numer_umowy}' dla {lookup_field}={employee_lookup_value}."
                    ),
                    field_name="номер умовы",
                )
            )

        importable_rows.append(
            {
                "employee_id": employee_id,
                "employee_lookup_value": employee_lookup_value,
                "employee_lookup_mode": employee_lookup_mode,
                "numer_umowy": numer_umowy,
                "numer_rachunku": numer_rachunku,
                "typ_umowy": "2",
                "forma_podatka": str(row.get("forma_podatka", "")),
                "data_wyplaty": data_wyplaty,
                "data_umowy": data_umowy,
                "wynagrodzenie_brutto": brutto,
                "koszty_proc": koszty_proc,
                "stawka_podatku_proc": stawka_podatku_proc,
            }
        )

    return CheckInResult(
        rows=rows,
        to_create_urzedy=0,
        to_create_links=len(importable_rows),
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
    )


def check_in_ubezpieczenia_obowiazkowe(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    rows: list[ValidationRow] = []
    errors = 0
    importable_rows: list[dict[str, Any]] = []

    for idx, row in mapped_df.iterrows():
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
        numer_umowy = str(row.get("numer_umowy", "")).strip()
        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Не заполнен {lookup_field}.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue
        if not numer_umowy:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнен номер умовы.",
                    field_name="Номер умовы",
                )
            )
            errors += 1
            continue

        data_obowiazku = _to_clarion_date(row.get("data_obowiazku_ubezpieczenia_source"))
        data_od = data_obowiazku
        typ_ubezpieczenia = _normalize_typ_ubezpieczenia(row.get("typ_ubezpieczenia", ""))
        emerytalne = _to_bool_int(row.get("ubezpieczenie_emerytalne_source"))
        rentowe = _to_bool_int(row.get("ubezpieczenie_rentowe_source"))
        wypadkowe = _to_bool_int(row.get("ubezpieczenie_wypadkowe_source"))
        chorobowe = _to_bool_int(row.get("ubezpieczenie_chorobowe_source"))

        if data_obowiazku is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Неверная 'Data powstania obowiazku ubezpieczenia'.",
                    field_name="Data powstania obowiazku ubezpieczenia",
                )
            )
            errors += 1
            continue
        if not typ_ubezpieczenia:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнено поле 'Typ ubezpieczenia'.",
                    field_name="Typ ubezpieczenia",
                )
            )
            errors += 1
            continue
        if None in (emerytalne, rentowe, wypadkowe, chorobowe):
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Поля ubezpieczenia должны быть 0/1 или Tak/Nie.",
                    field_name="Osoba podlega ubezpieczeniu Emerytalnemu",
                )
            )
            errors += 1
            continue

        employee_id = None
        if db_service is not None:
            employee_id = (
                db_service.employee_id_by_pesel(employee_lookup_value)
                if employee_lookup_mode == "pesel"
                else db_service.employee_id_by_nr(employee_lookup_value)
            )
            if employee_id is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                        field_name=lookup_field,
                    )
                )
                errors += 1
                continue
            if not db_service.umowa_type_one_exists(employee_id, numer_umowy):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=(
                            f"Для {lookup_field}={employee_lookup_value} не найдена UMOWA типа 1 "
                            f"с номером '{numer_umowy}'."
                        ),
                        field_name="Номер умовы",
                    )
                )
                errors += 1
                continue
            row_year = _clarion_year(int(data_obowiazku))
            if row_year is not None and db_service.obowiazkowe_ubezpieczenie_exists_for_year(
                employee_id=int(employee_id),
                year=row_year,
            ):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"У сотрудника уже есть ubezpieczenie за {row_year} год — будет пропущено."
                        ),
                        field_name=lookup_field,
                    )
                )
                continue
            if db_service.obowiazkowe_ubezpieczenie_exists(
                employee_id=int(employee_id),
                data_od=int(data_od),
                data_obowiazku=int(data_obowiazku),
                emerytalne=int(emerytalne),
                rentowe=int(rentowe),
                wypadkowe=int(wypadkowe),
                chorobowe=int(chorobowe),
            ):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message="Такое обязательное страхование уже существует и будет пропущено.",
                        field_name=lookup_field,
                    )
                )
            else:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.OK,
                        message=(
                            f"OK: ubezpieczenie obowiązkowe для {lookup_field}="
                            f"{employee_lookup_value}."
                        ),
                        field_name=lookup_field,
                    )
                )
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=(
                        f"OK: ubezpieczenie obowiązkowe для {lookup_field}={employee_lookup_value}."
                    ),
                    field_name=lookup_field,
                )
            )

        importable_rows.append(
            {
                "employee_id": employee_id,
                "employee_lookup_value": employee_lookup_value,
                "employee_lookup_mode": employee_lookup_mode,
                "numer_umowy": numer_umowy,
                "typ_ubezpieczenia": typ_ubezpieczenia,
                "data_obowiazku_ubezpieczenia": int(data_obowiazku),
                "ubezpieczenie_emerytalne": int(emerytalne),
                "ubezpieczenie_rentowe": int(rentowe),
                "ubezpieczenie_wypadkowe": int(wypadkowe),
                "ubezpieczenie_chorobowe": int(chorobowe),
                "data_od": int(data_od),
            }
        )

    return CheckInResult(
        rows=rows,
        to_create_urzedy=0,
        to_create_links=len(importable_rows),
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
    )


def check_in_przeprowadzki(
    mapped_df: pd.DataFrame,
    db_service: DatabaseService | None = None,
    default_data_od: int = 0,
    strict_od_dnia: bool = False,
    employee_lookup_mode: str = "nr",
) -> CheckInResult:
    urzedy_reference_entries = load_urzedy_reference_entries()
    rows: list[ValidationRow] = []
    errors = 0
    to_create_urzedy = 0
    importable_rows: list[dict[str, Any]] = []
    missing_urzedy: set[str] = set()

    for idx, row in mapped_df.iterrows():
        employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
        lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
        urzad_name = str(row.get("urzad_name", "")).strip()
        row_data_od = _resolve_data_od(row.get("data_od_source"), default_data_od, strict_od_dnia)

        if not employee_lookup_value:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=f"Не заполнен {lookup_field}.",
                    field_name=lookup_field,
                )
            )
            errors += 1
            continue
        if not urzad_name:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Не заполнено поле 'nazwa Urząd Skarbowy'.",
                    field_name="nazwa Urząd Skarbowy",
                )
            )
            errors += 1
            continue
        if row_data_od is None:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message="Поле 'Od dnia' не заполнено или имеет неверный формат.",
                    field_name="Od dnia",
                )
            )
            errors += 1
            continue

        too_long = []
        for field_key, max_len in ADDRESS_FIELD_LIMITS.items():
            value = str(row.get(field_key, "") or "").strip()
            if value and len(value) > max_len:
                too_long.append((field_key, len(value), max_len))
        if too_long:
            first = too_long[0]
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.ERROR,
                    message=(
                        f"Поле '{_address_key_to_field_name(first[0])}' слишком длинное "
                        f"({first[1]}>{first[2]}). Проверьте сопоставление колонок."
                    ),
                    field_name=_address_key_to_field_name(first[0]),
                )
            )
            errors += 1
            continue

        employee_id = None
        ref_name, code_from_reference = resolve_urzad_reference_entry(
            urzad_name,
            urzedy_reference_entries,
        )
        resolved_name = ref_name or urzad_name
        if db_service is not None:
            employee_id = (
                db_service.employee_id_by_pesel(employee_lookup_value)
                if employee_lookup_mode == "pesel"
                else db_service.employee_id_by_nr(employee_lookup_value)
            )
            if employee_id is None:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.ERROR,
                        message=f"Сотрудник с {lookup_field}={employee_lookup_value} не найден.",
                        field_name=lookup_field,
                    )
                )
                errors += 1
                continue

            if not db_service.urzad_exists_by_name(urzad_name):
                if not code_from_reference:
                    _register_missing_urzad(missing_urzedy, urzad_name)
                    rows.append(
                        ValidationRow(
                            index=idx,
                            status=RowStatus.ERROR,
                            message=(
                                f"Urząd '{urzad_name}' не найден в БД и отсутствует в "
                                "urzedy_reference.json. Добавьте его в словарь."
                            ),
                            field_name="nazwa Urząd Skarbowy",
                        )
                    )
                    errors += 1
                    continue
                to_create_urzedy += 1
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=f"Urząd '{resolved_name}' будет создан по коду {code_from_reference}.",
                        field_name="nazwa Urząd Skarbowy",
                    )
                )
            elif db_service.address_exists_on_date(int(employee_id), row_data_od):
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.WARNING,
                        message=(
                            f"У сотрудника уже есть адрес на DATA_OD={row_data_od}. "
                            "Будет выбрана следующая свободная дата."
                        ),
                        field_name="Od dnia",
                    )
                )
            else:
                rows.append(
                    ValidationRow(
                        index=idx,
                        status=RowStatus.OK,
                        message=f"OK: переезд для {lookup_field}={employee_lookup_value}.",
                        field_name=lookup_field,
                    )
                )
        else:
            rows.append(
                ValidationRow(
                    index=idx,
                    status=RowStatus.OK,
                    message=f"OK: переезд для {lookup_field}={employee_lookup_value}.",
                    field_name=lookup_field,
                )
            )

        payload = row.to_dict()
        payload["employee_id"] = employee_id
        payload["data_od"] = row_data_od
        payload["urzad_name_from_reference"] = ref_name
        payload["urzad_code_from_reference"] = code_from_reference
        importable_rows.append(payload)

    return CheckInResult(
        rows=rows,
        to_create_urzedy=to_create_urzedy,
        to_create_links=len(importable_rows),
        skipped_links=0,
        errors=errors,
        importable_rows=importable_rows,
        missing_urzedy=sorted(missing_urzedy),
    )


def summarize_result(result: CheckInResult) -> dict[str, Any]:
    return {
        "to_create_urzedy": result.to_create_urzedy,
        "to_create_links": result.to_create_links,
        "skipped_links": result.skipped_links,
        "errors": result.errors,
        "total_rows": len(result.rows),
    }

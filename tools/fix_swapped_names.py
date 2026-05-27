#!/usr/bin/env python
"""Wykrywanie i naprawa zamienionych miejscami IMIE_1 / NAZWISKO w payroll system.

Użycie:
  python tools/fix_swapped_names.py              # podgląd (dry-run)
  python tools/fix_swapped_names.py --apply      # zapis w bazie payroll system
  python tools/fix_swapped_names.py --min-confidence 0.7
  python tools/fix_swapped_names.py --export candidates.csv

Opcjonalnie używa CRM (tylko odczyt) do potwierdzenia po PESEL.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.config_loader import load_db_config
from db.name_swap import (
    apply_payroll_swaps,
    detect_swapped,
    fetch_payroll_employees,
)
from db.service import DatabaseService


def _load_crm_by_pesel() -> dict[str, tuple[str, str]]:
    try:
        from crm.mysql_client import connect_mysql
        from crm.settings import load_crm_settings
        from crm.tunnel import SshTunnel
    except ImportError:
        return {}

    settings = load_crm_settings()
    if settings.validate():
        print("CRM: pominięto (brak konfiguracji):", "; ".join(settings.validate()))
        return {}

    out: dict[str, tuple[str, str]] = {}
    tunnel = SshTunnel(settings)
    try:
        tunnel.start()
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pesel_number, name, surname
                    FROM employees
                    WHERE pesel_number IS NOT NULL
                      AND pesel_number NOT LIKE 'eyJ%%'
                      AND LENGTH(TRIM(pesel_number)) = 11
                    """
                )
                for row in cur.fetchall():
                    pesel = str(row.get("pesel_number") or "").strip()
                    if not pesel.isdigit():
                        continue
                    out[pesel] = (
                        str(row.get("name") or "").strip(),
                        str(row.get("surname") or "").strip(),
                    )
    except Exception as exc:
        print(f"CRM: pominięto ({exc})")
        return {}
    finally:
        tunnel.stop()

    print(f"CRM: załadowano {len(out)} pracowników z PESEL")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Naprawa zamienionych IMIE_1 i NAZWISKO w PRACOWNIK (payroll system)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Wykonaj UPDATE w bazie (domyślnie tylko podgląd)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.62,
        metavar="0.0-1.0",
        help="Minimalna pewność detekcji (domyślnie 0.62)",
    )
    parser.add_argument(
        "--no-crm",
        action="store_true",
        help="Nie ładuj danych z CRM do weryfikacji PESEL",
    )
    parser.add_argument(
        "--export",
        metavar="CSV",
        help="Zapisz listę kandydatów do pliku CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maks. liczba wierszy na liście (0 = bez limitu)",
    )
    args = parser.parse_args()

    db_config = load_db_config()
    service = DatabaseService(db_config)
    ok, msg = service.test_connection()
    if not ok:
        print(f"Błąd połączenia payroll system: {msg}")
        return 1
    print(f"payroll system: {msg}")

    rows = fetch_payroll_employees(service.engine)
    print(f"Załadowano {len(rows)} pracowników z imieniem lub nazwiskiem")

    crm_by_pesel = {} if args.no_crm else _load_crm_by_pesel()
    candidates = detect_swapped(
        rows,
        crm_by_pesel=crm_by_pesel or None,
        min_confidence=args.min_confidence,
    )
    print(f"\nWykryto {len(candidates)} rekordow do zamiany IMIE_1 <-> NAZWISKO")

    if args.export:
        path = Path(args.export)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(
                [
                    "id",
                    "pesel",
                    "imie_przed",
                    "nazwisko_przed",
                    "imie_po",
                    "nazwisko_po",
                    "pewnosc",
                    "powody",
                ]
            )
            for c in candidates:
                w.writerow(
                    [
                        c.id_pracownika,
                        c.pesel,
                        c.imie_before,
                        c.nazwisko_before,
                        c.imie_after,
                        c.nazwisko_after,
                        c.confidence,
                        ",".join(c.reasons),
                    ]
                )
        print(f"Zapisano: {path.resolve()}")

    show = candidates if args.limit <= 0 else candidates[: args.limit]
    for c in show:
        pesel_part = f" PESEL={c.pesel}" if c.pesel else ""
        print(
            f"  id={c.id_pracownika}{pesel_part}  "
            f"[{c.confidence:.2f} {','.join(c.reasons)}]  "
            f"'{c.nazwisko_before} {c.imie_before}' -> '{c.nazwisko_after} {c.imie_after}'"
        )
    if args.limit > 0 and len(candidates) > args.limit:
        print(f"  … i jeszcze {len(candidates) - args.limit}")

    if not candidates:
        return 0

    if args.apply:
        updated = apply_payroll_swaps(service.engine, candidates, dry_run=False)
        print(f"\nZaktualizowano wierszy: {updated} / {len(candidates)}")
        if updated < len(candidates):
            print(
                "Uwaga: część rekordów mogła się zmienić od podglądu "
                "(UPDATE tylko gdy imię/nazwisko bez zmian)."
            )
    else:
        print("\nTryb podglądu. Aby zapisać: python tools/fix_swapped_names.py --apply")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Polska sistema podatkowa i ZUS — parametry i algorytmy na rok 2026.

Moduł zawiera:
  - Stałe makroekonomiczne (minimalna płaca, średnia prognozowana, limity)
  - Obliczenia składek ZUS społecznych (emerytalne, rentowe, chorobowe, wypadkowe, FP, FGŚP)
  - Obliczenia składki zdrowotnej dla wszystkich form opodatkowania
  - Obliczenia zaliczki PIT (skala ogólna, liniowy, ryczałt)
  - Hierarchia ulg JDG (Ulga na Start, Preferencyjny ZUS, Mały ZUS Plus)
  - Reguły zaokrąglania zgodne z przepisami ZUS i US
  - Logika umów cywilnoprawnych (zlecenie, dzieło)

Źródła legislacyjne:
  - Ustawa o systemie ubezpieczeń społecznych (Dz.U. 1998 nr 137 poz. 887 ze zm.)
  - Ustawa o świadczeniach opieki zdrowotnej (Dz.U. 2022 poz. 2561 ze zm.)
  - Ustawa o podatku dochodowym od osób fizycznych (Dz.U. 1991 nr 80 poz. 350 ze zm.)
  - Obwieszczenie MRiPS w sprawie kwoty ograniczenia rocznej podstawy (M.P. 2025)
  - Obwieszczenie GUS w sprawie przeciętnego wynagrodzenia (M.P. 2026)
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_UP
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 1. Stałe makroekonomiczne 2026
# ─────────────────────────────────────────────────────────────────────────────

# § Minimalna płaca i stawka godzinowa (od 1 stycznia 2026)
MINIMALNA_PLACA_MIESIECZNA = Decimal("4806.00")
MINIMALNA_STAWKA_GODZINOWA = Decimal("31.40")

# § Średnia prognozowana — baza dla "Dużego ZUS" i limitu 30-krotności
SREDNIE_WYNAGRODZENIE_PROGNOZOWANE = Decimal("9420.00")

# § Średnia sektorowa IV kw. 2025 — baza zdrowotna dla ryczałtu
SREDNIE_WYNAGRODZENIE_SEKTOROWE_Q4_2025 = Decimal("9228.64")

# § Limit 30-krotności — po przekroczeniu zerowane emerytalne i rentowe
# Wzór: 30 × 9420 PLN
ROCZNA_PODSTAWA_LIMIT_30X = Decimal("282600.00")

# § Progi PIT (skala ogólna)
PIT_KWOTA_WOLNA = Decimal("30000.00")
PIT_PROG_I_GRANICA = Decimal("120000.00")
PIT_KWOTA_ZMNIEJSZAJACA_ROK = Decimal("3600.00")
PIT_KWOTA_ZMNIEJSZAJACA_MIES = Decimal("300.00")
PIT_STAWKA_I_PROCENT = Decimal("12")
PIT_STAWKA_II_PROCENT = Decimal("32")
PIT_PODATEK_NA_GRANICY = Decimal("10800.00")  # 120000 × 12% − 3600

# § Ulga dla młodych (do 26 r.ż.)
ULGA_MLODYCH_LIMIT = Decimal("85528.00")
ULGA_MLODYCH_WIEK = 26  # przychody do dnia 26. urodzin

# § Progi zdrowotne dla ryczałtu (baza = % z SREDNIE_WYNAGRODZENIE_SEKTOROWE_Q4_2025)
RYCZALT_ZDROWIE_PROGI = (
    Decimal("60000.00"),
    Decimal("300000.00"),
)
RYCZALT_ZDROWIE_BAZA_PROG1 = (SREDNIE_WYNAGRODZENIE_SEKTOROWE_Q4_2025 * Decimal("0.60"))
RYCZALT_ZDROWIE_BAZA_PROG2 = SREDNIE_WYNAGRODZENIE_SEKTOROWE_Q4_2025
RYCZALT_ZDROWIE_BAZA_PROG3 = (SREDNIE_WYNAGRODZENIE_SEKTOROWE_Q4_2025 * Decimal("1.80"))

# § Minimalne składki zdrowotne
# Rok składkowy trwa 1.02.2026 – 31.01.2027; styczeń 2026 rozliczany po starych zasadach
MIN_ZDROWOTNA_STYCZEN_2026 = Decimal("314.96")   # 9% × 3 499,50 (stara minimalna)
MIN_ZDROWOTNA_OD_LUTY_2026 = Decimal("432.54")   # 9% × 4 806,00

# § Bazy JDG
PREF_ZUS_BAZA = MINIMALNA_PLACA_MIESIECZNA * Decimal("0.30")  # 1 441,80
MALY_ZUS_PLUS_BAZA_MIN = PREF_ZUS_BAZA                         # 1 441,80
MALY_ZUS_PLUS_BAZA_MAX = SREDNIE_WYNAGRODZENIE_PROGNOZOWANE * Decimal("0.60")  # 5 652,00
DUZY_ZUS_BAZA = SREDNIE_WYNAGRODZENIE_PROGNOZOWANE              # 9 420,00


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 2. Stawki składek ZUS (2026 — bez zmian względem 2025)
# ─────────────────────────────────────────────────────────────────────────────

class StawkiZUS:
    """Procentowe stawki składek ZUS. Wartości jako Decimal dla precyzji."""
    EMERYTALNE_LACZNIE = Decimal("19.52")
    EMERYTALNE_UBEZPIECZONY = Decimal("9.76")
    EMERYTALNE_PLATNIK = Decimal("9.76")

    RENTOWE_LACZNIE = Decimal("8.00")
    RENTOWE_UBEZPIECZONY = Decimal("1.50")
    RENTOWE_PLATNIK = Decimal("6.50")

    CHOROBOWE = Decimal("2.45")   # wyłącznie ubezpieczony; dobrowolne dla JDG/zlecenie
    WYPADKOWE_MALE_FIRMY = Decimal("1.67")  # dla płatników zgłaszających < 10 ubezp.

    FP = Decimal("2.45")     # Fundusz Pracy — płatnik
    FGSP = Decimal("0.10")   # Fundusz Gwarantowanych Świadczeń Pracowniczych — płatnik
    FEP = Decimal("1.50")    # Fundusz Emerytur Pomostowych (praca w warunkach szczególnych)

    ZDROWOTNA_SKALA = Decimal("9.00")
    ZDROWOTNA_LINIOWY = Decimal("4.90")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 3. Typy wyliczeniowe
# ─────────────────────────────────────────────────────────────────────────────

class FormaOpodatkowania(str, Enum):
    SKALA = "skala"
    LINIOWY = "liniowy"
    RYCZALT = "ryczalt"
    KARTA_PODATKOWA = "karta"


class StatusJDG(str, Enum):
    """Hierarchia ulg składkowych dla jednoosobowej działalności."""
    ULGA_NA_START = "ulga_na_start"
    PREFERENCYJNY_ZUS = "preferencyjny_zus"
    MALY_ZUS_PLUS = "maly_zus_plus"
    DUZY_ZUS = "duzy_zus"


class RodzajUmowy(str, Enum):
    UMOWA_O_PRACE = "prace"
    UMOWA_ZLECENIE = "zlecenie"
    UMOWA_O_DZIELO = "dzielo"
    JDG = "jdg"


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 4. Zaokrąglenia
# ─────────────────────────────────────────────────────────────────────────────

def zaokr_zus(kwota: Decimal) -> Decimal:
    """Zaokrąglenie składek ZUS/zdrowotnej do 2 miejsc po przecinku (HALF_UP).

    Stosowane dla: emerytalne, rentowe, chorobowe, wypadkowe, FP, FGŚP, zdrowotna.
    Przykład: 150,505 → 150,51 | 150,504 → 150,50
    """
    return kwota.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def zaokr_pit(kwota: Decimal) -> Decimal:
    """Zaokrąglenie podstawy opodatkowania i zaliczki PIT do pełnych złotych (HALF_DOWN).

    Stosowane dla: podstawa_opodatkowania, podatek_naliczony, zaliczka_PIT.
    Przykład: 153,50 → 153 (nie 154!) | 153,51 → 154
    """
    return kwota.quantize(Decimal("1"), rounding=ROUND_HALF_DOWN)


def zaokr_pit_up(kwota: Decimal) -> Decimal:
    """Zaokrąglenie HALF_UP do pełnych złotych — dla kwoty podatku ryczałtowego."""
    return kwota.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 5. Składki ZUS — obliczenia
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkladkiZUSUbezpieczonego:
    """Składki ZUS potrącane z wynagrodzenia ubezpieczonego."""
    emerytalne: Decimal = Decimal("0")
    rentowe: Decimal = Decimal("0")
    chorobowe: Decimal = Decimal("0")

    @property
    def razem(self) -> Decimal:
        return zaokr_zus(self.emerytalne + self.rentowe + self.chorobowe)


@dataclass
class SkladkiZUSPlatnika:
    """Składki ZUS finansowane przez płatnika (pracodawcę/zleceniodawcę)."""
    emerytalne: Decimal = Decimal("0")
    rentowe: Decimal = Decimal("0")
    wypadkowe: Decimal = Decimal("0")
    fp: Decimal = Decimal("0")
    fgsp: Decimal = Decimal("0")

    @property
    def razem(self) -> Decimal:
        return zaokr_zus(
            self.emerytalne + self.rentowe + self.wypadkowe + self.fp + self.fgsp
        )


def oblicz_skladki_zus_pracownik(
    brutto: Decimal,
    *,
    chorobowe_aktywne: bool = True,
    wypadkowe_proc: Decimal = StawkiZUS.WYPADKOWE_MALE_FIRMY,
    fp_aktywne: bool = True,
    fgsp_aktywne: bool = True,
    podstawa_limit_narastajaco: Decimal = Decimal("0"),
) -> tuple[SkladkiZUSUbezpieczonego, SkladkiZUSPlatnika]:
    """Oblicza składki ZUS dla umowy o pracę lub zlecenia (pełny ZUS).

    Parametry:
        brutto: miesięczne wynagrodzenie brutto (baza składek).
        chorobowe_aktywne: False = składka nie naliczana (dobrowolna dla JDG/zlecenie).
        wypadkowe_proc: stawka wypadkowa płatnika (domyślnie 1,67% dla małych firm).
        fp_aktywne: czy naliczać Fundusz Pracy (False np. dla pracowników pow. 55/60 lat).
        fgsp_aktywne: czy naliczać FGŚP.
        podstawa_limit_narastajaco: suma podstaw od początku roku (do sprawdzenia limitu 30x).

    Zwraca:
        Krotka (składki ubezpieczonego, składki płatnika).
    """
    # Ograniczenie rocznej podstawy emerytalno-rentowej (limit 30-krotności)
    pozostaly_limit = max(
        Decimal("0"), ROCZNA_PODSTAWA_LIMIT_30X - podstawa_limit_narastajaco
    )
    baza_er = min(brutto, pozostaly_limit)

    # Ubezpieczony
    ubezp = SkladkiZUSUbezpieczonego(
        emerytalne=zaokr_zus(baza_er * StawkiZUS.EMERYTALNE_UBEZPIECZONY / 100),
        rentowe=zaokr_zus(baza_er * StawkiZUS.RENTOWE_UBEZPIECZONY / 100),
        chorobowe=zaokr_zus(brutto * StawkiZUS.CHOROBOWE / 100) if chorobowe_aktywne else Decimal("0"),
    )

    # Płatnik
    platnik = SkladkiZUSPlatnika(
        emerytalne=zaokr_zus(baza_er * StawkiZUS.EMERYTALNE_PLATNIK / 100),
        rentowe=zaokr_zus(baza_er * StawkiZUS.RENTOWE_PLATNIK / 100),
        wypadkowe=zaokr_zus(brutto * wypadkowe_proc / 100),
        fp=zaokr_zus(brutto * StawkiZUS.FP / 100) if fp_aktywne else Decimal("0"),
        fgsp=zaokr_zus(brutto * StawkiZUS.FGSP / 100) if fgsp_aktywne else Decimal("0"),
    )

    return ubezp, platnik


def oblicz_skladki_zus_jdg(
    baza: Decimal,
    *,
    chorobowe_aktywne: bool = True,
    wypadkowe_proc: Decimal = StawkiZUS.WYPADKOWE_MALE_FIRMY,
    podstawa_limit_narastajaco: Decimal = Decimal("0"),
) -> SkladkiZUSUbezpieczonego:
    """Oblicza składki ZUS dla JDG (jednoosobowa działalność gospodarcza).

    Baza składek jest z góry zadana (9 420 PLN, 1 441,80 PLN lub wartość
    z Małego ZUS Plus), a nie wynika bezpośrednio z dochodu.
    """
    pozostaly_limit = max(
        Decimal("0"), ROCZNA_PODSTAWA_LIMIT_30X - podstawa_limit_narastajaco
    )
    baza_er = min(baza, pozostaly_limit)

    return SkladkiZUSUbezpieczonego(
        emerytalne=zaokr_zus(baza_er * StawkiZUS.EMERYTALNE_UBEZPIECZONY / 100),
        rentowe=zaokr_zus(baza_er * StawkiZUS.RENTOWE_UBEZPIECZONY / 100),
        chorobowe=zaokr_zus(baza * StawkiZUS.CHOROBOWE / 100) if chorobowe_aktywne else Decimal("0"),
    )


def oblicz_baze_proporcjonalna(
    baza_pelna: Decimal,
    rok: int,
    miesiac: int,
    dni_przepracowane: int,
) -> Decimal:
    """Proporcjonalne zmniejszenie bazy ZUS przy chorobie/niezdolności.

    Wzór: baza_pelna / dni_miesiaca × dni_przepracowane.
    Pośredni wynik dzielenia NIE jest zaokrąglany — zaokrąglenie po mnożeniu.
    Dotyczy tylko składek społecznych; podstawa zdrowotna zawsze pełna.

    Uwaga: prawidłowa kolejność to: najpierw dziel, potem mnóż, potem zaokrąglij.
    """
    dni_miesiaca = Decimal(str(calendar.monthrange(rok, miesiac)[1]))
    if dni_miesiaca == 0 or dni_przepracowane <= 0:
        return Decimal("0")
    if dni_przepracowane >= int(dni_miesiaca):
        return baza_pelna
    # Celowo brak zaokrąglenia pośredniego — dopiero finał
    wynik_raw = baza_pelna / dni_miesiaca * Decimal(str(dni_przepracowane))
    return zaokr_zus(wynik_raw)


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 6. Składka zdrowotna
# ─────────────────────────────────────────────────────────────────────────────

def _min_zdrowotna_dla_miesiaca(miesiac: int, rok: int = 2026) -> Decimal:
    """Minimalna składka zdrowotna z uwzględnieniem przejścia roku składkowego.

    Rok składkowy: 1.02.2026 – 31.01.2027.
    W styczniu 2026 obowiązuje stara minimalna (5% × 3499,50 = 314,96 PLN).
    """
    if rok == 2026 and miesiac == 1:
        return MIN_ZDROWOTNA_STYCZEN_2026
    return MIN_ZDROWOTNA_OD_LUTY_2026


def oblicz_zdrowotna_skala_liniowy(
    dochod: Decimal,
    forma: FormaOpodatkowania,
    miesiac: int,
    rok: int = 2026,
) -> Decimal:
    """Składka zdrowotna dla skali podatkowej i podatku liniowego.

    Stawka 9% (skala) lub 4,9% (liniowy) od dochodu. Minimalna = 9% × min. płacy
    (od II.2026: 432,54 PLN; I.2026: 314,96 PLN).

    Uwaga: dochód = przychód − koszty − składki ZUS społeczne. Nie jest identyczny
    z dochodem do PIT — nie odlicza się tu kwoty wolnej.
    """
    if forma == FormaOpodatkowania.SKALA:
        stawka = StawkiZUS.ZDROWOTNA_SKALA
    elif forma == FormaOpodatkowania.LINIOWY:
        stawka = StawkiZUS.ZDROWOTNA_LINIOWY
    else:
        raise ValueError(f"Forma {forma} nie jest obsługiwana przez tę funkcję.")

    kwota_naliczona = zaokr_zus(dochod * stawka / 100)
    minimum = _min_zdrowotna_dla_miesiaca(miesiac, rok)
    return max(kwota_naliczona, minimum)


def oblicz_zdrowotna_ryczalt(
    przychod_narastajaco: Decimal,
    miesiac: int,
    rok: int = 2026,
) -> Decimal:
    """Składka zdrowotna dla ryczałtu ewidencjonowanego.

    Próg zależy od ROCZNEGO przychodu narastającego. Program musi
    dynamicznie zmieniać próg po przekroczeniu 60 000 PLN i 300 000 PLN.

    Roczne wyrównanie: jeśli przedsiębiorca przejdzie do wyższego progu,
    w rocznej deklaracji (DRA II) dopłaci różnicę za miesiące niższego progu.
    """
    _ = rok  # parametr zachowany dla spójności API (wartości bazowe są z 2025 Q4)

    prog1, prog2 = RYCZALT_ZDROWIE_PROGI
    if przychod_narastajaco <= prog1:
        baza = RYCZALT_ZDROWIE_BAZA_PROG1
    elif przychod_narastajaco <= prog2:
        baza = RYCZALT_ZDROWIE_BAZA_PROG2
    else:
        baza = RYCZALT_ZDROWIE_BAZA_PROG3

    return zaokr_zus(baza * StawkiZUS.ZDROWOTNA_SKALA / 100)


def oblicz_zdrowotna_pracownik(
    brutto: Decimal,
    skladki_ubezpieczonego: SkladkiZUSUbezpieczonego,
    miesiac: int,
    rok: int = 2026,
) -> Decimal:
    """Składka zdrowotna pracownika na umowie o pracę.

    Podstawa = brutto − składki ZUS ubezpieczonego (emerytalne + rentowe + chorobowe).
    """
    podstawa = brutto - skladki_ubezpieczonego.razem
    kwota = zaokr_zus(podstawa * StawkiZUS.ZDROWOTNA_SKALA / 100)
    minimum = _min_zdrowotna_dla_miesiaca(miesiac, rok)
    return max(kwota, minimum)


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 7. PIT — zaliczka miesięczna
# ─────────────────────────────────────────────────────────────────────────────

def oblicz_zaliczke_pit_skala(
    dochod_miesiac: Decimal,
    *,
    kup: Decimal = Decimal("250"),
    stosuj_kwote_zmniejszajaca: bool = True,
    dochod_narastajaco_prev: Decimal = Decimal("0"),
    ulga_mlodych: bool = False,
) -> Decimal:
    """Miesięczna zaliczka PIT dla skali podatkowej (umowa o pracę / JDG).

    Parametry:
        dochod_miesiac: dochód bieżącego miesiąca po odjęciu składek ZUS.
        kup: koszty uzyskania przychodu (250 PLN standard, 300 PLN podwyższone).
        stosuj_kwote_zmniejszajaca: True = odejmij 300 PLN/mies. od zaliczki.
        dochod_narastajaco_prev: skumulowany dochód do opodatkowania z poprzednich
            miesięcy (do wykrycia przejścia przez próg 120 000 PLN w ciągu roku).
        ulga_mlodych: True = PIT = 0 (do 26 r.ż., do limitu 85 528 PLN).

    Ważna zasada implementacji:
        Miesięczna kwota wolna od podatku (30 000 PLN/rok) NIE zeruje zaliczki
        bezpośrednio. Mechanizm jej uwzględnienia to miesięczna kwota zmniejszająca
        = 300 PLN (3 600 PLN ÷ 12). Przykład: podstawa 2 500 PLN → podatek naliczony
        = 300 PLN → po odjęciu kwoty zmniejszającej = 0 PLN. Dla wyższych podstaw
        (np. 5 000 PLN → 600 PLN − 300 PLN = 300 PLN zaliczki) zawsze wymagana jest
        kwota zmniejszająca, a nie zerowanie podatku.

    Zwraca: zaliczkę PIT zaokrągloną do pełnych złotych (HALF_DOWN).
    """
    if ulga_mlodych:
        return Decimal("0")

    podstawa_raw = dochod_miesiac - kup
    if podstawa_raw <= 0:
        return Decimal("0")

    podstawa = zaokr_pit(podstawa_raw)
    dochod_po = dochod_narastajaco_prev + podstawa

    if dochod_narastajaco_prev >= PIT_PROG_I_GRANICA:
        # Całość bieżącego miesiąca w II progu (32%) — brak kwoty zmniejszającej
        podatek = zaokr_zus(podstawa * PIT_STAWKA_II_PROCENT / 100)
        zaliczka = zaokr_pit(podatek)
    elif dochod_po > PIT_PROG_I_GRANICA:
        # Przejście przez próg I → II w bieżącym miesiącu
        czesc_w_i_progu = PIT_PROG_I_GRANICA - dochod_narastajaco_prev
        czesc_ponad = dochod_po - PIT_PROG_I_GRANICA
        podatek = zaokr_zus(
            czesc_w_i_progu * PIT_STAWKA_I_PROCENT / 100
            + czesc_ponad * PIT_STAWKA_II_PROCENT / 100
        )
        # Kwota zmniejszająca stosowana tylko do części w I progu
        zmniejszenie = PIT_KWOTA_ZMNIEJSZAJACA_MIES if stosuj_kwote_zmniejszajaca else Decimal("0")
        zaliczka = max(Decimal("0"), zaokr_pit(podatek - zmniejszenie))
    else:
        # Cały bieżący miesiąc w I progu (12%)
        # Kwota wolna od podatku (30 000 PLN/rok) realizowana przez kwotę zmniejszającą 300 PLN/mies.
        # — NIE przez zerowanie podatku przy niskim dochodzie narastającym.
        podatek = zaokr_zus(podstawa * PIT_STAWKA_I_PROCENT / 100)
        zmniejszenie = PIT_KWOTA_ZMNIEJSZAJACA_MIES if stosuj_kwote_zmniejszajaca else Decimal("0")
        zaliczka = max(Decimal("0"), zaokr_pit(podatek - zmniejszenie))

    return zaliczka


def oblicz_zaliczke_pit_liniowy(
    dochod_miesiac: Decimal,
    *,
    kup: Decimal = Decimal("0"),
) -> Decimal:
    """Miesięczna zaliczka PIT dla podatku liniowego (JDG, 19%).

    Brak kwoty wolnej, brak kwoty zmniejszającej — płaskie 19%.
    """
    podstawa_raw = dochod_miesiac - kup
    if podstawa_raw <= 0:
        return Decimal("0")
    podstawa = zaokr_pit(podstawa_raw)
    podatek = zaokr_zus(podstawa * Decimal("19") / 100)
    return zaokr_pit(podatek)


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 8. Hierarchia ulg JDG
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KonfiguracjaJDG:
    """Parametry wejściowe do wyznaczenia statusu ulgi JDG."""
    data_rozpoczecia: date
    # Dochód z poprzedniego roku (dla Małego ZUS Plus)
    dochod_rok_poprzedni: Optional[Decimal] = None
    # Liczba dni prowadzenia działalności w roku poprzednim (dla MZP)
    dni_dzialalnosci_rok_poprzedni: Optional[int] = None
    # Historia miesięcy z preferencyjnym ZUS (do sprawdzenia limitu 24 mies.)
    miesiace_pref_zus: int = 0
    # Historia miesięcy z Małym ZUS Plus w bieżącym oknie 60 mies.
    miesiace_maly_zus_plus_w_oknie: int = 0


def wyznacz_status_jdg(
    konfiguracja: KonfiguracjaJDG,
    data_rozliczenia: date,
) -> StatusJDG:
    """Wyznacza aktualny status ulgi składkowej JDG.

    Kolejność sprawdzania (ważna):
    1. Ulga na Start — 6 pełnych miesięcy kalendarzowych po starcie.
       Miesiąc startu nie liczy się, jeśli firma nie ruszyła 1. dnia.
    2. Preferencyjny ZUS — do 24 miesięcy.
    3. Mały ZUS Plus — do 36 miesięcy w każdych 60 miesiącach prowadzenia działalności.
    4. Duży ZUS — wariant domyślny.
    """
    start = konfiguracja.data_rozpoczecia

    # ── Ulga na Start ──────────────────────────────────────────────────────
    # Liczymy liczbę pełnych miesięcy kalendarzowych NASTĘPUJĄCYCH po starcie.
    # Jeśli start = 1. dnia miesiąca, ten miesiąc LICZY SIĘ do limitu 6.
    if start.day == 1:
        first_full_month_start = start
    else:
        # Miesiąc startu nie pełny — pierwszym pełnym jest następny
        if start.month == 12:
            first_full_month_start = date(start.year + 1, 1, 1)
        else:
            first_full_month_start = date(start.year, start.month + 1, 1)

    # Koniec ulgi na start: po 6 pełnych miesiącach
    mies = first_full_month_start.month + 6
    rok_end = first_full_month_start.year + (mies - 1) // 12
    mies_end = (mies - 1) % 12 + 1
    ulga_start_koniec = date(rok_end, mies_end, 1)

    if data_rozliczenia < ulga_start_koniec:
        return StatusJDG.ULGA_NA_START

    # ── Preferencyjny ZUS ──────────────────────────────────────────────────
    if konfiguracja.miesiace_pref_zus < 24:
        return StatusJDG.PREFERENCYJNY_ZUS

    # ── Mały ZUS Plus ──────────────────────────────────────────────────────
    if konfiguracja.miesiace_maly_zus_plus_w_oknie < 36:
        return StatusJDG.MALY_ZUS_PLUS

    return StatusJDG.DUZY_ZUS


def oblicz_baze_maly_zus_plus(
    dochod_rok_poprzedni: Decimal,
    dni_dzialalnosci: int,
) -> Decimal:
    """Oblicza podstawę wymiaru składek dla Małego ZUS Plus.

    Wzór (art. 18c ustawy o sus):
        podstawa = (dochod_rok_poprzedni × 30 / dni_dzialalnosci) × 0.5

    Wynik zaokrąglany do 1 grosza i ograniczany do przedziału
    [MALY_ZUS_PLUS_BAZA_MIN, MALY_ZUS_PLUS_BAZA_MAX].
    """
    if dni_dzialalnosci <= 0:
        raise ValueError("Liczba dni prowadzenia działalności musi być > 0.")

    # Nie zaokrąglamy kroku pośredniego — dopiero wynik końcowy
    sredni_mies = dochod_rok_poprzedni * 30 / Decimal(str(dni_dzialalnosci))
    baza_raw = zaokr_zus(sredni_mies * Decimal("0.5"))
    baza = max(MALY_ZUS_PLUS_BAZA_MIN, min(baza_raw, MALY_ZUS_PLUS_BAZA_MAX))
    return baza


def baza_zus_dla_statusu(status: StatusJDG) -> Optional[Decimal]:
    """Zwraca stałą bazę ZUS dla danego statusu JDG (None = dynamiczna lub zerowa)."""
    match status:
        case StatusJDG.ULGA_NA_START:
            return None  # Brak składek społecznych
        case StatusJDG.PREFERENCYJNY_ZUS:
            return PREF_ZUS_BAZA
        case StatusJDG.DUZY_ZUS:
            return DUZY_ZUS_BAZA
        case StatusJDG.MALY_ZUS_PLUS:
            return None  # Wymaga obliczenia przez oblicz_baze_maly_zus_plus()
        case _:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 9. Umowy cywilnoprawne
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WynikUmowyCywilnoprawnej:
    """Wynik kalkulacji wynagrodzenia z umowy zlecenia lub o dzieło."""
    brutto: Decimal
    emerytalne_ubezp: Decimal
    rentowe_ubezp: Decimal
    chorobowe_ubezp: Decimal
    emerytalne_platnik: Decimal
    rentowe_platnik: Decimal
    wypadkowe_platnik: Decimal
    fp_platnik: Decimal
    fgsp_platnik: Decimal
    zdrowotna: Decimal
    kup_kwota: Decimal
    podstawa_opodatkowania: Decimal
    zaliczka_pit: Decimal
    netto: Decimal

    @property
    def koszt_pracodawcy(self) -> Decimal:
        return zaokr_zus(
            self.brutto
            + self.emerytalne_platnik
            + self.rentowe_platnik
            + self.wypadkowe_platnik
            + self.fp_platnik
            + self.fgsp_platnik
        )

    @property
    def razem_zus_ubezpieczonego(self) -> Decimal:
        return zaokr_zus(self.emerytalne_ubezp + self.rentowe_ubezp + self.chorobowe_ubezp)


def oblicz_umowe_zlecenie(
    brutto: Decimal,
    kup_proc: Decimal,
    stawka_pit_proc: Decimal,
    *,
    emerytalne_aktywne: bool = True,
    rentowe_aktywne: bool = True,
    chorobowe_aktywne: bool = False,
    zdrowotne_aktywne: bool = True,
    fp_aktywne: bool = True,
    fgsp_aktywne: bool = True,
    wypadkowe_proc: Decimal = StawkiZUS.WYPADKOWE_MALE_FIRMY,
    podstawa_limit_narastajaco: Decimal = Decimal("0"),
    miesiac: int = 2,
    rok: int = 2026,
) -> WynikUmowyCywilnoprawnej:
    """Kalkulacja wynagrodzenia z umowy zlecenia.

    Obsługuje przypadki:
    - Pełny ZUS (emerytalne/rentowe aktywne)
    - Zbieg tytułów — etat ≥ min. płacy (emerytalne/rentowe = False)
    - Student do 26 lat (wszystkie stawki = 0%, stawka_pit_proc = 0%)

    Parametry kup_proc podawane w procentach (np. Decimal("20") = 20%).
    """
    # Ograniczenie limitu 30-krotności
    pozostaly_limit = max(Decimal("0"), ROCZNA_PODSTAWA_LIMIT_30X - podstawa_limit_narastajaco)
    baza_er = min(brutto, pozostaly_limit) if emerytalne_aktywne else Decimal("0")
    baza_rent = min(brutto, pozostaly_limit) if rentowe_aktywne else Decimal("0")

    # Składki ZUS ubezpieczonego
    emer_u = zaokr_zus(baza_er * StawkiZUS.EMERYTALNE_UBEZPIECZONY / 100)
    rent_u = zaokr_zus(baza_rent * StawkiZUS.RENTOWE_UBEZPIECZONY / 100)
    chor_u = zaokr_zus(brutto * StawkiZUS.CHOROBOWE / 100) if chorobowe_aktywne else Decimal("0")
    razem_u_raw = emer_u + rent_u + chor_u

    # Składki ZUS płatnika
    emer_p = zaokr_zus(baza_er * StawkiZUS.EMERYTALNE_PLATNIK / 100) if emerytalne_aktywne else Decimal("0")
    rent_p = zaokr_zus(baza_rent * StawkiZUS.RENTOWE_PLATNIK / 100) if rentowe_aktywne else Decimal("0")
    wypad_p = zaokr_zus(brutto * wypadkowe_proc / 100) if emerytalne_aktywne else Decimal("0")
    fp_p = zaokr_zus(brutto * StawkiZUS.FP / 100) if fp_aktywne else Decimal("0")
    fgsp_p = zaokr_zus(brutto * StawkiZUS.FGSP / 100) if fgsp_aktywne else Decimal("0")

    # Podstawa zdrowotna i składka
    # Uwaga: minimalna składka zdrowotna (432,54 PLN/mies. od II.2026) dotyczy
    # WYŁĄCZNIE JDG na skali/liniowym (art. 79a ustawy o swiadczeniach). Dla umowy
    # zlecenia stosuje się czyste 9% × podstawa, bez progu minimalnego — w
    # przeciwnym razie obliczone tutaj netto rozjeżdża się z `_calculate_umowa_financials`
    # i z `recalculate_umowa_zlecenie_from_rates`, które są źródłem prawdy dla importu.
    podstawa_zdrowia = brutto - razem_u_raw
    if zdrowotne_aktywne:
        zdrowotna = zaokr_zus(podstawa_zdrowia * StawkiZUS.ZDROWOTNA_SKALA / 100)
    else:
        zdrowotna = Decimal("0")
    _ = miesiac, rok  # zachowane w sygnaturze API; minimum stosujemy tylko w JDG

    # KUP i dochód
    kup_kwota = zaokr_zus(podstawa_zdrowia * kup_proc / 100)
    dochod_raw = podstawa_zdrowia - kup_kwota
    podstawa_op = zaokr_pit(dochod_raw)

    # PIT
    if stawka_pit_proc > 0 and podstawa_op > 0:
        podatek_raw = zaokr_zus(Decimal(str(podstawa_op)) * stawka_pit_proc / 100)
        zaliczka = zaokr_pit(podatek_raw)
    else:
        zaliczka = Decimal("0")

    # Netto
    netto = zaokr_zus(brutto - razem_u_raw - zdrowotna - zaliczka)

    return WynikUmowyCywilnoprawnej(
        brutto=brutto,
        emerytalne_ubezp=emer_u,
        rentowe_ubezp=rent_u,
        chorobowe_ubezp=chor_u,
        emerytalne_platnik=emer_p,
        rentowe_platnik=rent_p,
        wypadkowe_platnik=wypad_p,
        fp_platnik=fp_p,
        fgsp_platnik=fgsp_p,
        zdrowotna=zdrowotna,
        kup_kwota=kup_kwota,
        podstawa_opodatkowania=podstawa_op,
        zaliczka_pit=zaliczka,
        netto=netto,
    )


def oblicz_umowe_o_dzielo(
    brutto: Decimal,
    kup_proc: Decimal = Decimal("50"),
    stawka_pit_proc: Decimal = Decimal("12"),
) -> WynikUmowyCywilnoprawnej:
    """Kalkulacja wynagrodzenia z umowy o dzieło.

    Umowa o dzieło: brak składek ZUS i zdrowotnej; wyłącznie KUP i PIT.
    Domyślne 50% KUP (twórcze/autorskie). Dla nietwórczych: kup_proc=20.
    """
    kup_kwota = zaokr_zus(brutto * kup_proc / 100)
    dochod_raw = brutto - kup_kwota
    podstawa_op = zaokr_pit(dochod_raw)

    if podstawa_op > 0 and stawka_pit_proc > 0:
        podatek_raw = zaokr_zus(Decimal(str(podstawa_op)) * stawka_pit_proc / 100)
        zaliczka = zaokr_pit(podatek_raw)
    else:
        zaliczka = Decimal("0")

    netto = brutto - zaliczka

    zero = Decimal("0")
    return WynikUmowyCywilnoprawnej(
        brutto=brutto,
        emerytalne_ubezp=zero,
        rentowe_ubezp=zero,
        chorobowe_ubezp=zero,
        emerytalne_platnik=zero,
        rentowe_platnik=zero,
        wypadkowe_platnik=zero,
        fp_platnik=zero,
        fgsp_platnik=zero,
        zdrowotna=zero,
        kup_kwota=kup_kwota,
        podstawa_opodatkowania=podstawa_op,
        zaliczka_pit=zaliczka,
        netto=netto,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 10. Logika zbiegu tytułów (zbieg tytułów ubezpieczeń)
# ─────────────────────────────────────────────────────────────────────────────

def czy_pelny_zus_dla_zlecenia(
    podstawa_etatu: Optional[Decimal],
) -> bool:
    """Wyznacza, czy zleceniobiorca podlega pełnemu ZUS.

    Zasada zbiegu tytułów:
    - Jeśli zleceniobiorca ma równocześnie umowę o pracę i wynagrodzenie
      z etatu >= minimalna płaca (4 806 PLN), zlecenie wolne od ZUS społecznego.
    - W przeciwnym razie zlecenie podlega pełnemu ZUS.

    Parametry:
        podstawa_etatu: wynagrodzenie z umowy o pracę w danym miesiącu (None = brak etatu).
    """
    if podstawa_etatu is None:
        return True  # Brak etatu — pełny ZUS
    return podstawa_etatu < MINIMALNA_PLACA_MIESIECZNA


def czy_student_wolny_od_zus(
    jest_studentem: bool,
    data_urodzenia: Optional[date],
    data_wyplaty: date,
) -> bool:
    """Sprawdza, czy zleceniobiorca jest studentem/uczniem poniżej 26 lat i wolny od ZUS.

    Warunek łączny: jest aktywnym studentem/uczniem ORAZ w dniu wypłaty ma < 26 lat.
    Jeśli data_urodzenia nieznana, metoda konserwatywnie zwraca False.
    """
    if not jest_studentem or data_urodzenia is None:
        return False
    wiek = data_wyplaty.year - data_urodzenia.year
    if (data_wyplaty.month, data_wyplaty.day) < (data_urodzenia.month, data_urodzenia.day):
        wiek -= 1
    return wiek < ULGA_MLODYCH_WIEK


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 11. Artykuł 83 — ograniczenie zdrowotnej do kwoty podatku
# ─────────────────────────────────────────────────────────────────────────────

def ograniczenie_art83(
    zdrowotna_naliczona: Decimal,
    dochod: Decimal,
    kup: Decimal = Decimal("250"),
) -> Decimal:
    """Ograniczenie składki zdrowotnej do kwoty hipotetycznego podatku wg zasad z 2021 r.

    Art. 83 ustawy o świadczeniach zdrowotnych: dla osób z bardzo niskim dochodem
    składka zdrowotna nie może przekraczać kwoty podatku obliczonego według zasad
    sprzed 2022 roku (stawka 17%, z odliczeniem składki zdrowotnej 7,75%).

    Logika podwójnego obliczenia:
    1. Składka zdrowotna standardowa (9% dochodu).
    2. Hipotetyczny podatek wg zasad 2021: (dochód − KUP) × 17% − kwota_zmniejszająca_2021.
       Kwota zmniejszająca z 2021: 43,76 PLN/mies. (525,12 PLN/rok ÷ 12).

    Jeśli składka > hipotetyczny podatek, składka zostaje obniżona do jego poziomu.
    """
    STAWKA_2021 = Decimal("17")
    KWOTA_ZMNIEJSZAJACA_2021_MIES = Decimal("43.76")
    ZDROWOTNA_ODLICZALNA_2021 = Decimal("7.75")  # % stosowany do pomniejszenia podatku

    podstawa_2021_raw = dochod - kup
    if podstawa_2021_raw <= 0:
        return Decimal("0")

    podstawa_2021 = zaokr_pit(podstawa_2021_raw)
    # Hipotetyczna składka zdrowotna odliczana w 2021 (7,75% × podstawa zdrowotna)
    zdrowotna_odlicz = zaokr_zus(podstawa_2021_raw * ZDROWOTNA_ODLICZALNA_2021 / 100)
    hipotetyczny_podatek_raw = (
        zaokr_zus(Decimal(str(podstawa_2021)) * STAWKA_2021 / 100)
        - zdrowotna_odlicz
        - KWOTA_ZMNIEJSZAJACA_2021_MIES
    )
    hipotetyczny_podatek = max(Decimal("0"), zaokr_pit(hipotetyczny_podatek_raw))

    return min(zdrowotna_naliczona, hipotetyczny_podatek)


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 12. Pomocnicze funkcje kalendarza i walidacji
# ─────────────────────────────────────────────────────────────────────────────

TERMINY_PLATNOSCI = {
    "zus_dra": 20,   # Dzień miesiąca — DRA + zapłata składek
    "pit_zaliczka": 20,
    "ryczalt_zaliczka": 20,
}

def termin_platnosci_zus(rok: int, miesiac: int) -> date:
    """Termin zapłaty składek ZUS (20. następnego miesiąca).

    Jeśli 20. wypada w weekend lub święto, termin przesuwa się na następny
    dzień roboczy. Ta funkcja zwraca 20. bez korekty na dni wolne —
    korekty musi dokonać wywołujący kod (np. przez bibliotekę świąt PL).
    """
    if miesiac == 12:
        return date(rok + 1, 1, 20)
    return date(rok, miesiac + 1, 20)


TERMINY_KSEF = {
    "duze_firmy_od": date(2026, 2, 1),      # Przychody > 200 mln PLN
    "pozostale_firmy_od": date(2026, 4, 1), # Wszyscy pozostali przedsiębiorcy
}


def min_placa_na_date(dzien: date) -> Decimal:
    """Minimalna płaca obowiązująca w danym dniu (tablica obejmuje rok 2026).

    W 2026 obowiązuje jedna stawka od 1 stycznia (brak wzrostu w lipcu
    jak w poprzednich latach). Po wejściu w nowy rok kalendarzowy należy
    rozbudować tablicę — milcząca ekstrapolacja wartości 2026 doprowadziłaby
    do błędnych progów (FP, baza zdrowotna minimalna, limity JDG).
    """
    if date(2026, 1, 1) <= dzien <= date(2026, 12, 31):
        return MINIMALNA_PLACA_MIESIECZNA
    raise ValueError(
        f"Brak danych minimalnej płacy dla daty {dzien}. Zaktualizuj tablicę w tax_calc_2026."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 13. Przeliczenie wartości finansowych umowy wg stawek z BD
#            (używane do weryfikacji poprawności danych w GANG_UMOWY_CYWILNO_PRAWNE)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RecalcResult:
    """Wynik przeliczenia finansowego jednej umowy cywilnoprawnej."""
    emerytalne_zleceniobiorca: Decimal
    emerytalne_zleceniodawca: Decimal
    rentowe_zleceniobiorca: Decimal
    rentowe_zleceniodawca: Decimal
    chorobowe_zleceniobiorca: Decimal
    wypadkowe_zleceniodawca: Decimal
    zdrowotne_zleceniobiorca: Decimal
    fp_kwota: Decimal
    fgsp_kwota: Decimal
    kup_kwota: Decimal
    dochod: Decimal
    stawka_podatku: Decimal
    kwota_podatku: Decimal
    kwota_do_wyplaty: Decimal


def recalculate_umowa_zlecenie_from_rates(
    brutto: float,
    kup_proc: float,
    stawka_podatku_proc: float,
    emerytalne_proc: float,
    rentowe_u_proc: float,
    rentowe_p_proc: float,
    chorobowe_proc: float,
    wypadkowe_proc: float,
    zdrowotne_proc: float,
    fp_proc: float,
    fgsp_proc: float,
) -> RecalcResult:
    """Przelicza wartości finansowe umowy zlecenia na podstawie stawek przechowywanych w BD.

    Algorytm jest dokładnym odwzorowaniem logiki WaPro („przycisk Wylicz") przy użyciu
    Decimal zamiast float. Funkcja służy do WERYFIKACJI — porównania danych w BD
    z wartościami obliczonymi od nowa z tą samą precyzją.

    Kluczowe zasady (tożsame z _calculate_umowa_financials w service.py):
      * emerytalne dzielone równo 50/50 z sumarycznej stawki (emerytalne_proc / 2 na każdą stronę)
      * składki_zleceniobiorca_raw = suma RAW (bez zaokrąglenia pośredniego) → podstawa zdrowotna
      * KUP naliczany od (brutto − składki_zleceniobiorca_raw)
      * PIT zaokrąglany HALF_DOWN do pełnych złotych
      * netto wybierane z dwóch kandydatów wg logiki WaPro (net_raw vs net_rounded_components)
      * wartości kończące się na x,99 PLN zaokrąglane do x+1,00 PLN (WaPro display rule)
    """
    d = Decimal
    br = d(str(brutto))

    # Emerytalne dzielone równo
    emer_u_proc = d(str(emerytalne_proc)) / d("2")
    emer_u_raw = br * emer_u_proc / d("100")
    emer_p_raw = br * emer_u_proc / d("100")

    rent_u_raw = br * d(str(rentowe_u_proc)) / d("100")
    rent_p_raw = br * d(str(rentowe_p_proc)) / d("100")
    chor_u_raw = br * d(str(chorobowe_proc)) / d("100")
    wypad_p_raw = br * d(str(wypadkowe_proc)) / d("100")
    fp_raw = br * d(str(fp_proc)) / d("100")
    fgsp_raw = br * d(str(fgsp_proc)) / d("100")

    emer_u = zaokr_zus(emer_u_raw)
    emer_p = zaokr_zus(emer_p_raw)
    rent_u = zaokr_zus(rent_u_raw)
    rent_p = zaokr_zus(rent_p_raw)
    chor_u = zaokr_zus(chor_u_raw)
    wypad_p = zaokr_zus(wypad_p_raw)
    fp_kwota = zaokr_zus(fp_raw)
    fgsp_kwota = zaokr_zus(fgsp_raw)

    # Podstawa zdrowotna i KUP — od RAW (bez zaokrąglenia pośrednich składek)
    skladki_u_raw = emer_u_raw + rent_u_raw + chor_u_raw
    podstawa_raw = br - skladki_u_raw
    zdr_raw = podstawa_raw * d(str(zdrowotne_proc)) / d("100")
    kup_raw = podstawa_raw * d(str(kup_proc)) / d("100")
    dochod_raw = br - skladki_u_raw - kup_raw

    # PIT
    podstawa_op = zaokr_pit(dochod_raw)
    stawka = zaokr_zus(d(str(stawka_podatku_proc)))
    podatek_naliczony = zaokr_zus(d(str(podstawa_op)) * stawka / d("100"))
    zaliczka = zaokr_pit(podatek_naliczony)

    # Wartości kwotowe (do porównania z BD)
    skladki_u = zaokr_zus(skladki_u_raw)
    zdr = zaokr_zus(zdr_raw)
    kup = zaokr_zus(kup_raw)
    dochod = zaokr_zus(dochod_raw)

    # Netto — dwaj kandydaci (logika WaPro)
    net_raw = zaokr_zus(br - skladki_u_raw - zdr_raw - d(str(zaliczka)))
    podstawa_rd = br - zaokr_zus(skladki_u_raw)
    zdr_rd = zaokr_zus(podstawa_rd * d(str(zdrowotne_proc)) / d("100"))
    net_rdc = zaokr_zus(br - zaokr_zus(skladki_u_raw) - zdr_rd - d(str(zaliczka)))

    selected_net = net_rdc if d(str(zaliczka)) > podatek_naliczony else net_raw

    # WaPro display rule: x,99 → zaokrąglij do (x+1),00
    next_zl = selected_net.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if next_zl > selected_net and (next_zl - selected_net) <= Decimal("0.01"):
        kwota_do_wyplaty = next_zl
    else:
        kwota_do_wyplaty = selected_net

    return RecalcResult(
        emerytalne_zleceniobiorca=emer_u,
        emerytalne_zleceniodawca=emer_p,
        rentowe_zleceniobiorca=rent_u,
        rentowe_zleceniodawca=rent_p,
        chorobowe_zleceniobiorca=chor_u,
        wypadkowe_zleceniodawca=wypad_p,
        zdrowotne_zleceniobiorca=zdr,
        fp_kwota=fp_kwota,
        fgsp_kwota=fgsp_kwota,
        kup_kwota=kup,
        dochod=dochod,
        stawka_podatku=stawka,
        kwota_podatku=zaliczka,
        kwota_do_wyplaty=kwota_do_wyplaty,
    )


def recalculate_umowa_dzielo_from_rates(
    brutto: float,
    kup_proc: float,
    stawka_podatku_proc: float = 12.0,
) -> RecalcResult:
    """Przelicza wartości finansowe umowy o dzieło na podstawie stawek z BD.

    Umowa o dzieło: brak składek ZUS i zdrowotnej, KUP od pełnego brutto,
    PIT zaokrąglany HALF_DOWN. Odwzorowanie _calculate_umowa_o_dzielo_financials.
    """
    d = Decimal
    br = d(str(brutto))

    kup_raw = br * d(str(kup_proc)) / d("100")
    dochod_raw = br - kup_raw
    podstawa_op = zaokr_pit(dochod_raw)
    stawka = d(str(stawka_podatku_proc))
    podatek_naliczony = zaokr_zus(d(str(podstawa_op)) * stawka / d("100"))
    zaliczka = zaokr_pit(podatek_naliczony)

    kup = zaokr_zus(kup_raw)
    dochod = zaokr_zus(dochod_raw)

    # Brak zdrowotnej i składek — netto to po prostu brutto − zaliczka PIT.
    # Nie ma sensu rozróżniać net_raw / net_rounded_components jak w zleceniu.
    selected_net = zaokr_zus(br - d(str(zaliczka)))

    next_zl = selected_net.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if next_zl > selected_net and (next_zl - selected_net) <= Decimal("0.01"):
        kwota_do_wyplaty = next_zl
    else:
        kwota_do_wyplaty = selected_net

    zero = Decimal("0")
    return RecalcResult(
        emerytalne_zleceniobiorca=zero,
        emerytalne_zleceniodawca=zero,
        rentowe_zleceniobiorca=zero,
        rentowe_zleceniodawca=zero,
        chorobowe_zleceniobiorca=zero,
        wypadkowe_zleceniodawca=zero,
        zdrowotne_zleceniobiorca=zero,
        fp_kwota=zero,
        fgsp_kwota=zero,
        kup_kwota=kup,
        dochod=dochod,
        stawka_podatku=zaokr_zus(stawka),
        kwota_podatku=zaliczka,
        kwota_do_wyplaty=kwota_do_wyplaty,
    )


# Mapowanie kolumn BD → pola RecalcResult (dla automatycznego porównania)
DB_TO_RECALC_FIELD_MAP: dict[str, str] = {
    "EMERYTALNE_ZLECENIOBIORCA": "emerytalne_zleceniobiorca",
    "EMERYTALNE_ZLECENIODAWCA": "emerytalne_zleceniodawca",
    "RENTOWE_ZLECENIOBIORCA": "rentowe_zleceniobiorca",
    "RENTOWE_ZLECENIODAWCA": "rentowe_zleceniodawca",
    "CHOROBOWE_ZLECENIOBIORCA": "chorobowe_zleceniobiorca",
    "WYPADKOWE_ZLECENIODAWCA": "wypadkowe_zleceniodawca",
    "ZDROWOTNE_ZLECENIOBIORCA": "zdrowotne_zleceniobiorca",
    "FP": "fp_kwota",
    "FGSP": "fgsp_kwota",
    "KOSZTY_UZYSKANIA__KWOTA_": "kup_kwota",
    "DOCHOD": "dochod",
    "KWOTA_PODATKU": "kwota_podatku",
    "KWOTA_DO_WYPLATY": "kwota_do_wyplaty",
}


# Standardowe stawki ZUS 2026 do weryfikacji poprawności stawek w BD
STANDARD_RATES_2026: dict[str, float] = {
    "EMERYTALNE____": 19.52,
    "RENTOWE_U____": 1.50,
    "RENTOWE____": 6.50,
    "CHOROBOWE____": 2.45,
    "ZDROWOTNE____": 9.00,
}

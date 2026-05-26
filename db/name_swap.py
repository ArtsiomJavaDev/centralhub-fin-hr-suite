"""Detect and fix swapped first name / surname (IMIE_1 <-> NAZWISKO)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Najczęstsze polskie imiona (m/k) — do heurystyki gdy brak PESEL/CRM.
_COMMON_FIRST_NAMES: frozenset[str] = frozenset(
    n.lower()
    for n in (
        "Adam",
        "Adrian",
        "Agnieszka",
        "Aleksander",
        "Aleksandra",
        "Alicja",
        "Amelia",
        "Anastazja",
        "Andrzej",
        "Angelika",
        "Anna",
        "Antoni",
        "Antonina",
        "Arkadiusz",
        "Artur",
        "Barbara",
        "Bartosz",
        "Beata",
        "Bogdan",
        "Bogusław",
        "Bogumiła",
        "Borys",
        "Cezary",
        "Cyprian",
        "Czesław",
        "Dagmara",
        "Damian",
        "Daniel",
        "Dariusz",
        "Dawid",
        "Dominik",
        "Dominika",
        "Dorota",
        "Edyta",
        "Ela",
        "Eliza",
        "Elżbieta",
        "Emilia",
        "Ewa",
        "Fabian",
        "Filip",
        "Gabriel",
        "Gabriela",
        "Grzegorz",
        "Gustaw",
        "Halina",
        "Hanna",
        "Henryk",
        "Hubert",
        "Igor",
        "Irena",
        "Iwona",
        "Izabela",
        "Jacek",
        "Jagoda",
        "Jakub",
        "Jan",
        "Janina",
        "Jarosław",
        "Jerzy",
        "Joanna",
        "Jolanta",
        "Józef",
        "Julia",
        "Julian",
        "Julita",
        "Justyna",
        "Kacper",
        "Kamil",
        "Kamila",
        "Karina",
        "Karol",
        "Karolina",
        "Katarzyna",
        "Kinga",
        "Klaudia",
        "Konrad",
        "Krystian",
        "Krystyna",
        "Krzysztof",
        "Laura",
        "Leon",
        "Leszek",
        "Lidia",
        "Liliana",
        "Lucyna",
        "Ludwik",
        "Łukasz",
        "Maciej",
        "Magdalena",
        "Maja",
        "Marcin",
        "Marek",
        "Maria",
        "Marian",
        "Mariusz",
        "Marta",
        "Martyna",
        "Marzena",
        "Mateusz",
        "Małgorzata",
        "Michał",
        "Mikołaj",
        "Mirosław",
        "Monika",
        "Natalia",
        "Nina",
        "Norbert",
        "Oksana",
        "Ola",
        "Olaf",
        "Oleg",
        "Oliwia",
        "Oskar",
        "Patrycja",
        "Patryk",
        "Paulina",
        "Paweł",
        "Piotr",
        "Przemysław",
        "Radosław",
        "Rafał",
        "Renata",
        "Robert",
        "Roksana",
        "Roman",
        "Ryszard",
        "Sabina",
        "Sandra",
        "Sebastian",
        "Sergiusz",
        "Sławomir",
        "Stanisław",
        "Stefan",
        "Sylwia",
        "Szymon",
        "Tadeusz",
        "Tatiana",
        "Tomasz",
        "Urszula",
        "Waldemar",
        "Weronika",
        "Wiktor",
        "Wiktoria",
        "Wioletta",
        "Witold",
        "Władysław",
        "Włodzimierz",
        "Zbigniew",
        "Zdzisław",
        "Zofia",
        "Zuzanna",
        "Żaneta",
    )
)

_SURNAME_SUFFIX = re.compile(
    r"(?:"
    r"ski|ska|cki|cka|wicz|witz|ak|ek|uk|cz|ów|owa|ewicz|iewicz|enko|"
    r"ova|eva|ov|ev|ko|yuk|yshch|ets|ian|ich|ych|aya|skiy|skaya"
    r")$",
    re.IGNORECASE,
)

# Imiona częste w tej bazie (UA/BY/PL), uzupełniane z korpusu przy detekcji.
_EXTRA_FIRST_NAMES: frozenset[str] = frozenset(
    n.lower()
    for n in (
        "Olga",
        "Anastasiya",
        "Anastasia",
        "Milana",
        "Maryna",
        "Marina",
        "Daria",
        "Darya",
        "Alina",
        "Yana",
        "Iuliia",
        "Yuliia",
        "Nataliia",
        "Natalia",
        "Tetiana",
        "Tatsiana",
        "Volha",
        "Dzmitry",
        "Dmitry",
        "Siarhei",
        "Aliaksandr",
        "Aleksandr",
        "Pavel",
        "Stanislav",
        "Viachaslau",
        "Valeryia",
        "Valeria",
        "Veranika",
        "Veronika",
        "Roman",
        "Oleg",
        "Oleksii",
        "Dmytro",
        "Igor",
        "Viktoriya",
        "Viktoria",
        "Liudmila",
        "LiudmiIa",
        "Katsiaryna",
        "Ekaterina",
        "Sergei",
        "Natallia",
        "Alena",
        "Ulyana",
        "Leila",
        "Tsimafei",
        "Daniil",
        "Anton",
        "Piotr",
        "Stela",
    )
)

_SKIP_VALUES = frozenset({"", "brak", "brak danych", "n/d", "nd", "-", "?"})
_MIN_TOKEN_LEN = 2


def normalize_name(value: object) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() in _SKIP_VALUES:
        return ""
    return " ".join(s.split())


def _tokens(value: str) -> list[str]:
    return [t for t in normalize_name(value).split() if len(t) >= _MIN_TOKEN_LEN]


@dataclass(frozen=True)
class EmployeeNameRow:
    id_pracownika: int
    pesel: str
    imie: str
    nazwisko: str


@dataclass(frozen=True)
class SwapCandidate:
    id_pracownika: int
    pesel: str
    imie_before: str
    nazwisko_before: str
    imie_after: str
    nazwisko_after: str
    confidence: float
    reasons: tuple[str, ...]


class LearnedNameCorpus:
    """Imiona/nazwiska wyuczone z kolumn IMIE_1 i NAZWISKO w payroll system."""

    def __init__(
        self,
        rows: Iterable[EmployeeNameRow],
        *,
        min_count: int = 3,
        min_ratio: float = 0.65,
    ) -> None:
        imie_counts: Counter[str] = Counter()
        nazwisko_counts: Counter[str] = Counter()
        for row in rows:
            for token in _tokens(row.imie):
                imie_counts[token.lower()] += 1
            for token in _tokens(row.nazwisko):
                nazwisko_counts[token.lower()] += 1

        self._imie = imie_counts
        self._nazwisko = nazwisko_counts
        self.first_names: set[str] = set(_COMMON_FIRST_NAMES) | set(_EXTRA_FIRST_NAMES)
        self.surnames: set[str] = set()

        all_tokens = set(imie_counts) | set(nazwisko_counts)
        for token in all_tokens:
            in_imie = imie_counts.get(token, 0)
            in_nazw = nazwisko_counts.get(token, 0)
            total = in_imie + in_nazw
            if total < min_count:
                continue
            if in_imie / total >= min_ratio:
                self.first_names.add(token)
            if in_nazw / total >= min_ratio:
                self.surnames.add(token)

    def is_first_name(self, token: str) -> bool:
        return token.lower() in self.first_names

    def is_surname(self, token: str) -> bool:
        low = token.lower()
        if low in self.surnames:
            return True
        return bool(_SURNAME_SUFFIX.search(low))

    def prefers_imie_column(self, token: str) -> bool:
        low = token.lower()
        in_imie = self._imie.get(low, 0)
        in_nazw = self._nazwisko.get(low, 0)
        total = in_imie + in_nazw
        if total == 0:
            return False
        return in_imie / total >= 0.65


class ColumnFrequencyModel:
    """Learn which tokens usually appear in IMIE_1 vs NAZWISKO."""

    def __init__(self, rows: Iterable[EmployeeNameRow]) -> None:
        corpus = LearnedNameCorpus(rows)
        self._imie = corpus._imie
        self._nazwisko = corpus._nazwisko

    def token_role_score(self, token: str, role: str) -> float:
        key = token.lower()
        in_imie = self._imie.get(key, 0)
        in_nazw = self._nazwisko.get(key, 0)
        total = in_imie + in_nazw
        if total == 0:
            return 0.5
        if role == "imie":
            return in_imie / total
        return in_nazw / total

    def layout_score(self, imie: str, nazwisko: str) -> float:
        imie_tokens = _tokens(imie)
        nazw_tokens = _tokens(nazwisko)
        if not imie_tokens and not nazw_tokens:
            return 0.5
        scores: list[float] = []
        for t in imie_tokens:
            scores.append(self.token_role_score(t, "imie"))
        for t in nazw_tokens:
            scores.append(self.token_role_score(t, "nazwisko"))
        return sum(scores) / len(scores)


def _polish_heuristic_score(imie: str, nazwisko: str) -> tuple[float, float]:
    """Return (score_correct, score_swapped) in 0..1 from name-shape rules."""
    imie_tokens = _tokens(imie)
    nazw_tokens = _tokens(nazwisko)
    if not imie_tokens and not nazw_tokens:
        return 0.5, 0.5

    def token_imie_likelihood(token: str) -> float:
        low = token.lower()
        score = 0.5
        if low in _COMMON_FIRST_NAMES:
            score += 0.35
        if _SURNAME_SUFFIX.search(low):
            score -= 0.4
        return max(0.0, min(1.0, score))

    def token_nazwisko_likelihood(token: str) -> float:
        return 1.0 - token_imie_likelihood(token) * 0.85 + 0.075

    correct_parts: list[float] = []
    swapped_parts: list[float] = []
    for t in imie_tokens:
        correct_parts.append(token_imie_likelihood(t))
        swapped_parts.append(token_nazwisko_likelihood(t))
    for t in nazw_tokens:
        correct_parts.append(token_nazwisko_likelihood(t))
        swapped_parts.append(token_imie_likelihood(t))
    return (
        sum(correct_parts) / len(correct_parts),
        sum(swapped_parts) / len(swapped_parts),
    )


def _crm_layout_match(
    imie: str,
    nazwisko: str,
    crm_name: str,
    crm_surname: str,
) -> Optional[str]:
    """'ok' | 'wapro_swapped' | 'crm_swapped' | None if inconclusive."""
    wi, wn = normalize_name(imie).lower(), normalize_name(nazwisko).lower()
    cn, cs = normalize_name(crm_name).lower(), normalize_name(crm_surname).lower()
    if not wi and not wn:
        return None
    if not cn and not cs:
        return None

    direct = (wi == cn or not cn) and (wn == cs or not cs) and (wi == cn or wn == cs)
    cross_wapro = (wi == cs or not cs) and (wn == cn or not cn) and (wi == cs or wn == cn)

    if direct and not cross_wapro:
        return "ok"
    if cross_wapro and not direct:
        return "wapro_swapped"
    if direct and cross_wapro:
        return "ok"
    return None


def _primary_token(value: str) -> str:
    parts = _tokens(value)
    return parts[0] if parts else ""


def _wapro_swap_score(
    imie: str,
    nazwisko: str,
    corpus: LearnedNameCorpus,
    freq: ColumnFrequencyModel,
) -> tuple[float, list[str]]:
    """Higher score => more likely IMIE_1 and NAZWISKO are swapped."""
    reasons: list[str] = []
    imie_t = _primary_token(imie)
    nazw_t = _primary_token(nazwisko)
    if not imie_t or not nazw_t:
        return 0.0, reasons

    score = 0.0
    nazw_is_first = corpus.is_first_name(nazw_t) or corpus.prefers_imie_column(nazw_t)
    imie_is_first = corpus.is_first_name(imie_t) or corpus.prefers_imie_column(imie_t)
    imie_is_surname = corpus.is_surname(imie_t) and not imie_is_first

    if nazw_is_first and not imie_is_first:
        score += 0.55
        reasons.append("imie_w_nazwisko")
    if imie_is_surname:
        score += 0.35
        reasons.append("nazwisko_w_imie")

    freq_ok = freq.layout_score(imie, nazwisko)
    freq_swap = freq.layout_score(nazwisko, imie)
    if freq_swap - freq_ok >= 0.1:
        score += 0.25
        reasons.append("freq")

    _, heur_swap = _polish_heuristic_score(imie, nazwisko)
    if heur_swap >= 0.6:
        score += 0.15
        reasons.append("heurystyka")

    return min(1.0, score), reasons


def detect_swapped(
    rows: list[EmployeeNameRow],
    crm_by_pesel: Mapping[str, tuple[str, str]] | None = None,
    *,
    min_confidence: float = 0.55,
) -> list[SwapCandidate]:
    if not rows:
        return []

    corpus = LearnedNameCorpus(rows)
    freq = ColumnFrequencyModel(rows)
    candidates: list[SwapCandidate] = []

    for row in rows:
        imie = normalize_name(row.imie)
        nazwisko = normalize_name(row.nazwisko)
        if not imie and not nazwisko:
            continue
        if imie.lower() == nazwisko.lower() and imie:
            continue

        swap_score, reasons = _wapro_swap_score(imie, nazwisko, corpus, freq)

        pesel_key = normalize_name(row.pesel)
        if crm_by_pesel and pesel_key and len(pesel_key) == 11 and pesel_key.isdigit():
            crm_pair = crm_by_pesel.get(pesel_key)
            if crm_pair:
                crm_name, crm_surname = crm_pair
                crm_match = _crm_layout_match(imie, nazwisko, crm_name, crm_surname)
                if crm_match == "wapro_swapped":
                    reasons.append("crm")
                    swap_score = max(swap_score, 0.8)
                elif crm_match == "ok":
                    swap_score = 0.0
                    reasons = []

        if swap_score < min_confidence:
            continue
        if "imie_w_nazwisko" not in reasons:
            continue

        candidates.append(
            SwapCandidate(
                id_pracownika=row.id_pracownika,
                pesel=pesel_key,
                imie_before=imie,
                nazwisko_before=nazwisko,
                imie_after=nazwisko,
                nazwisko_after=imie,
                confidence=round(swap_score, 3),
                reasons=tuple(dict.fromkeys(reasons)),
            )
        )

    candidates.sort(key=lambda c: (-c.confidence, c.nazwisko_before, c.imie_before))
    return candidates


def fetch_wapro_employees(engine: Engine) -> list[EmployeeNameRow]:
    query = text(
        """
        SELECT
            ID_PRACOWNIKA,
            LTRIM(RTRIM(ISNULL(PESEL, ''))) AS pesel,
            LTRIM(RTRIM(ISNULL(IMIE_1, ''))) AS imie,
            LTRIM(RTRIM(ISNULL(NAZWISKO, ''))) AS nazwisko
        FROM PRACOWNIK
        WHERE (IMIE_1 IS NOT NULL AND LTRIM(IMIE_1) <> '')
           OR (NAZWISKO IS NOT NULL AND LTRIM(NAZWISKO) <> '')
        ORDER BY ID_PRACOWNIKA
        """
    )
    with engine.connect() as conn:
        result = conn.execute(query)
        return [
            EmployeeNameRow(
                id_pracownika=int(r[0]),
                pesel=str(r[1] or ""),
                imie=str(r[2] or ""),
                nazwisko=str(r[3] or ""),
            )
            for r in result
        ]


def apply_wapro_swaps(
    engine: Engine,
    candidates: Iterable[SwapCandidate],
    *,
    dry_run: bool = True,
) -> int:
    update_sql = text(
        """
        UPDATE PRACOWNIK
        SET IMIE_1 = :imie, NAZWISKO = :nazwisko
        WHERE ID_PRACOWNIKA = :id
          AND LTRIM(RTRIM(ISNULL(IMIE_1, ''))) = :imie_before
          AND LTRIM(RTRIM(ISNULL(NAZWISKO, ''))) = :nazwisko_before
        """
    )
    count = 0
    if dry_run:
        return sum(1 for _ in candidates)

    with engine.begin() as conn:
        for c in candidates:
            result = conn.execute(
                update_sql,
                {
                    "id": c.id_pracownika,
                    "imie": c.imie_after[:30],
                    "nazwisko": c.nazwisko_after[:40],
                    "imie_before": c.imie_before,
                    "nazwisko_before": c.nazwisko_before,
                },
            )
            count += int(result.rowcount or 0)
    return count

-- Przykładowy SQL do wklejenia w config.ini → [crm] → report_sql (jedna linia)
-- Kolumny muszą odpowiadać UDUZ04 (aliasy): lp, numer_umowy, numer_rachunku, typ,
-- pracownik, pesel, kwota_netto, kwota_brutto, kup, data_zawarcia, podatek, data_wyplaty
--
-- Dostosuj nazwy tabel do schematu CRM (po: python tools/crm_configure.py --list-tables)

SELECT
    NULL AS lp,
    u.numer_umowy,
    u.numer_rachunku,
    u.typ_umowy AS typ,
    CONCAT(p.imie, ' ', p.nazwisko) AS pracownik,
    p.pesel,
    u.kwota_netto,
    u.kwota_brutto,
    u.kup_proc AS kup,
    u.data_zawarcia,
    u.podatek,
    u.data_wyplaty,
    NULL AS ppk
FROM umowy u
JOIN pracownicy p ON p.id = u.pracownik_id
WHERE YEAR(u.data_wyplaty) = %(year)s
  AND MONTH(u.data_wyplaty) = %(month)s
  AND u.typ_umowy IN ('Umowa Zlecenie', 'Umowa o Dzieło', 'PPK');

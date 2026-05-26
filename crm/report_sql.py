"""Default CRM report SQL — bills → UDUZ04 layout.

Filter: YEAR/MONTH of bills.account_till (data wypłaty).
Use %% for literal % in SQL (pymysql %(year)s params).
"""

DEFAULT_REPORT_SQL = """
SELECT
    NULL AS lp,
    COALESCE(cz.number, cd.number) AS numer_umowy,
    b.bill_number AS numer_rachunku,
    CASE
        WHEN b.contract_type LIKE '%%Zlicen%%' THEN 'Umowa Zlecenie'
        WHEN b.contract_type LIKE '%%Dzielo%%' THEN 'Umowa o Dzieło'
    END AS typ,
    TRIM(CONCAT(COALESCE(e.name, ''), ' ', COALESCE(e.surname, ''))) AS pracownik,
    e.pesel_number AS pesel,
    b.netto_amount AS kwota_netto,
    b.brutto_amount AS kwota_brutto,
    CONCAT(b.kup, '%%') AS kup,
    COALESCE(cz.start_date, cd.start_date) AS data_zawarcia,
    ROUND(b.brutto_amount - b.netto_amount, 2) AS podatek,
    b.account_till AS data_wyplaty,
    NULL AS ppk
FROM bills b
LEFT JOIN contracts_zlicen cz
    ON b.contract_id = cz.id AND b.contract_type LIKE '%%Zlicen%%'
LEFT JOIN contracts_dzielo cd
    ON b.contract_id = cd.id AND b.contract_type LIKE '%%Dzielo%%'
LEFT JOIN employees e
    ON e.id = COALESCE(cz.employee_id, cd.employee_id)
WHERE YEAR(b.account_till) = %(year)s
  AND MONTH(b.account_till) = %(month)s
  AND b.brutto_amount > 0
ORDER BY typ, b.bill_number
""".strip()

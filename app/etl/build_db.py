"""CSV -> DuckDB: сырые таблицы + профили поставщиков.

Запуск: python -m app.etl.build_db
"""
import time

import duckdb

from app.config import DATA_DIR, DB_PATH, RAW_NOTICES, RAW_SUPPLIERS, RAW_TRU

CSV_OPTS = "delim=';', quote='\"', escape='\"', header=true, all_varchar=true"


def _csv(path) -> str:
    return f"read_csv('{path.as_posix()}', {CSV_OPTS})"


def build() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    t0 = time.time()

    def step(name: str, sql: str) -> None:
        con.execute(sql)
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"[{time.time() - t0:6.1f}s] {name}: {n:,}")

    # --- сырые таблицы -----------------------------------------------------
    step("lots", f"""
        CREATE TABLE lots AS
        SELECT CAST(lot_id AS BIGINT)                AS lot_id,
               CAST(procedure_id AS BIGINT)          AS procedure_id,
               CAST(publish_date AS DATE)            AS publish_date,
               TRY_CAST(start_price AS DOUBLE)       AS start_price,
               coalesce(nullif(trim(subject), ''), procedure_name) AS subject,
               CASE WHEN is_eshop_or_aisgz = 'ЭМ' THEN 'ЭМ' ELSE 'АИС ГЗ' END AS platform,
               is_smp = 'true'                       AS is_smp,
               customer_inn,
               customer_kpp
        FROM {_csv(RAW_NOTICES)}
    """)

    step("lot_items", f"""
        CREATE TABLE lot_items AS
        SELECT CAST(lot_id AS BIGINT) AS lot_id,
               trim(product_name)     AS product_name,
               okpd2_code
        FROM {_csv(RAW_TRU)}
    """)

    step("participations", f"""
        CREATE TABLE participations AS
        SELECT CAST(lot_id AS BIGINT)  AS lot_id,
               supplier_inn            AS inn,
               nullif(supplier_kpp,'') AS kpp,
               is_winner = 'true'      AS is_winner
        FROM {_csv(RAW_SUPPLIERS)}
        WHERE length(supplier_inn) IN (10, 12)
    """)

    # --- документы для поиска: предмет лота + уникальные позиции ТРУ ---------
    step("lot_docs", """
        CREATE TABLE lot_docs AS
        SELECT l.lot_id,
               l.subject,
               coalesce(string_agg(DISTINCT i.product_name, ' | '), '') AS items,
               list(DISTINCT i.okpd2_code) FILTER (WHERE i.okpd2_code IS NOT NULL) AS okpd2_codes
        FROM lots l
        LEFT JOIN lot_items i USING (lot_id)
        GROUP BY l.lot_id, l.subject
    """)

    # --- профиль поставщика -------------------------------------------------
    step("supplier_profile", """
        CREATE TABLE supplier_profile AS
        WITH p AS (
            SELECT p.*, l.publish_date, l.start_price, l.platform, l.customer_inn
            FROM participations p JOIN lots l USING (lot_id)
        )
        SELECT inn,
               mode(kpp)                                            AS kpp,
               CASE WHEN length(inn) = 12 THEN 'ИП' ELSE 'ЮЛ' END   AS entity_type,
               substr(coalesce(mode(kpp), inn), 1, 2)               AS region_code,
               count(DISTINCT lot_id)                               AS n_lots,
               count(DISTINCT lot_id) FILTER (WHERE is_winner)      AS n_wins,
               round(count(DISTINCT lot_id) FILTER (WHERE is_winner)
                     / count(DISTINCT lot_id), 3)                   AS win_rate,
               count(DISTINCT customer_inn)                         AS n_customers,
               count(DISTINCT lot_id) FILTER (WHERE platform = 'ЭМ')     AS n_eshop,
               count(DISTINCT lot_id) FILTER (WHERE platform = 'АИС ГЗ') AS n_aisgz,
               min(publish_date)                                    AS first_date,
               max(publish_date)                                    AS last_date,
               sum(start_price) FILTER (WHERE is_winner)            AS sum_won,
               median(start_price)                                  AS median_price
        FROM p
        GROUP BY inn
    """)

    # --- опыт поставщика по кодам ОКПД2 -------------------------------------
    step("supplier_okpd", """
        CREATE TABLE supplier_okpd AS
        WITH li AS (SELECT DISTINCT lot_id, okpd2_code FROM lot_items WHERE okpd2_code IS NOT NULL)
        SELECT p.inn,
               li.okpd2_code,
               count(DISTINCT p.lot_id)                        AS n_lots,
               count(DISTINCT p.lot_id) FILTER (WHERE p.is_winner) AS n_wins,
               max(l.publish_date)                             AS last_date
        FROM participations p
        JOIN li USING (lot_id)
        JOIN lots l USING (lot_id)
        GROUP BY p.inn, li.okpd2_code
    """)

    # --- справочник ОКПД2: самое частое название позиции на код ---------------
    step("okpd2_names", """
        CREATE TABLE okpd2_names AS
        SELECT okpd2_code, arg_max(product_name, cnt) AS sample_name, sum(cnt) AS n_items
        FROM (SELECT okpd2_code, product_name, count(*) AS cnt
              FROM lot_items WHERE okpd2_code IS NOT NULL GROUP BY ALL)
        GROUP BY okpd2_code
    """)

    con.execute("CREATE INDEX idx_part_lot ON participations(lot_id)")
    con.execute("CREATE INDEX idx_part_inn ON participations(inn)")
    con.execute("CREATE INDEX idx_sokpd_inn ON supplier_okpd(inn)")
    con.close()
    print(f"done in {time.time() - t0:.1f}s -> {DB_PATH}")


if __name__ == "__main__":
    build()

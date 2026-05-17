#!/usr/bin/env python3
"""
Pre-aggregate AIS data for browser consumption.

This script reads a large AIS Parquet file, samples 20M rows into test.parquet,
then builds pre-aggregated files that the browser can load quickly:

  - test.parquet                    (~1GB):  20M sampled rows, all 18 columns
  - ais_position_points.parquet     (~300MB): All valid lat/lon positions
  - ais_latest_positions.parquet    (~5-15MB): One row per MMSI with latest position

Usage:
    python aggregate.py [source.parquet] [output_dir]

Defaults:
    source.parquet = ~/code/data/mc/mcdec/parquet/ais_2024.parquet
    output_dir     = . (current directory)

Requires: duckdb (pip install duckdb)
"""

import duckdb
import sys
from pathlib import Path

SAMPLE_ROWS = 20_000_000
MERCATOR_MAX_LATITUDE = 85.05112878


def main():
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "code/data/mc/mcdec/parquet/ais_2024.parquet"
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")

    if not source_path.exists():
        print(f"Error: {source_path} not found")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()

    # ── 0. Sample 20M rows → test.parquet ──────────────────────────────────
    test_output = output_dir / "test.parquet"
    print(f"[1/3] Sampling {SAMPLE_ROWS:,} rows from {source_path}...")
    con.execute(f"""
        CREATE OR REPLACE TABLE sampled_data AS
        SELECT * FROM read_parquet('{source_path}')
        USING SAMPLE {SAMPLE_ROWS} ROWS
    """)
    sample_count = con.execute("SELECT count(*) FROM sampled_data").fetchone()[0]
    print(f"      {sample_count:,} rows sampled")

    con.execute(f"COPY sampled_data TO '{test_output}' (FORMAT PARQUET);")
    test_size = test_output.stat().st_size / (1024 * 1024)
    print(f"      Written to {test_output} ({test_size:.1f} MB)")

    # ── 1. ais_position_points ──────────────────────────────────────────────
    print(f"[2/3] Creating ais_position_points from test.parquet...")
    con.execute(f"""
        CREATE OR REPLACE TABLE ais_position_points AS
        SELECT
            CAST(LAT AS DOUBLE) AS latitude,
            CAST(LON AS DOUBLE) AS longitude
        FROM '{test_output}'
        WHERE LAT BETWEEN {-MERCATOR_MAX_LATITUDE} AND {MERCATOR_MAX_LATITUDE}
          AND LON BETWEEN -180 AND 180
    """)

    pos_count = con.execute("SELECT count(*) FROM ais_position_points").fetchone()[0]
    print(f"      {pos_count:,} position points")

    pos_output = output_dir / "ais_position_points.parquet"
    con.execute(f"COPY ais_position_points TO '{pos_output}' (FORMAT PARQUET);")
    pos_size = pos_output.stat().st_size / (1024 * 1024)
    print(f"      Written to {pos_output} ({pos_size:.1f} MB)")

    # ── 2. ais_latest_positions ─────────────────────────────────────────────
    print(f"[3/3] Creating ais_latest_positions from test.parquet...")
    con.execute(f"""
        CREATE OR REPLACE TABLE ais_latest_positions AS
        WITH valid AS (
            SELECT
                CAST(MMSI AS VARCHAR) AS mmsi,
                CAST(LAT AS DOUBLE) AS latitude,
                CAST(LON AS DOUBLE) AS longitude,
                CAST(extract(epoch from BaseDateTime) AS BIGINT) AS position_time,
                COALESCE(CAST(extract(epoch from BaseDateTime) AS BIGINT), 0) AS position_rank,
                CASE WHEN trim(VesselName) <> '' THEN trim(VesselName) ELSE NULL END AS name,
                CASE WHEN trim(VesselType) <> '' THEN trim(VesselType) ELSE NULL END AS ship_type,
                CASE WHEN trim(Status) <> '' THEN trim(Status) ELSE NULL END AS navigation_status,
                TRY_CAST(SOG AS DOUBLE) AS speed_over_ground,
                TRY_CAST(COG AS DOUBLE) AS raw_course_over_ground,
                TRY_CAST(Heading AS DOUBLE) AS heading
            FROM '{test_output}'
            WHERE MMSI IS NOT NULL
              AND LAT BETWEEN -90 AND 90
              AND LON BETWEEN -180 AND 180
        )
        SELECT
            mmsi,
            arg_max(latitude, position_rank) AS latitude,
            arg_max(longitude, position_rank) AS longitude,
            max(position_time) AS position_time,
            arg_max(name, position_rank) AS name,
            arg_max(ship_type, position_rank) AS ship_type,
            arg_max(navigation_status, position_rank) AS navigation_status,
            arg_max(speed_over_ground, position_rank) AS speed_over_ground,
            CASE
                WHEN arg_max(raw_course_over_ground, position_rank) BETWEEN 0 AND 360
                THEN arg_max(raw_course_over_ground, position_rank)
                ELSE NULL
            END AS course_over_ground,
            arg_max(heading, position_rank) AS heading
        FROM valid
        GROUP BY mmsi
    """)

    ship_count = con.execute("SELECT count(*) FROM ais_latest_positions").fetchone()[0]
    print(f"      {ship_count:,} unique vessels")

    ship_output = output_dir / "ais_latest_positions.parquet"
    con.execute(f"COPY ais_latest_positions TO '{ship_output}' (FORMAT PARQUET);")
    ship_size = ship_output.stat().st_size / (1024 * 1024)
    print(f"      Written to {ship_output} ({ship_size:.1f} MB)")

    # ── Summary ─────────────────────────────────────────────────────────────
    total_size = test_size + pos_size + ship_size
    source_size = source_path.stat().st_size / (1024 * 1024)
    print(f"\nDone!")
    print(f"  Source:      {source_path} ({source_size:.0f} MB)")
    print(f"  Output:      {total_size:.1f} MB total")
    print(f"  Reduction:   {source_size / total_size:.0f}x smaller")
    print(f"\nServe these files alongside index.html:")
    print(f"  {test_output}")
    print(f"  {pos_output}")
    print(f"  {ship_output}")


if __name__ == "__main__":
    main()

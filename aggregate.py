#!/usr/bin/env python3
"""
Pre-aggregate AIS data for browser consumption.

This script reads a large AIS Parquet file, selects a temporal contiguous sample
just under 20M rows into test.parquet, then builds pre-aggregated files that the
browser can load quickly:

  - test.parquet                    (~1GB):  temporal sample, all 18 columns
  - ais_position_points.parquet     (~300MB): All valid lat/lon positions
  - ais_latest_positions.parquet    (~5-15MB): One row per MMSI with latest position
  - sample_manifest.json            metadata for the selected temporal window

Usage:
    python aggregate.py [source.parquet] [output_dir]
    python aggregate.py [source.parquet] [output_dir] --target-rows 20000000 --seed 1234

Defaults:
    source.parquet = ~/code/data/mc/mcdec/parquet/ais_2024.parquet
    output_dir     = . (current directory)

Requires: duckdb (pip install duckdb)
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TARGET_ROWS = 20_000_000
MERCATOR_MAX_LATITUDE = 85.05112878
DEFAULT_SOURCE_PATH = Path.home() / "code/data/mc/mcdec/parquet/ais_2024.parquet"


def parse_args():
    parser = argparse.ArgumentParser(description="Create browser-sized AIS Parquet extracts and aggregates.")
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE_PATH, help="Source AIS Parquet file")
    parser.add_argument("output_dir", nargs="?", type=Path, default=Path("."), help="Output directory")
    parser.add_argument(
        "--target-rows",
        type=int,
        default=DEFAULT_TARGET_ROWS,
        help="Upper row budget. The selected temporal window will stay below this value.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible anchor-day selection")
    return parser.parse_args()


def sql_path(path):
    return str(path).replace("'", "''")


def iso_datetime(value):
    if value is None:
        return None
    return value.isoformat()


def sql_utc_timestamp(value):
    return f"{value.isoformat()}+00:00"


def normalize_hour(value):
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def load_hourly_counts(con, source_path):
    rows = con.execute(f"""
        SELECT
            time_bucket(INTERVAL '1 hour', BaseDateTime) AS hour_start,
            count(*) AS report_count
        FROM read_parquet('{sql_path(source_path)}')
        WHERE BaseDateTime IS NOT NULL
        GROUP BY hour_start
        ORDER BY hour_start
    """).fetchall()

    hourly_counts = {normalize_hour(row[0]): int(row[1]) for row in rows}
    daily_counts = {}
    for hour, count in hourly_counts.items():
        day = hour.date()
        daily_counts[day] = daily_counts.get(day, 0) + count

    return hourly_counts, daily_counts


def select_anchor_day(daily_counts, rng):
    available_days = sorted(daily_counts)
    if not available_days:
        raise ValueError("No BaseDateTime values found in source data.")
    return rng.choice(available_days)


def expand_by_days(anchor_day, daily_counts, max_rows):
    selected_days = {anchor_day}
    selected_count = daily_counts[anchor_day]
    start_day = anchor_day
    end_day = anchor_day
    min_day = min(daily_counts)
    max_day = max(daily_counts)
    skipped_days = []
    direction = -1

    while True:
        prev_day = start_day - timedelta(days=1)
        next_day = end_day + timedelta(days=1)
        prev_available = prev_day >= min_day and prev_day in daily_counts
        next_available = next_day <= max_day and next_day in daily_counts
        prev_fits = prev_available and selected_count + daily_counts[prev_day] <= max_rows
        next_fits = next_available and selected_count + daily_counts[next_day] <= max_rows

        if not prev_fits and not next_fits:
            for day, available in ((prev_day, prev_available), (next_day, next_available)):
                if available:
                    skipped_days.append({"day": day.isoformat(), "report_count": daily_counts[day]})
            break

        candidates = (prev_day, next_day) if direction < 0 else (next_day, prev_day)
        for candidate in candidates:
            if candidate == prev_day and prev_fits:
                selected_days.add(candidate)
                selected_count += daily_counts[candidate]
                start_day = candidate
                break
            if candidate == next_day and next_fits:
                selected_days.add(candidate)
                selected_count += daily_counts[candidate]
                end_day = candidate
                break

        direction *= -1

    return selected_days, selected_count, start_day, end_day, skipped_days


def expand_by_hours(start_hour, end_hour, hourly_counts, selected_count, max_rows):
    min_hour = min(hourly_counts)
    max_hour = max(hourly_counts)
    selected_hours = set()
    skipped_hours = []
    direction = -1

    while True:
        prev_hour = start_hour - timedelta(hours=1)
        next_hour = end_hour + timedelta(hours=1)
        prev_available = prev_hour >= min_hour and prev_hour in hourly_counts
        next_available = next_hour <= max_hour and next_hour in hourly_counts
        prev_fits = prev_available and selected_count + hourly_counts[prev_hour] <= max_rows
        next_fits = next_available and selected_count + hourly_counts[next_hour] <= max_rows

        if not prev_fits and not next_fits:
            for hour, available in ((prev_hour, prev_available), (next_hour, next_available)):
                if available:
                    skipped_hours.append({"hour_start": hour.isoformat(), "report_count": hourly_counts[hour]})
            break

        candidates = (prev_hour, next_hour) if direction < 0 else (next_hour, prev_hour)
        for hour in candidates:
            if hour == prev_hour and prev_fits:
                selected_hours.add(hour)
                selected_count += hourly_counts[hour]
                start_hour = hour
                break
            if hour == next_hour and next_fits:
                selected_hours.add(hour)
                selected_count += hourly_counts[hour]
                end_hour = hour
                break

        direction *= -1

    return selected_hours, selected_count, start_hour, end_hour, skipped_hours


def choose_temporal_window(hourly_counts, daily_counts, target_rows, seed):
    max_rows = target_rows - 1
    if max_rows < 1:
        raise ValueError("--target-rows must be greater than 1")

    rng = random.Random(seed)
    anchor_day = select_anchor_day(daily_counts, rng)
    anchor_day_count = daily_counts[anchor_day]

    if anchor_day_count <= max_rows:
        selected_days, selected_count, start_day, end_day, skipped_days = expand_by_days(anchor_day, daily_counts, max_rows)
        start_hour = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc).replace(tzinfo=None)
        end_hour = datetime.combine(end_day, datetime.min.time(), tzinfo=timezone.utc).replace(tzinfo=None) + timedelta(hours=23)
        selected_hours, selected_count, start_hour, end_hour, skipped_hours = expand_by_hours(start_hour, end_hour, hourly_counts, selected_count, max_rows)
    else:
        day_hours = sorted(hour for hour in hourly_counts if hour.date() == anchor_day and hourly_counts[hour] <= max_rows)
        if not day_hours:
            raise ValueError(f"Every hour on anchor day {anchor_day} exceeds target row budget.")
        anchor_hour = rng.choice(day_hours)
        selected_days = set()
        skipped_days = [{"day": anchor_day.isoformat(), "report_count": anchor_day_count}]
        selected_count = hourly_counts[anchor_hour]
        start_hour = anchor_hour
        end_hour = anchor_hour
        selected_hours, selected_count, start_hour, end_hour, skipped_hours = expand_by_hours(start_hour, end_hour, hourly_counts, selected_count, max_rows)

    return {
        "anchor_day": anchor_day,
        "selected_days": selected_days,
        "selected_hours": selected_hours,
        "selected_count": selected_count,
        "start_time": start_hour,
        "end_time": end_hour + timedelta(hours=1),
        "skipped_days": skipped_days,
        "skipped_hours": skipped_hours,
        "target_rows": target_rows,
        "max_rows": max_rows,
        "seed": seed,
    }


def write_manifest(output_dir, source_path, selection, sample_count, test_size, pos_count, ship_count):
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path),
        "target_rows": selection["target_rows"],
        "max_rows": selection["max_rows"],
        "selected_report_count": selection["selected_count"],
        "actual_test_parquet_count": sample_count,
        "anchor_day": selection["anchor_day"].isoformat(),
        "selected_start_time": iso_datetime(selection["start_time"]),
        "selected_end_time_exclusive": iso_datetime(selection["end_time"]),
        "selected_duration_hours": int((selection["end_time"] - selection["start_time"]).total_seconds() // 3600),
        "selected_full_days": [day.isoformat() for day in sorted(selection["selected_days"])],
        "selected_extra_hours": [hour.isoformat() for hour in sorted(selection["selected_hours"])],
        "skipped_days": selection["skipped_days"],
        "skipped_hours": selection["skipped_hours"],
        "test_parquet_mb": test_size,
        "valid_position_count": pos_count,
        "latest_ship_count": ship_count,
        "seed": selection["seed"],
    }

    manifest_output = output_dir / "sample_manifest.json"
    manifest_output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_output


def main():
    args = parse_args()
    source_path = args.source.expanduser()
    output_dir = args.output_dir.expanduser()
    target_rows = args.target_rows

    if not source_path.exists():
        print(f"Error: {source_path} not found")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    import duckdb

    con = duckdb.connect()

    # -- 0. Select temporal contiguous sample -> test.parquet ---------------
    test_output = output_dir / "test.parquet"
    print(f"[1/4] Counting reports by hour from {source_path}...")
    hourly_counts, daily_counts = load_hourly_counts(con, source_path)
    selection = choose_temporal_window(hourly_counts, daily_counts, target_rows, args.seed)
    print(f"      Anchor day: {selection['anchor_day']}")
    print(f"      Selected window: {selection['start_time']} to {selection['end_time']} (exclusive)")
    print(f"      Selected reports: {selection['selected_count']:,} / max {selection['max_rows']:,}")

    print(f"[2/4] Writing temporal sample to {test_output}...")
    con.execute(f"""
        COPY (
            SELECT *
            FROM read_parquet('{sql_path(source_path)}')
            WHERE BaseDateTime >= TIMESTAMPTZ '{sql_utc_timestamp(selection['start_time'])}'
              AND BaseDateTime < TIMESTAMPTZ '{sql_utc_timestamp(selection['end_time'])}'
            ORDER BY BaseDateTime
        ) TO '{sql_path(test_output)}' (FORMAT PARQUET);
    """)
    sample_count = con.execute(f"SELECT count(*) FROM '{sql_path(test_output)}';").fetchone()[0]
    if sample_count != selection["selected_count"]:
        print(f"      Warning: selected-count estimate was {selection['selected_count']:,}, written file has {sample_count:,} rows")
    test_size = test_output.stat().st_size / (1024 * 1024)
    print(f"      Written {sample_count:,} rows to {test_output} ({test_size:.1f} MB)")

    # -- 1. ais_position_points --------------------------------------------
    print(f"[3/4] Creating ais_position_points from test.parquet...")
    con.execute(f"""
        CREATE OR REPLACE TABLE ais_position_points AS
        SELECT
            CAST(LAT AS DOUBLE) AS latitude,
            CAST(LON AS DOUBLE) AS longitude
        FROM '{sql_path(test_output)}'
        WHERE LAT BETWEEN {-MERCATOR_MAX_LATITUDE} AND {MERCATOR_MAX_LATITUDE}
          AND LON BETWEEN -180 AND 180
    """)

    pos_count = con.execute("SELECT count(*) FROM ais_position_points").fetchone()[0]
    print(f"      {pos_count:,} position points")

    pos_output = output_dir / "ais_position_points.parquet"
    con.execute(f"COPY ais_position_points TO '{sql_path(pos_output)}' (FORMAT PARQUET);")
    pos_size = pos_output.stat().st_size / (1024 * 1024)
    print(f"      Written to {pos_output} ({pos_size:.1f} MB)")

    # -- 2. ais_latest_positions -------------------------------------------
    print(f"[4/4] Creating ais_latest_positions from test.parquet...")
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
            FROM '{sql_path(test_output)}'
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
    con.execute(f"COPY ais_latest_positions TO '{sql_path(ship_output)}' (FORMAT PARQUET);")
    ship_size = ship_output.stat().st_size / (1024 * 1024)
    print(f"      Written to {ship_output} ({ship_size:.1f} MB)")

    manifest_output = write_manifest(output_dir, source_path, selection, sample_count, test_size, pos_count, ship_count)

    # -- Summary -------------------------------------------------------------
    total_size = test_size + pos_size + ship_size
    source_size = source_path.stat().st_size / (1024 * 1024)
    print(f"\nDone!")
    print(f"  Source:      {source_path} ({source_size:.0f} MB)")
    print(f"  Output:      {total_size:.1f} MB total")
    print(f"  Reduction:   {source_size / total_size:.0f}x smaller")
    print(f"  Manifest:    {manifest_output}")
    print(f"\nServe these files alongside index.html:")
    print(f"  {test_output}")
    print(f"  {pos_output}")
    print(f"  {ship_output}")
    print(f"  {manifest_output}")


if __name__ == "__main__":
    main()

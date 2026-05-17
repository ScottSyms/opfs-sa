# In-Browser DuckDB

A high-performance geospatial visualization tool that runs entirely in the browser. Uses the **Origin Private File System (OPFS)** to store pre-aggregated Parquet files locally, then queries them with in-browser DuckDB and renders millions of points using WebAssembly-accelerated density raster tiles.

## What It Does

In-Browser DuckDB fetches pre-aggregated Parquet files once and stores them in the browser's **OPFS** — a performant, persistent, sandboxed file system. All subsequent queries run against this local copy, eliminating network latency and server costs. The visualization uses a hybrid rendering approach:

- **Aggregate density tiles** (zoom levels 0–20): Renders point density as Datashader-style raster tiles computed in Rust/WASM. Fractional zoom levels round up to the next whole zoom, with higher-zoom tiles layered on top for better resolution.
- **Ship circles** (zoom 8+): Shows individual ship positions as GPU-instanced circles with white borders, color-coded by speed.
- **Ship track visualization** (zoom 8+): Click any ship circle to see its 4-hour track with time-based color fading. Click again to hide.

## Key Features

- **Server-side pre-aggregation** — a 24GB raw Parquet file is pre-processed into ~500MB of aggregated data before the browser ever sees it
- **OPFS-backed data** — pre-aggregated Parquet files stored in the browser's Origin Private File System; fetch once, query forever with zero server cost
- **Smart caching** — on each page load, cached files are loaded instantly from OPFS. No expensive in-browser aggregation needed.
- **50M+ points** rendered smoothly using a 512×512 uniform grid spatial index in Rust
- **Web Worker offload** — tile rendering runs off the main thread so the UI stays responsive
- **Double-buffered tile transitions** with crossfade for smooth zoom/pan
- **Tile request debouncing** and coalescing to prevent wasted computation during rapid navigation
- **Parent tile fallback** — cached lower-zoom tiles shown while higher-zoom tiles load
- **Aggregate opacity slider** and zoom level display
- **Ad-hoc SQL queries** via DuckDB-WASM against the local Parquet file (does not affect the map)
- **Kepler.gl** integration for query result visualization
- **Remote HTTP fallback** — if OPFS is unavailable, queries the remote file directly over HTTP with range request support

## Server-Side Pre-Aggregation

For large datasets (e.g., 24GB with 50M+ rows), the browser cannot efficiently download and aggregate the raw data. Instead, pre-aggregate the data server-side using the provided `aggregate.py` script.

### Prerequisites

- Python 3.10+
- DuckDB Python package: `pip install duckdb`

### Running the Aggregation

```bash
# Basic usage (reads test.parquet, outputs to current directory)
python aggregate.py

# Custom source and output directory
python aggregate.py /path/to/large_dataset.parquet /path/to/output/
```

The script produces two files:

| File | Description | Estimated size (50M rows) |
|------|-------------|--------------------------|
| `ais_position_points.parquet` | All valid lat/lon positions within Mercator bounds | ~300-500 MB |
| `ais_latest_positions.parquet` | One row per MMSI with latest position and vessel metadata | ~5-15 MB |

### Schema Requirements

The source Parquet file must have these columns:

| Column | Type | Description |
|--------|------|-------------|
| `MMSI` | VARCHAR/STRING | Vessel identifier |
| `BaseDateTime` | TIMESTAMP | Position timestamp |
| `LAT` | DOUBLE | Latitude |
| `LON` | DOUBLE | Longitude |
| `SOG` | DOUBLE | Speed over ground |
| `COG` | DOUBLE | Course over ground |
| `VesselName` | VARCHAR/STRING | Vessel name (optional) |
| `VesselType` | VARCHAR/STRING | Ship type (optional) |
| `Status` | VARCHAR/STRING | Navigation status (optional) |

### Deployment

Serve these files alongside `index.html`:

```
your-server/
├── index.html
├── density_engine.wasm
├── density_worker.js
├── ais_position_points.parquet    ← pre-aggregated
├── ais_latest_positions.parquet   ← pre-aggregated
└── test.parquet                   ← optional, for track queries
```

The browser will automatically detect and download the pre-aggregated files on first visit, then cache them in OPFS for instant loading on subsequent visits.

### Fallback Behavior

If pre-aggregated files are not available on the server, the app falls back to downloading the raw `test.parquet` file and building the aggregated tables in the browser. This is significantly slower and not recommended for large datasets.

## Architecture

### Rust WASM Density Engine (`src/lib.rs`)

- Loads latitude/longitude columns into WASM memory once
- Builds a uniform grid spatial index during `load_points`
- `render_tile(z, x, y, size)` queries only relevant spatial cells instead of iterating all points
- Outputs RGBA pixel data for 512×512 tiles
- Hardcoded color ramp (purple → pink → red → orange → yellow)

### JavaScript Frontend (`index.html`)

- **MapLibre GL** — base map with Carto Voyager style
- **deck.gl** — overlay layers for density tiles and ship circles
- **Kepler.gl** — optional query result visualization
- **DuckDB-WASM** — SQL queries against OPFS-stored Parquet files
- **Web Worker** (`density_worker.js`) — offloads WASM tile rendering

### Data Flow

1. **Server-side**: `aggregate.py` reads the raw Parquet file and produces `ais_position_points.parquet` and `ais_latest_positions.parquet`
2. **Browser first visit**: Downloads pre-aggregated files and stores them in OPFS
3. **Browser subsequent visits**: Loads pre-aggregated files directly from OPFS (instant)
4. **Single Arrow-to-WASM transfer** — `SELECT latitude, longitude FROM ais_position_points` produces one Arrow table. The two columns are copied into `Float64Array`s, transferred (zero-copy via `Transferable`) to the Web Worker, and loaded into WASM memory via `load_points`. This happens exactly once.
5. WASM builds a 512×512 uniform grid spatial index on that single copy of points
6. All subsequent tile rendering reads exclusively from the in-WASM `PointStore` — no more Arrow tables, no more DuckDB queries, no more JS allocations
7. On pan/zoom, visible tile coordinates calculated
8. Tile render requests sent to Web Worker
9. Worker calls `render_tile` in WASM, returns RGBA data
10. deck.gl `BitmapLayer` renders tiles with crossfade transitions

### Why Pre-Aggregation?

| Metric | Raw in-browser | Pre-aggregated |
|--------|---------------|----------------|
| Download size | 24 GB | ~500 MB |
| First-load time | 5-15 minutes | 30-60 seconds |
| Browser memory | ~1 GB+ | ~100 MB |
| Stability | Prone to crashes | Stable |

### Why OPFS?

- **Zero server cost** — data lives in the browser after initial fetch
- **Fast queries** — no network latency; DuckDB reads directly from local storage
- **Persistent** — data survives page reloads and browser restarts
- **Sandboxed** — isolated per origin, no cross-site access
- **Large files** — handles multi-gigabyte Parquet files that would exceed `localStorage` or `IndexedDB` limits

### Remote HTTP Fallback

If the Parquet file cannot be downloaded to OPFS (e.g., CORS restrictions, no OPFS support, or network issues), the app automatically falls back to querying the remote file directly over HTTP. DuckDB-WASM's `httpfs` extension uses HTTP range requests to download only the parts of the Parquet file needed for each query, dramatically reducing bandwidth for selective queries. All in-memory tables are still built, so density tiles and ship tracks work identically. The tradeoff is slower initial load and no persistence across page reloads.

## Building

### Prerequisites

- Rust toolchain with `wasm32-unknown-unknown` target
- `lld` linker

### Quick Start with devenv (Recommended)

This project includes a [`devenv.nix`](devenv.nix) configuration that provides a reproducible development shell with all dependencies pre-installed:

```bash
devenv shell
```

This gives you:
- **Rust toolchain** — compiler, cargo, and `wasm32-unknown-unknown` target
- **DuckDB CLI** — for inspecting Parquet files directly
- **Python + duckdb** — for running `aggregate.py`
- **Node.js + pnpm** — for serving the project
- **rclone** — for data transfer between storage backends
- **Git** — version control

No need to install anything globally. The shell is isolated and reproducible — same environment on any machine with Nix.

**First time with Nix/devenv?** See the official installation guide at <https://devenv.sh/getting-started/> for step-by-step instructions on installing Nix, enabling flakes, and installing devenv.

### Manual Setup

### Build WASM

```bash
cargo build --target wasm32-unknown-unknown --release
cp target/wasm32-unknown-unknown/release/ducklake_wasm_density.wasm density_engine.wasm
```

### Run

Serve the project directory with any static file server (e.g., `python -m http.server 8080`) and open `index.html`. The default query expects pre-aggregated files (`ais_position_points.parquet` and `ais_latest_positions.parquet`) available at the same origin.

**Server requirements for remote fallback:**
- Must support HTTP Range requests (`Accept-Ranges: bytes` header)
- Must serve the Parquet file with appropriate CORS headers if accessed from a different origin
- Python's built-in `http.server` supports range requests natively

## Configuration Constants

| Constant | Value | Description |
|---|---|---|
| `GRID_RESOLUTION` | 512 | Spatial index grid size (Rust) |
| `DENSITY_TILE_SIZE` | 512 | Tile pixel dimensions |
| `DENSITY_TILE_MAX_ZOOM` | 20 | Maximum render zoom |
| `DENSITY_TILE_CACHE_LIMIT` | 256 | Max cached tiles |
| `DENSITY_TILE_FADE_MS` | 220 | Crossfade duration |
| `DENSITY_TILE_REQUEST_DEBOUNCE_MS` | 150 | Tile request debounce |
| `MAX_INDIVIDUAL_SHIPS` | 30000 | Max ship circles before falling back to density |
| `SHIP_GLYPH_MIN_ZOOM` | 8 | Zoom level where ship circles appear |

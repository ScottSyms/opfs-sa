# In-Browser DuckDB

A high-performance geospatial visualization tool that runs entirely in the browser. Uses the **Origin Private File System (OPFS)** to store large Parquet files locally, then queries them with in-browser DuckDB and renders millions of points using WebAssembly-accelerated density raster tiles.

## What It Does

In-Browser DuckDB fetches a Parquet file (e.g., AIS ship position data) once and stores it in the browser's **OPFS** — a performant, persistent, sandboxed file system. All subsequent queries run against this local copy, eliminating network latency and server costs. The visualization uses a hybrid rendering approach:

- **Aggregate density tiles** (zoom levels 0–20): Renders point density as Datashader-style raster tiles computed in Rust/WASM. Fractional zoom levels round up to the next whole zoom, with higher-zoom tiles layered on top for better resolution.
- **Ship circles** (zoom 8+): Shows individual ship positions as GPU-instanced circles with white borders, color-coded by speed.

## Key Features

- **OPFS-backed data** — Parquet files stored in the browser's Origin Private File System; fetch once, query forever with zero server cost
- **9.7M+ points** rendered smoothly using a 512×512 uniform grid spatial index in Rust
- **Web Worker offload** — tile rendering runs off the main thread so the UI stays responsive
- **Double-buffered tile transitions** with crossfade for smooth zoom/pan
- **Tile request debouncing** and coalescing to prevent wasted computation during rapid navigation
- **Parent tile fallback** — cached lower-zoom tiles shown while higher-zoom tiles load
- **Aggregate opacity slider** and zoom level display
- **Ad-hoc SQL queries** via DuckDB-WASM against the local Parquet file (does not affect the map)
- **Kepler.gl** integration for query result visualization

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
- **DuckDB-WASM** — SQL queries against OPFS-stored Parquet file
- **Web Worker** (`density_worker.js`) — offloads WASM tile rendering

### Data Flow

1. Parquet file fetched once from a URL and persisted in the browser's **OPFS** (Origin Private File System)
2. DuckDB-WASM registers the OPFS file and runs SQL queries entirely locally — no server round-trips
3. Coordinates copied to WASM memory via `load_points`
4. Spatial index built during load (~512×512 grid)
5. On pan/zoom, visible tile coordinates calculated
6. Tile render requests sent to Web Worker
7. Worker calls `render_tile` in WASM, returns RGBA data
8. deck.gl `BitmapLayer` renders tiles with crossfade transitions

### Why OPFS?

- **Zero server cost** — data lives in the browser after initial fetch
- **Fast queries** — no network latency; DuckDB reads directly from local storage
- **Persistent** — data survives page reloads and browser restarts
- **Sandboxed** — isolated per origin, no cross-site access
- **Large files** — handles multi-gigabyte Parquet files that would exceed `localStorage` or `IndexedDB` limits

## Building

### Prerequisites

- Rust toolchain with `wasm32-unknown-unknown` target
- `lld` linker

### Build WASM

```bash
cargo build --target wasm32-unknown-unknown --release
cp target/wasm32-unknown-unknown/release/ducklake_wasm_density.wasm density_engine.wasm
```

### Run

Serve the project directory with any static file server (e.g., `python -m http.server 8080`) and open `index.html`. The default query expects a `test.parquet` file available at `http://localhost:8080/test.parquet`.

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

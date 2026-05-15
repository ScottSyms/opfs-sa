use std::alloc::{alloc, dealloc, Layout};
use std::f64::consts::PI;
use std::mem;
use std::slice;
use std::sync::{Mutex, OnceLock};

const MERCATOR_MAX_LATITUDE: f64 = 85.051_128_78;
const GRID_RESOLUTION: u32 = 512;

#[derive(Default)]
struct PointStore {
    x: Vec<f32>,
    y: Vec<f32>,
    rgba: Vec<u8>,
    cell_starts: Vec<u32>,
    cell_counts: Vec<u32>,
}

static POINTS: OnceLock<Mutex<PointStore>> = OnceLock::new();

fn point_store() -> &'static Mutex<PointStore> {
    POINTS.get_or_init(|| Mutex::new(PointStore::default()))
}

#[no_mangle]
pub extern "C" fn alloc_bytes(len: usize) -> *mut u8 {
    if len == 0 {
        return std::ptr::null_mut();
    }

    let layout = Layout::from_size_align(len, mem::align_of::<f64>()).unwrap();
    unsafe { alloc(layout) }
}

#[no_mangle]
pub extern "C" fn dealloc_bytes(ptr: *mut u8, len: usize) {
    if ptr.is_null() || len == 0 {
        return;
    }

    let layout = Layout::from_size_align(len, mem::align_of::<f64>()).unwrap();
    unsafe { dealloc(ptr, layout) };
}

#[no_mangle]
pub extern "C" fn load_points(lat_ptr: *const f64, lon_ptr: *const f64, len: usize) -> usize {
    if lat_ptr.is_null() || lon_ptr.is_null() || len == 0 {
        let mut points = point_store().lock().unwrap();
        points.x.clear();
        points.y.clear();
        points.cell_starts.clear();
        points.cell_counts.clear();
        return 0;
    }

    let latitudes = unsafe { slice::from_raw_parts(lat_ptr, len) };
    let longitudes = unsafe { slice::from_raw_parts(lon_ptr, len) };
    let mut cell_indices = Vec::with_capacity(len);
    let mut x = Vec::with_capacity(len);
    let mut y = Vec::with_capacity(len);

    for index in 0..len {
        let latitude = latitudes[index];
        let longitude = longitudes[index];
        if !latitude.is_finite()
            || !longitude.is_finite()
            || !(-MERCATOR_MAX_LATITUDE..=MERCATOR_MAX_LATITUDE).contains(&latitude)
            || !(-180.0..=180.0).contains(&longitude)
        {
            continue;
        }

        let latitude_radians = latitude.to_radians();
        let world_x = ((longitude + 180.0) / 360.0) as f32;
        let world_y = ((1.0 - ((latitude_radians.tan() + (1.0 / latitude_radians.cos())).ln() / PI)) / 2.0) as f32;
        if world_x.is_finite() && world_y.is_finite() {
            let clamped_x = world_x.clamp(0.0, 1.0);
            let clamped_y = world_y.clamp(0.0, 1.0);
            let cell_x = (clamped_x as f64 * GRID_RESOLUTION as f64).min(GRID_RESOLUTION as f64 - 1.0) as u32;
            let cell_y = (clamped_y as f64 * GRID_RESOLUTION as f64).min(GRID_RESOLUTION as f64 - 1.0) as u32;
            let cell_index = cell_y * GRID_RESOLUTION + cell_x;
            cell_indices.push(cell_index);
            x.push(clamped_x);
            y.push(clamped_y);
        }
    }

    let count = x.len();
    let mut points = point_store().lock().unwrap();

    let grid_size = (GRID_RESOLUTION * GRID_RESOLUTION) as usize;
    let mut cell_counts = vec![0_u32; grid_size];
    for &cell_index in &cell_indices {
        cell_counts[cell_index as usize] += 1;
    }

    let mut cell_starts = vec![0_u32; grid_size + 1];
    let mut current_start = 0_u32;
    for (cell_index, &count) in cell_counts.iter().enumerate() {
        cell_starts[cell_index] = current_start;
        current_start += count;
    }
    cell_starts[grid_size] = current_start;

    let mut sorted_x = vec![0.0_f32; count];
    let mut sorted_y = vec![0.0_f32; count];
    let mut temp_positions = cell_starts[..grid_size].to_vec();

    for index in 0..count {
        let cell_index = cell_indices[index] as usize;
        let position = temp_positions[cell_index] as usize;
        sorted_x[position] = x[index];
        sorted_y[position] = y[index];
        temp_positions[cell_index] += 1;
    }

    points.x = sorted_x;
    points.y = sorted_y;
    points.cell_starts = cell_starts;
    points.cell_counts = cell_counts;
    count
}

#[no_mangle]
pub extern "C" fn point_count() -> usize {
    point_store().lock().unwrap().x.len()
}

#[no_mangle]
pub extern "C" fn render_tile(z: u32, x: u32, y: u32, size: usize) -> *const u8 {
    if size == 0 || size > 4096 {
        return std::ptr::null();
    }

    let mut points = point_store().lock().unwrap();
    let pixel_count = size.saturating_mul(size);
    let mut counts = vec![0_u32; pixel_count];
    let scale = (1_u64 << z.min(31)) as f64;
    let tile_x = x as f64;
    let tile_y = y as f64;
    let size_f64 = size as f64;

    let tile_min_world_x = tile_x / scale;
    let tile_max_world_x = (tile_x + 1.0) / scale;
    let tile_min_world_y = tile_y / scale;
    let tile_max_world_y = (tile_y + 1.0) / scale;

    let min_cell_x = (tile_min_world_x * GRID_RESOLUTION as f64).max(0.0) as u32;
    let max_cell_x = ((tile_max_world_x * GRID_RESOLUTION as f64).min(GRID_RESOLUTION as f64 - 1.0)) as u32;
    let min_cell_y = (tile_min_world_y * GRID_RESOLUTION as f64).max(0.0) as u32;
    let max_cell_y = ((tile_max_world_y * GRID_RESOLUTION as f64).min(GRID_RESOLUTION as f64 - 1.0)) as u32;

    if points.cell_starts.len() <= (GRID_RESOLUTION * GRID_RESOLUTION) as usize {
        return std::ptr::null();
    }

    for cell_y in min_cell_y..=max_cell_y {
        for cell_x in min_cell_x..=max_cell_x {
            let cell_index = (cell_y * GRID_RESOLUTION + cell_x) as usize;
            let start = points.cell_starts[cell_index] as usize;
            let end = points.cell_starts[cell_index + 1] as usize;

            for index in start..end {
                let px = (((points.x[index] as f64 * scale) - tile_x) * size_f64).floor() as isize;
                let py = (((points.y[index] as f64 * scale) - tile_y) * size_f64).floor() as isize;
                if px < 0 || py < 0 || px >= size as isize || py >= size as isize {
                    continue;
                }

                let pixel_index = py as usize * size + px as usize;
                counts[pixel_index] = counts[pixel_index].saturating_add(1);
            }
        }
    }

    let max_count = counts.iter().copied().max().unwrap_or(0);
    let max_log_count = (max_count as f64).ln_1p().max(1.0);
    points.rgba.clear();
    points.rgba.resize(pixel_count * 4, 0);

    for (pixel_index, count) in counts.into_iter().enumerate() {
        if count == 0 {
            continue;
        }

        let value = (count as f64).ln_1p() / max_log_count;
        let [red, green, blue, alpha] = density_color(value);
        let rgba_index = pixel_index * 4;
        points.rgba[rgba_index] = red;
        points.rgba[rgba_index + 1] = green;
        points.rgba[rgba_index + 2] = blue;
        points.rgba[rgba_index + 3] = alpha;
    }

    points.rgba.as_ptr()
}

#[no_mangle]
pub extern "C" fn rendered_tile_len(size: usize) -> usize {
    size.saturating_mul(size).saturating_mul(4)
}

fn density_color(value: f64) -> [u8; 4] {
    if value <= 0.0 {
        return [0, 0, 0, 0];
    }

    if value < 0.18 {
        [88, 24, 169, (55.0 + value * 230.0).round() as u8]
    } else if value < 0.38 {
        [199, 24, 166, (90.0 + value * 225.0).round() as u8]
    } else if value < 0.62 {
        [255, 53, 127, (120.0 + value * 180.0).round() as u8]
    } else if value < 0.82 {
        [255, 140, 41, (150.0 + value * 105.0).round() as u8]
    } else {
        [255, 238, 88, 245]
    }
}

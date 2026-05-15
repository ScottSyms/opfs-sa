let wasmInstance = null;
let wasmExports = null;
let pointsLoaded = false;

self.onmessage = async function (event) {
  const { type } = event.data;

  if (type === "init") {
    try {
      const { wasmUrl } = event.data;
      const response = await fetch(wasmUrl);
      if (!response.ok) {
        throw new Error(`Failed to load WASM: ${response.status}`);
      }

      const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), {});
      wasmInstance = instance;
      wasmExports = instance.exports;
      pointsLoaded = false;

      self.postMessage({ type: "init_complete", success: true });
    } catch (error) {
      self.postMessage({ type: "init_complete", success: false, error: error.message });
    }
    return;
  }

  if (type === "load_points") {
    if (!wasmExports) {
      self.postMessage({ type: "load_points_complete", success: false, error: "WASM not initialized" });
      return;
    }

    try {
      const { latitudes, longitudes } = event.data;
      const length = latitudes.length;

      const byteLength = latitudes.byteLength;
      const latPtr = wasmExports.alloc_bytes(byteLength);
      const lonPtr = wasmExports.alloc_bytes(byteLength);

      if (!latPtr || !lonPtr) {
        throw new Error(`Failed to allocate ${byteLength} bytes in WASM memory`);
      }

      new Float64Array(wasmExports.memory.buffer, latPtr, length).set(latitudes);
      new Float64Array(wasmExports.memory.buffer, lonPtr, length).set(longitudes);

      const loadedCount = wasmExports.load_points(latPtr, lonPtr, length);

      wasmExports.dealloc_bytes(latPtr, byteLength);
      wasmExports.dealloc_bytes(lonPtr, byteLength);

      pointsLoaded = true;
      self.postMessage({ type: "load_points_complete", success: true, count: loadedCount });
    } catch (error) {
      self.postMessage({ type: "load_points_complete", success: false, error: error.message });
    }
    return;
  }

  if (type === "render_tile") {
    if (!wasmExports) {
      self.postMessage({ type: "tile_ready", success: false, error: "WASM not initialized", key: event.data.key });
      return;
    }

    try {
      const { z, x, y, size, key } = event.data;
      const ptr = wasmExports.render_tile(z, x, y, size);

      if (!ptr) {
        self.postMessage({ type: "tile_ready", success: false, error: "Render returned null", key });
        return;
      }

      const byteLength = wasmExports.rendered_tile_len(size);
      const rgba = new Uint8ClampedArray(byteLength);
      rgba.set(new Uint8Array(wasmExports.memory.buffer, ptr, byteLength));

      const transferBuffer = rgba.buffer.slice(0);
      const transferred = new Uint8ClampedArray(transferBuffer);

      self.postMessage(
        {
          type: "tile_ready",
          success: true,
          key,
          rgba: transferred,
          size,
        },
        [transferBuffer],
      );
    } catch (error) {
      self.postMessage({ type: "tile_ready", success: false, error: error.message, key: event.data.key });
    }
    return;
  }

  if (type === "point_count") {
    if (!wasmExports) {
      self.postMessage({ type: "point_count_result", count: 0 });
      return;
    }
    const count = wasmExports.point_count();
    self.postMessage({ type: "point_count_result", count });
  }
};

"""GPU-only painting feedback for Petrify Painter.

The stroke owns the weights and final commit.  This module only reads the cached
evaluated geometry; it never edits a Blender mesh, material, or dependency graph.
GPU objects are created lazily from the viewport draw callback.
"""

import time


class ViewportOverlayState:
    """Keep Blender's scene depth available while a brush is active.

    In material preview with overlays hidden, POST_VIEW can receive an empty
    depth buffer cleared to zero. LESS_EQUAL then rejects every painted pixel.
    Temporarily enable the overlay pass, without revealing bones and guides
    that the user had hidden. Restore all changed display settings on exit.
    """

    _GUIDES = (
        'show_bones', 'show_extras', 'show_floor', 'show_axis_x', 'show_axis_y',
        'show_axis_z', 'show_cursor', 'show_text', 'show_stats',
        'show_outline_selected', 'show_relationship_lines', 'show_motion_paths',
        'show_wireframes', 'show_object_origins', 'show_object_origins_all',
    )

    def __init__(self, space):
        self.overlay = space.overlay
        self.saved = {}

    def enable(self):
        if self.overlay.show_overlays:
            return
        self.saved = {'show_overlays': False}
        for name in self._GUIDES:
            if hasattr(self.overlay, name):
                self.saved[name] = getattr(self.overlay, name)
                setattr(self.overlay, name, False)
        self.overlay.show_overlays = True

    def restore(self):
        saved, self.saved = self.saved, {}
        for name, value in saved.items():
            try:
                setattr(self.overlay, name, value)
            except (ReferenceError, RuntimeError):
                pass  # The invoking editor/window may already have closed.


def _pointer(value):
    return value.as_pointer() if value is not None else None


def _triangles(entry):
    """Prefer Blender's evaluated loop triangles; support older cache objects."""
    if 'triangles' in entry:
        return entry['triangles']
    from mathutils.geometry import tessellate_polygon
    coords = entry['coords']
    result = []
    for polygon in entry['polygons']:
        if len(polygon) == 3:
            result.append(tuple(polygon))
        elif len(polygon) > 3:
            points = [coords[i] for i in polygon]
            # Blender 4.5 returns indices within the supplied polygon points.
            result.extend(tuple(polygon[i] for i in triangle)
                          for triangle in tessellate_polygon([points]))
    return result


def _create_shader(gpu):
    interface = gpu.types.GPUStageInterfaceInfo('petrify_overlay_interface')
    interface.smooth('FLOAT', 'coverage')
    interface.smooth('FLOAT', 'lighting')
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', 'viewProjectionMatrix')
    info.push_constant('VEC4', 'overlayTint')
    info.vertex_in(0, 'VEC3', 'position')
    info.vertex_in(1, 'VEC3', 'normal')
    info.vertex_in(2, 'FLOAT', 'weight')
    info.vertex_out(interface)
    info.fragment_out(0, 'VEC4', 'fragmentColor')
    info.vertex_source('''
        void main() {
            vec4 clip = viewProjectionMatrix * vec4(position, 1.0);
            // Avoid z-fighting with the source surface without moving geometry.
            clip.z -= 0.00003 * clip.w;
            gl_Position = clip;
            coverage = weight;
            lighting = 0.64 + 0.36 * abs(dot(normal, normalize(vec3(0.35, -0.45, 0.85))));
        }
    ''')
    info.fragment_source('''
        void main() {
            float alpha = clamp(coverage, 0.0, 1.0) * overlayTint.a;
            if (alpha <= 0.0001) discard;
            fragmentColor = vec4(overlayTint.rgb * lighting, alpha);
        }
    ''')
    return gpu.shader.create_from_info(info)


class OverlayPreview:
    """Read-only brush coverage, limited to the invoking 3D viewport.

    Call ``mark_dirty`` after weights change, and ``draw`` from POST_VIEW even
    when the cursor is hidden during navigation. A modal timer can request a
    redraw while ``needs_redraw`` is true, so the last throttled update is shown
    after a pen-up. ``dispose`` is safe outside a GPU context.
    """

    def __init__(self, stroke, erase=False, refresh_hz=30.0, area=None, region=None):
        self.stroke = stroke
        self.erase = bool(erase)
        self._area = _pointer(area)
        self._region = _pointer(region)
        self._interval = 1.0 / max(1.0, float(refresh_hz))
        self._last_upload = float('-inf')
        self._shader = None
        self._geometry = {}
        self._batches = {}
        self._dirty = {entry['ob'].name for entry in stroke.cache.entries}
        self._disposed = False
        self.stats = {'geometry_builds': 0, 'weight_uploads': 0, 'draw_calls': 0}

    @property
    def needs_redraw(self):
        return bool(self._dirty) and not self._disposed

    def mark_dirty(self, object_names=None):
        if self._disposed:
            return
        if object_names is None:
            object_names = (entry['ob'].name for entry in self.stroke.cache.entries)
        self._dirty.update(object_names)

    def _make_geometry(self, gpu, entry):
        triangles = _triangles(entry)
        if not triangles:
            return None
        vertex_format = gpu.types.GPUVertFormat()
        vertex_format.attr_add(id='position', comp_type='F32', len=3, fetch_mode='FLOAT')
        vertex_format.attr_add(id='normal', comp_type='F32', len=3, fetch_mode='FLOAT')
        vertices = gpu.types.GPUVertBuf(format=vertex_format, len=entry['count'])
        vertices.attr_fill(id='position', data=entry['coords'])
        vertices.attr_fill(id='normal', data=entry['normals'])
        indices = gpu.types.GPUIndexBuf(type='TRIS', seq=triangles)
        self.stats['geometry_builds'] += 1
        return vertices, indices

    def _upload(self, gpu, now):
        if not self._dirty or now - self._last_upload < self._interval:
            return
        for entry in self.stroke.cache.entries:
            name = entry['ob'].name
            if name not in self._dirty:
                continue
            weights = self.stroke.weights[name]
            if not any(weights):
                self._batches.pop(name, None)
                continue
            if name not in self._geometry:
                self._geometry[name] = self._make_geometry(gpu, entry)
            geometry = self._geometry[name]
            if geometry is None:
                continue
            # Blender's Python VBO API exposes static buffers. Keep the expensive
            # coordinates, normals, and indices; replace only this scalar buffer
            # and its inexpensive batch wrapper when coverage changes.
            weight_format = gpu.types.GPUVertFormat()
            weight_format.attr_add(id='weight', comp_type='F32', len=1, fetch_mode='FLOAT')
            weight_buffer = gpu.types.GPUVertBuf(format=weight_format, len=entry['count'])
            weight_buffer.attr_fill(id='weight', data=weights)
            batch = gpu.types.GPUBatch(type='TRIS', buf=geometry[0], elem=geometry[1])
            batch.vertbuf_add(weight_buffer)
            self._batches[name] = (batch, weight_buffer)
            self.stats['weight_uploads'] += 1
        self._dirty.clear()
        self._last_upload = now

    def draw(self, context=None):
        if self._disposed:
            return
        if context is None:
            import bpy
            context = bpy.context
        if self._area is not None and _pointer(context.area) != self._area:
            return
        if self._region is not None and _pointer(context.region) != self._region:
            return
        if context.region_data is None:
            return
        import gpu
        self._upload(gpu, time.perf_counter())
        if not self._batches:
            return
        if self._shader is None:
            self._shader = _create_shader(gpu)
        previous = (gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get())
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(False)
            self._shader.bind()
            self._shader.uniform_float('viewProjectionMatrix', context.region_data.perspective_matrix)
            self._shader.uniform_float('overlayTint',
                                       (1.0, 0.32, 0.07, 0.82) if self.erase else (0.48, 0.51, 0.54, 0.88))
            for batch, weight_buffer in self._batches.values():
                batch.draw(self._shader)
                self.stats['draw_calls'] += 1
        finally:
            gpu.state.blend_set(previous[0])
            gpu.state.depth_test_set(previous[1])
            gpu.state.depth_mask_set(previous[2])

    def dispose(self):
        self._disposed = True
        self._batches.clear()
        self._geometry.clear()
        self._dirty.clear()
        self._shader = None
        self.stroke = None

"""Temporary, reversible lightweight materials for Petrify Painter.

No original node tree is edited.  Material overrides belong to objects, so a
linked mesh outside the preview selection keeps its original appearance.
Petrify time masks are copied as small, driver-free math graphs.  Scene time
is read by a View Layer attribute, avoiding animated shader-group dependencies.
"""

import json
import traceback
import bpy
from bpy.app.handlers import persistent


PROXY_TAG = '_pp_light_preview'
ORIGINAL_KEY = '_pp_preview_original'
RESTORE_KEY = '_pp_preview_restore'
_targets = []
_pending_targets = []
_handler_targets = {}
_api = None
_last_error = ''


def _node(tree, kind, name):
    node = tree.nodes.new(kind)
    node.name = name
    node.label = name
    return node


def _math(tree, operation, a, b, name):
    node = _node(tree, 'ShaderNodeMath', name)
    node.operation = operation
    for socket, value in zip(node.inputs, (a, b)):
        if isinstance(value, (float, int)):
            socket.default_value = value
        else:
            tree.links.new(value, socket)
    return node.outputs[0]


def _image_is_base(node):
    return (node.type == 'TEX_IMAGE' and node.image is not None
            and node.name != 'Petrify Mask' and not node.image.get('petrify_mask'))


def _copy_image_user(source, target):
    for prop in ('frame_duration', 'frame_start', 'frame_offset', 'use_auto_refresh', 'use_cyclic', 'tile'):
        if hasattr(source.image_user, prop) and hasattr(target.image_user, prop):
            setattr(target.image_user, prop, getattr(source.image_user, prop))


def _walk_nodes(tree, seen=None):
    seen = set() if seen is None else seen
    if not tree or tree.as_pointer() in seen:
        return
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        yield node
        if node.type == 'GROUP' and node.node_tree and not node.name.startswith('Petrify'):
            if not node.node_tree.get('petrify_stroke') and not node.node_tree.get('petrify_preset'):
                yield from _walk_nodes(node.node_tree, seen)


def _upstream_image(socket, seen=None):
    seen = set() if seen is None else seen
    for link in socket.links:
        node = link.from_node
        key = node.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        if _image_is_base(node):
            return node
        if node.type == 'NORMAL_MAP' or node.name.startswith('Petrify'):
            continue
        for inp in node.inputs:
            found = _upstream_image(inp, seen)
            if found:
                return found
    return None


def _base_description(material):
    """Prefer MMD's explicit base texture, then a Principled base-color link.

    Complex custom shader groups have no universal base color.  For those we
    use an obvious color input/image, or material display color, and report a
    simplified appearance.  Normal/roughness/mask images are never a fallback.
    """
    color = tuple(material.diffuse_color)
    alpha = color[3]
    if not material.use_nodes or not material.node_tree:
        return None, color, alpha, False, False
    nodes = list(_walk_nodes(material.node_tree))
    image = next((n for n in nodes if n.name == 'mmd_base_tex' and _image_is_base(n)), None)
    principals = [n for n in nodes if n.type == 'BSDF_PRINCIPLED']
    principal = next((n for n in principals if n.inputs['Base Color'].is_linked),
                     principals[0] if principals else None)
    if not image and principal:
        image = _upstream_image(principal.inputs['Base Color'])
    if not image:
        for node in material.node_tree.nodes:
            if node.type != 'GROUP' or node.name.startswith('Petrify'):
                continue
            for inp in node.inputs:
                if inp.name.casefold() in {'base color', 'base colour', 'color', 'colour', 'diffuse', 'diffuse color', 'a'}:
                    image = _upstream_image(inp)
                    if image:
                        break
            if image:
                break
    if principal and not image:
        color = tuple(principal.inputs['Base Color'].default_value)
    if principal and not principal.inputs['Alpha'].is_linked:
        alpha = float(principal.inputs['Alpha'].default_value)
    # Use alpha only when the original graph consumes the image alpha.  Some
    # models intentionally ignore alpha channels in their diffuse textures.
    image_alpha = bool(image and image.outputs['Alpha'].is_linked)
    mmd = material.node_tree.nodes.get('mmd_shader')
    if mmd and mmd.inputs.get('Alpha') and not mmd.inputs['Alpha'].is_linked:
        alpha = float(mmd.inputs['Alpha'].default_value)
    if image and image.name == 'mmd_base_tex':
        image_alpha = True
    return image, color, alpha, image_alpha, bool(not image and not principal)


def _copy_vector_input(source, target, tree, warnings):
    """Copy simple UV/mapping inputs without retaining a complex source group."""
    cache = {}

    def clone(socket):
        node = socket.node
        key = node.as_pointer()
        if node.type == 'GROUP' and (node.name == 'mmd_tex_uv' or (node.node_tree and node.node_tree.name.startswith('MMDTexUV'))):
            uv = _node(tree, 'ShaderNodeTexCoord', 'Preview Original UV')
            return uv.outputs['UV']
        if node.type not in {'UVMAP', 'TEX_COORD', 'MAPPING', 'VECTOR_MATH', 'REROUTE'}:
            return None
        if key not in cache:
            copied = _node(tree, node.bl_idname, 'Preview UV ' + node.name)
            cache[key] = copied
            for prop in ('uv_map', 'from_instancer', 'vector_type', 'operation', 'object'):
                if hasattr(node, prop) and hasattr(copied, prop):
                    setattr(copied, prop, getattr(node, prop))
            for old, new in zip(node.inputs, copied.inputs):
                if hasattr(old, 'default_value') and hasattr(new, 'default_value'):
                    new.default_value = old.default_value
                if old.is_linked:
                    upstream = clone(old.links[0].from_socket)
                    if upstream is not None:
                        tree.links.new(upstream, new)
                    elif new.type == 'VECTOR':
                        uv = _node(tree, 'ShaderNodeTexCoord', 'Preview Fallback UV')
                        tree.links.new(uv.outputs['UV'], new)
                        warnings.add('复杂纹理坐标已简化为模型活动渲染 UV')
        return cache[key].outputs[list(node.outputs).index(socket)]

    vector = source.inputs.get('Vector')
    if vector and vector.is_linked:
        copied = clone(vector.links[0].from_socket)
        if copied is not None:
            tree.links.new(copied, target.inputs['Vector'])
        else:
            warnings.add('复杂纹理坐标已简化为模型活动渲染 UV')


def _copy_mask(material, target, warnings):
    strength = material.node_tree.nodes.get('Petrify Strength') if material.node_tree else None
    if not strength:
        return 0.0
    cache = {}
    allowed = {'MATH', 'VALUE', 'ATTRIBUTE', 'TEX_IMAGE', 'UVMAP', 'TEX_COORD', 'REROUTE'}

    def clone(socket):
        node = socket.node
        tree = node.id_data
        path = tree.get('petrify_stroke', False)
        key = node.as_pointer()
        if path and node.type == 'VALUE' and node.name == 'Frame':
            if 'FRAME' not in cache:
                frame = _node(target, 'ShaderNodeAttribute', 'Preview Timeline Frame')
                frame.attribute_type = 'VIEW_LAYER'
                # Unlike frame_current, this includes subframes and time remap.
                frame.attribute_name = 'frame_current_final'
                cache['FRAME'] = frame
            return cache['FRAME'].outputs['Fac']
        if node.type == 'GROUP':
            if not node.node_tree or not node.node_tree.get('petrify_stroke'):
                raise RuntimeError('蒙版连接到不支持的自定义节点组：' + node.name)
            output = next(n for n in node.node_tree.nodes
                          if n.type == 'GROUP_OUTPUT' and n.is_active_output)
            index = list(node.outputs).index(socket)
            source = output.inputs[index]
            if not source.is_linked:
                value = _node(target, 'ShaderNodeValue', 'Preview Unlinked Path')
                value.outputs[0].default_value = source.default_value
                return value.outputs[0]
            return clone(source.links[0].from_socket)
        if key not in cache:
            if node.type not in allowed:
                raise RuntimeError('蒙版连接到不支持的节点：' + node.name)
            prefix = tree.get('weight_attr', '') + ' / ' if path else ''
            copied = _node(target, node.bl_idname, prefix + node.name)
            cache[key] = copied
            for prop in ('operation', 'use_clamp', 'attribute_name', 'attribute_type',
                         'uv_map', 'from_instancer', 'interpolation', 'projection',
                         'projection_blend', 'extension', 'image'):
                if hasattr(node, prop) and hasattr(copied, prop):
                    setattr(copied, prop, getattr(node, prop))
            for old, new in zip(node.inputs, copied.inputs):
                if hasattr(old, 'default_value') and hasattr(new, 'default_value'):
                    new.default_value = old.default_value
                if old.is_linked:
                    target.links.new(clone(old.links[0].from_socket), new)
            if node.type == 'VALUE':
                copied.outputs[0].default_value = node.outputs[0].default_value
            if node.type == 'TEX_IMAGE':
                _copy_image_user(node, copied)
        return cache[key].outputs[list(node.outputs).index(socket)]

    return clone(strength.outputs[0])


def _stone_color(material, context):
    # Imported/custom stone presets deliberately use a neutral gray here;
    # preserving their shader graph would defeat this lightweight mode.
    rock = material.node_tree.nodes.get('Petrify Stone') if material.node_tree else None
    if rock and rock.node_tree:
        group = rock.node_tree
        control = group.nodes.get('Stone Color')
        if control and len(control.inputs) > 2 and hasattr(control.inputs[2], 'default_value'):
            return tuple(control.inputs[2].default_value)
        preset = group.nodes.get('Preset Material')
        if preset and preset.inputs.get('Stone Color'):
            return tuple(preset.inputs['Stone Color'].default_value)
    settings = getattr(context.scene, 'petrify_settings', None) if context else None
    return tuple(settings.color) if settings and hasattr(settings, 'color') else (0.32, 0.34, 0.36, 1.0)


def _make_proxy(original, context, warnings):
    proxy = bpy.data.materials.new('PP 轻量预览 · ' + original.name)
    proxy[PROXY_TAG] = True
    proxy[ORIGINAL_KEY] = original
    proxy.use_nodes = True
    proxy.diffuse_color = original.diffuse_color
    proxy.use_backface_culling = original.use_backface_culling
    proxy.surface_render_method = 'DITHERED'
    proxy.use_transparency_overlap = getattr(original, 'use_transparency_overlap', True)
    tree = proxy.node_tree
    tree.nodes.clear()
    image_node, color, alpha, use_image_alpha, fallback = _base_description(original)
    if fallback:
        warnings.add('部分自定义材质使用显示颜色；完整着色和材质动画仅在完整模式显示')
    image = None
    if image_node:
        image = _node(tree, 'ShaderNodeTexImage', 'Preview Base Texture')
        image.image = image_node.image
        image.interpolation = image_node.interpolation
        image.extension = image_node.extension
        image.projection = image_node.projection
        image.projection_blend = image_node.projection_blend
        _copy_image_user(image_node, image)
        _copy_vector_input(image_node, image, tree, warnings)
    mix = _node(tree, 'ShaderNodeMixRGB', 'Preview Stone Color')
    mix.blend_type = 'MIX'
    mix.inputs[1].default_value = color
    mix.inputs[2].default_value = _stone_color(original, context)
    if image:
        tree.links.new(image.outputs['Color'], mix.inputs[1])
    mask = _copy_mask(original, tree, warnings)
    if isinstance(mask, (float, int)):
        mix.inputs[0].default_value = mask
    else:
        tree.links.new(mask, mix.inputs[0])
    surface = _node(tree, 'ShaderNodeBsdfPrincipled', 'Preview Simple Surface')
    surface.inputs['Roughness'].default_value = 0.8
    surface.inputs['Specular IOR Level'].default_value = 0.2
    tree.links.new(mix.outputs[0], surface.inputs['Base Color'])
    surface.inputs['Alpha'].default_value = max(0.0, min(1.0, alpha))
    if image and use_image_alpha:
        value = image.outputs['Alpha']
        if abs(alpha - 1.0) > 1e-6:
            value = _math(tree, 'MULTIPLY', value, alpha, 'Preview Alpha')
        tree.links.new(value, surface.inputs['Alpha'])
    output = _node(tree, 'ShaderNodeOutputMaterial', 'Material Output')
    tree.links.new(surface.outputs[0], output.inputs['Surface'])
    return proxy


def is_active():
    return any(RESTORE_KEY in ob for ob in getattr(bpy.data, 'objects', ()))


def active_targets():
    return [ob for ob in getattr(bpy.data, 'objects', ()) if RESTORE_KEY in ob]


def last_error():
    return _last_error


def _restore():
    """Recover fresh datablocks, including proxies brought back by Undo."""
    restored = 0
    failures = []
    for ob in getattr(bpy.data, 'objects', ()):
        if ob.type != 'MESH':
            continue
        records = json.loads(ob.get(RESTORE_KEY, '[]'))
        recorded = {record['index']: record for record in records}
        for index, slot in enumerate(ob.material_slots):
            proxy = slot.material
            if not proxy or not proxy.get(PROXY_TAG):
                continue
            original = proxy.get(ORIGINAL_KEY)
            if not isinstance(original, bpy.types.Material):
                failures.append(ob.name + ' / ' + str(index) + '：找不到原材质')
                continue
            record = recorded.get(index)
            slot.material = original
            # An untracked proxy copied by the user can still safely become
            # its original material.  Its existing link mode is retained.
            if record:
                slot.link = record['link']
            restored += 1
        if RESTORE_KEY in ob and not any(s.material and s.material.get(PROXY_TAG) for s in ob.material_slots):
            del ob[RESTORE_KEY]
    for proxy in tuple(getattr(bpy.data, 'materials', ())):
        if not proxy.get(PROXY_TAG):
            continue
        if proxy.users == 0:
            bpy.data.materials.remove(proxy)
        else:
            failures.append(proxy.name + '：仍被使用，已保留以避免丢失数据')
    if failures:
        raise RuntimeError('轻量预览恢复未完成：' + '；'.join(failures))
    return restored


def exit():
    global _targets, _pending_targets
    restored = _restore()
    # Restore only generated ice companions hidden by this preview session.
    for ob in getattr(bpy.data, 'objects', ()):
        if 'petrify_ice_preview_hidden' in ob:
            ob.hide_set(bool(ob['petrify_ice_preview_hidden']))
            del ob['petrify_ice_preview_hidden']
    _targets = []
    _pending_targets = []
    return restored


def enter(context, meshes):
    global _targets, _last_error
    objects = [ob for ob in meshes if ob and ob.type == 'MESH']
    names = [ob.name for ob in objects]
    exit()
    warnings = set()
    cache = {}
    used = []
    try:
        for name in names:
            ob = bpy.data.objects.get(name)
            if not ob or ob.library or ob.is_editable is False:
                raise RuntimeError('轻量预览需要本地可编辑的网格对象：' + name)
            records = []
            # Store before the first assignment, so partial failures roll back.
            ob[RESTORE_KEY] = '[]'
            for index, slot in enumerate(ob.material_slots):
                original = slot.material
                if not original:
                    continue
                pointer = original.as_pointer()
                if pointer not in cache:
                    cache[pointer] = _make_proxy(original, context, warnings)
                records.append({'index': index, 'link': slot.link})
                ob[RESTORE_KEY] = json.dumps(records)
                slot.link = 'OBJECT'
                slot.material = cache[pointer]
            if records:
                used.append(name)
            else:
                del ob[RESTORE_KEY]
    except Exception:
        _restore()
        raise
    for name in used:
        source=bpy.data.objects.get(name)
        companion=source.get('petrify_ice_object') if source else None
        if isinstance(companion,bpy.types.Object) and companion.get('petrify_ice_companion'):
            companion['petrify_ice_preview_hidden']=companion.hide_get()
            companion.hide_set(True)
    _targets = used
    _last_error = ''
    return {'objects': len(used), 'materials': len(cache), 'warnings': sorted(warnings)}


def suspend(reason='edit'):
    global _pending_targets
    if not is_active():
        return False
    names = [ob.name for ob in active_targets()]
    exit()
    _pending_targets = names
    return True


def resume(context=None):
    global _pending_targets
    if not _pending_targets:
        return False
    names = list(_pending_targets)
    _pending_targets = []
    meshes = [ob for name in names if (ob := bpy.data.objects.get(name)) is not None]
    if meshes:
        enter(context or bpy.context, meshes)
        return True
    return False


def refresh(context=None):
    if not is_active():
        return False
    names = [ob.name for ob in active_targets()]
    enter(context or bpy.context, [bpy.data.objects[name] for name in names])
    return True


def _report_handler_failure():
    global _last_error
    _last_error = traceback.format_exc().strip().splitlines()[-1]
    print('[Petrify lightweight preview] ' + _last_error)
    traceback.print_exc()


def _pause_for(reason):
    if is_active():
        names = [ob.name for ob in active_targets()]
        exit()
        _handler_targets[reason] = names


def _resume_for(reason):
    names = _handler_targets.pop(reason, [])
    if names:
        meshes = [ob for name in names if (ob := bpy.data.objects.get(name)) is not None]
        if meshes:
            enter(bpy.context, meshes)


@persistent
def preview_save_pre(*_):
    try:
        _pause_for('save')
    except Exception:
        _report_handler_failure()
        raise


@persistent
def preview_save_post(*_):
    try:
        _resume_for('save')
    except Exception:
        _report_handler_failure()
        raise


@persistent
def preview_render_pre(*_):
    try:
        _pause_for('render')
    except Exception:
        _report_handler_failure()
        raise


@persistent
def preview_render_post(*_):
    try:
        _resume_for('render')
    except Exception:
        _report_handler_failure()
        raise


@persistent
def preview_reset(*_):
    try:
        _handler_targets.clear()
        exit()
    except Exception:
        _report_handler_failure()
        raise


HANDLERS = (
    ('save_pre', preview_save_pre), ('save_post', preview_save_post),
    ('save_post_fail', preview_save_post),
    ('render_init', preview_render_pre), ('render_pre', preview_render_pre),
    ('render_complete', preview_render_post), ('render_cancel', preview_render_post),
    ('undo_pre', preview_reset), ('undo_post', preview_reset),
    ('redo_pre', preview_reset), ('redo_post', preview_reset),
    ('load_pre', preview_reset), ('load_post', preview_reset),
)


def register(api=None):
    global _api
    _api = api
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback not in handlers:
            handlers.append(callback)


def unregister():
    global _api
    exit()
    _handler_targets.clear()
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback in handlers:
            handlers.remove(callback)
    _api = None

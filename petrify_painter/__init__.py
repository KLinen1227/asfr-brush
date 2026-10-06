EDITION = 'PUBLIC'
DISPLAY_NAME = 'ASFR笔刷0.0.1'

bl_info = {
    'name': 'ASFR笔刷0.0.1',
    'author': 'KLinen', 'version': (0, 0, 1), 'blender': (4, 5, 0),
    'location': '3D 视图 > N 面板 > ASFR',
    'description': '局部涂抹石化、硬质化、冰霜与黄金效果，支持多角度绘制、时间路径和材质预设',
    'doc_url': 'https://space.bilibili.com/2732398',
    'category': 'Paint',
}

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy.app.handlers import persistent
import math
import os
import uuid
import json
import time
import struct
import sys
from functools import wraps
from inspect import signature
if __package__:
    from . import petrify_preview as lightweight
    from . import petrify_topology as topology
    from . import petrify_resin as resin
    from . import petrify_ice as ice
    from . import petrify_normals as normals
    from . import petrify_gold as gold
    from . import petrify_management as management
    from .petrify_overlay import OverlayPreview, ViewportOverlayState
else:
    import petrify_preview as lightweight
    import petrify_topology as topology
    import petrify_resin as resin
    import petrify_ice as ice
    import petrify_normals as normals
    import petrify_gold as gold
    import petrify_management as management
    from petrify_overlay import OverlayPreview, ViewportOverlayState
from pathlib import Path
from array import array
from mathutils import Vector
from mathutils.kdtree import KDTree
from mathutils.bvhtree import BVHTree


def full_materials_edit(function):
    """Edit source materials, then reconstruct the temporary viewport preview."""
    context_index = tuple(signature(function).parameters).index('context')

    def invoke(args, kwargs):
        context = args[context_index] if len(args) > context_index else kwargs['context']
        resume = lightweight.suspend('edit')
        try:
            return function(*args, **kwargs)
        finally:
            if resume:
                lightweight.resume(context)
    if context_index == 1:
        # Blender validates the actual positional count of RNA update callbacks.
        @wraps(function)
        def wrapped(self, context):
            return invoke((self, context), {})
    else:
        @wraps(function)
        def wrapped(context, *args, **kwargs):
            return invoke((context, *args), kwargs)
    return wrapped

UV_NAME = 'Petrify_UV'
MASK_NODE = 'Petrify Mask'
LEGACY_TIMING_MODES = [
    ('SIMULTANEOUS', '同时渐变', '所有涂过的区域一起从开始帧渐变，到结束帧一起完成', 1),
    ('SEQUENTIAL', '按绘制顺序', '按各段落笔的先后顺序推进，先画的先变化', 0),
]
TIMING_MODES = LEGACY_TIMING_MODES + [
    ('PARALLEL', '各段同时推进', '每次按下到松开左键为一段；所有段同时开始，沿各自绘制方向推进并同时完成', 2),
]
STONE_PRESETS = [
    ('DEFAULT', '默认石头', '保存的当前材质外观，可调整颜色、粗糙度和颗粒', 0),
]
STONE_PRESETS.append(('DOLL', '人形硬质化', '光滑非金属树脂，支持原色、统一颜色和比例叠加；不冻结动作', 3))
STONE_PRESETS.append(('FROST', '冰霜冻结', '原模型外叠加独立冰壳、白霜与可调冰锥；不冻结骨骼动作', 4))
STONE_PRESETS.append((gold.KEY, '黄金化', '可调金色、金属度、镜面或磨砂粗糙度及微纹理；沿已绘制范围生效', 5))
PRESET_GROUPS = {'DEFAULT': 'PP Preset Default', 'STONE001': 'PP Preset STONE001', 'ROUGH': 'PP Preset ROUGH', 'DOLL': 'PP Preset Glossy Doll', 'FROST': 'PP Preset Clear Frost', gold.KEY: 'PP Preset Golden Statue'}
if EDITION == 'PUBLIC':
    STONE_PRESETS = [x for x in STONE_PRESETS if x[0] not in {'STONE001', 'ROUGH'}]
_custom_presets = {}
_preset_items = list(STONE_PRESETS)
# Blender's dynamic enums retain string pointers, so keep prior item strings alive.
_preset_item_history = [_preset_items]
_preset_library_errors = []
_deleted_presets = {}
DEFAULT_CONTROLS = {
    'color': ('Stone Color', 2, 'Stone Color', 'NodeSocketColor'),
    'roughness': ('Stone Surface', 'Roughness', 'Roughness', 'NodeSocketFloat'),
    'bump': ('Stone Bump', 'Strength', 'Bump Strength', 'NodeSocketFloat'),
    'scale': ('Stone Grain', 'Scale', 'Grain Scale', 'NodeSocketFloat'),
    'fine_scale': ('Fine Grain', 'Scale', 'Fine Scale', 'NodeSocketFloat'),
    'bump_distance': ('Stone Bump', 'Distance', 'Bump Distance', 'NodeSocketFloat'),
}


def custom_preset_directory():
    return Path(bpy.utils.user_resource('DATAFILES', path='petrify_painter/presets'))


def valid_custom_id(key):
    return isinstance(key, str) and key.startswith('USER_') and len(key) == 37 and all(c in '0123456789abcdef' for c in key[5:])


def preset_number(key):
    # Enum menu buttons round-trip through a float in Blender's UI. All integers
    # below 2**24 are exact; old 28-bit IDs could turn into an invalid selection.
    return int(key[5:12], 16) % (2**24 - 1000) + 1000


def migrate_preset_selection(settings, items):
    stored = settings.get('stone_preset', 0)
    if settings.get('preset_enum_schema', 0) < 2:
        if stored not in {item[3] for item in STONE_PRESETS}:
            matches = []
            for key, _, _, number in items:
                if not valid_custom_id(key):
                    continue
                legacy = int(key[5:12], 16) + 1000
                rounded = int(struct.unpack('f', struct.pack('f', legacy))[0])
                if stored in (legacy, rounded):
                    matches.append(number)
            # Ambiguous/missing legacy entries must never select another asset.
            settings['stone_preset'] = matches[0] if len(matches) == 1 else 0
        settings['preset_enum_schema'] = 2
    if settings.get('stone_preset', 0) not in {item[3] for item in items}:
        settings['stone_preset'] = 0


def refresh_presets():
    global _custom_presets, _preset_items, _preset_library_errors, _deleted_presets
    records, errors, deleted = {}, [], {}
    directory = custom_preset_directory()
    try:
        for path in (directory / '.deleted').glob('USER_*/entry.json'):
            try:
                meta = json.loads(path.read_text(encoding='utf8'))
                key = path.parent.name
                if valid_custom_id(key) and meta['id'] == key:
                    deleted[key] = meta
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(path.name + '：' + str(exc))
        files = sorted(directory.glob('*.json')) if directory.is_dir() else []
        for path in files:
            try:
                meta = json.loads(path.read_text(encoding='utf8'))
                key = meta['id']
                if not valid_custom_id(key) or path.stem != key or not isinstance(meta['name'], str) or not meta['name'].strip():
                    raise ValueError('无效的预设信息')
                if key in deleted:
                    continue
                if not (directory / (key + '.blend')).is_file():
                    raise ValueError('缺少预设 blend 文件')
                records[key] = {'name': meta['name'], 'file': str(directory / (key + '.blend')),
                                'group': meta.get('group', 'PP Custom ' + key[5:])}
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(path.name + '：' + str(exc))
    except OSError as exc:
        errors.append(str(exc))
    # Applied custom presets travel with the project, including on another PC.
    for tree in getattr(bpy.data, 'node_groups', ()):
        key = tree.get('petrify_preset')
        if valid_custom_id(key) and key not in deleted and tree.get('petrify_preset_revision') == 'CUSTOM1' and any(getattr(sc, 'petrify_settings', None) and sc.petrify_settings.show_scene_presets for sc in getattr(bpy.data, 'scenes', ())):
            records.setdefault(key, {'name': str(tree.get('petrify_preset_label', tree.name)), 'file': ''})
    numbers, usable = set(), {}
    items = list(STONE_PRESETS)
    for key, meta in sorted(records.items(), key=lambda pair: (pair[1]['name'].casefold(), pair[0])):
        number = preset_number(key)
        if number in numbers:
            errors.append(meta['name'] + '：预设编号冲突')
            continue
        numbers.add(number); usable[key] = meta
        items.append((key, meta['name'], '自定义预设 · ' + ('已保存到本机预设库' if meta['file'] else '随当前工程保存'), number))
    _custom_presets, _preset_library_errors, _deleted_presets = usable, errors, deleted
    if items != _preset_items:
        _preset_items = items
        _preset_item_history.append(items)
    # Deletion/undo may leave an enum's stored integer outside the available items.
    for scene in getattr(bpy.data, 'scenes', ()):
        if hasattr(scene, 'petrify_settings'):
            migrate_preset_selection(scene.petrify_settings, items)


def delete_custom_preset(key):
    # Guard in the core function as well as the UI: built-ins can never be deleted.
    if key in PRESET_GROUPS or not valid_custom_id(key):
        raise RuntimeError('内置预设受保护，不能删除')
    refresh_presets()
    if key not in _custom_presets:
        raise RuntimeError('此自定义预设已删除或不可用')
    name = preset_label(key)
    directory = custom_preset_directory()
    archive = directory / '.deleted' / key
    archive.mkdir(parents=True, exist_ok=True)
    moved, created = [], []
    marker = archive / 'entry.json'
    temporary = archive / 'entry.tmp'
    try:
        for suffix in ('.blend', '.json'):
            source, destination = directory / (key + suffix), archive / ('preset' + suffix)
            if source.exists():
                if destination.exists():
                    raise RuntimeError('回收区已有同名文件，请检查预设目录')
                os.replace(source, destination); moved.append((source, destination))
        if not (archive / 'preset.blend').exists():
            template = stone_template(key)
            path = archive / 'preset.blend'; created.append(path)
            bpy.data.libraries.write(str(path), {template}, path_remap='ABSOLUTE', fake_user=True, compress=True)
        else:
            template = None
        if not (archive / 'preset.json').exists():
            path = archive / 'preset.json'; created.append(path)
            meta = {'id': key, 'name': name, 'schema': 1}
            if template:
                meta['group'] = template.name
            path.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf8')
        temporary.write_text(json.dumps({'id': key, 'name': name, 'deleted_at': time.time_ns()}, ensure_ascii=False), encoding='utf8')
        os.replace(temporary, marker)
    except Exception:
        for source, destination in reversed(moved):
            os.replace(destination, source)
        for path in created:
            if path.exists():
                path.unlink()
        if temporary.exists():
            temporary.unlink()
        raise
    refresh_presets()
    return name


def restore_deleted_preset(key):
    if not valid_custom_id(key):
        raise RuntimeError('无效的自定义预设')
    refresh_presets()
    if key not in _deleted_presets:
        raise RuntimeError('回收区中没有这个预设')
    directory = custom_preset_directory(); archive = directory / '.deleted' / key
    moved = []
    try:
        for suffix in ('.blend', '.json'):
            source, destination = archive / ('preset' + suffix), directory / (key + suffix)
            if source.exists():
                if destination.exists():
                    raise RuntimeError('预设库已有同名文件，恢复不会覆盖它')
                os.replace(source, destination); moved.append((source, destination))
        os.replace(archive / 'entry.json', archive / 'entry.restored.json')
    except Exception:
        for source, destination in reversed(moved):
            os.replace(destination, source)
        raise
    refresh_presets()
    return key


def preset_items(self, context):
    return _preset_items


def scene_presets_changed(self, context):
    refresh_presets()


def preset_selection_changed(self, context):
    self['preset_enum_schema'] = 2


def preset_label(key, tree=None):
    return next((item[1] for item in _preset_items if item[0] == key),
                str(tree.get('petrify_preset_label', '自定义预设')) if tree else '自定义预设')


@persistent
def refresh_presets_after_load(_):
    refresh_presets()


def refresh_presets_deferred():
    refresh_presets()
    return None

# Store paths, never RNA wrappers: undo can replace datablocks underneath Python.
_source_paths = {}


def source_images(objects):
    seen_trees, seen_images = set(), set()
    def walk(tree):
        if not tree or tree.as_pointer() in seen_trees:
            return
        seen_trees.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type == 'GROUP':
                yield from walk(node.node_tree)
            elif node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT'} and node.image:
                image = node.image
                if image.session_uid not in seen_images and not image.get('petrify_mask'):
                    seen_images.add(image.session_uid)
                    yield image
    for ob in objects:
        if ob.type == 'MESH':
            for material in ob.data.materials:
                if material and material.use_nodes:
                    yield from walk(material.node_tree)


def protect_source_images(objects):
    """Keep the original appearance portable without touching paint canvases."""
    for image in source_images(objects):
        if image.source != 'FILE' or image.library or image.is_dirty:
            continue
        path = os.path.normpath(bpy.path.abspath(image.filepath))
        if os.path.isfile(path):
            _source_paths[image.session_uid] = path
        else:
            path = _source_paths.get(image.session_uid, '')
            if not os.path.isfile(path):
                continue
            # Save As followed by Undo may restore paths relative to the old file.
            image.filepath_raw = path
            if not image.packed_file:
                image.reload()
        if not image.packed_file:
            try:
                image.pack()
            except RuntimeError as exc:
                print('Petrify: cannot pack source texture', image.name, str(exc))


@persistent
def remember_source_paths(_):
    for image in source_images(ob for ob in bpy.data.objects if ob.get('petrify_ready')):
        if image.source == 'FILE' and not image.library and not image.is_dirty:
            path = os.path.normpath(bpy.path.abspath(image.filepath))
            if os.path.isfile(path):
                _source_paths[image.session_uid] = path


@persistent
def restore_source_images_after_undo(_):
    protect_source_images(ob for ob in bpy.data.objects if ob.get('petrify_ready'))


@persistent
def clear_source_paths(_):
    _source_paths.clear()


def mask_for(ob):
    return bpy.data.images.get(ob.get('petrify_image', '')) if ob else None


def stone_for(ob):
    return bpy.data.node_groups.get(ob.get('petrify_stone', '')) if ob else None


def target(context):
    return context.scene.petrify_settings.target or context.active_object


def target_meshes(context):
    root = target(context)
    if not root:
        return []
    candidates = [root]
    if context.scene.petrify_settings.include_children:
        candidates += list(root.children_recursive)
    return [ob for ob in candidates if not ob.get(ice.TAG) and ob.type == 'MESH' and ob.data.polygons
            and ob.name in context.view_layer.objects
            and getattr(ob, 'mmd_type', 'NONE') not in {'RIGID_BODY', 'JOINT'}
            and ob.visible_get(view_layer=context.view_layer) and not ob.hide_render]


def activate_object(context, ob):
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for item in context.selected_objects:
        item.select_set(False)
    ob.hide_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob


def new_node(tree, node_type, name, location):
    node = tree.nodes.new(node_type)
    node.name = name
    node.label = name
    node.location = location
    return node


def stone_template(preset):
    # Resolve datablocks afresh after undo; never cache RNA wrappers in Python.
    if preset == resin.KEY:
        return resin.template()
    if preset == ice.KEY:
        return ice.template()
    if preset == gold.KEY:
        return gold.template()
    if valid_custom_id(preset):
        if preset in _deleted_presets:
            raise RuntimeError('此预设已从库中删除；模型上已有的效果仍保留')
        for tree in bpy.data.node_groups:
            if tree.get('petrify_preset_template') == preset:
                return tree
        meta = _custom_presets.get(preset, {})
        path = meta.get('file', '')
        if not path or not os.path.isfile(path):
            for tree in bpy.data.node_groups:
                if tree.get('petrify_preset') == preset and tree.get('petrify_preset_revision') == 'CUSTOM1':
                    if tree.get('petrify_controller'):
                        return tree.nodes['Preset Material'].node_tree
                    # A legacy project may contain only an applied copy. Preserve it
                    # as a shared asset before rebuilding its per-model controller.
                    asset = tree.copy()
                    asset['petrify_preset_template'] = preset
                    if 'petrify_owner' in asset:
                        del asset['petrify_owner']
                    asset.use_fake_user = True
                    return asset
            raise RuntimeError('找不到此自定义预设，请刷新列表并检查预设目录')
        name = meta.get('group', 'PP Custom ' + preset[5:])
        with bpy.data.libraries.load(path, link=False) as (source, destination):
            if name not in source.node_groups:
                raise RuntimeError('自定义预设文件不完整：' + meta.get('name', preset))
            destination.node_groups = [name]
        return destination.node_groups[0]
    for tree in bpy.data.node_groups:
        if tree.get('petrify_preset_template') == preset and tree.get('petrify_preset_revision') == '2.0.6':
            return tree
    if EDITION == 'PUBLIC' and preset in {'STONE001', 'ROUGH'}:
        raise RuntimeError('此材质未随配布版提供；工程中已有的材质效果保持不变')
    path = os.path.join(os.path.dirname(__file__), 'petrify_presets.blend')
    if not os.path.isfile(path):
        raise RuntimeError('找不到石头预设库，请重新安装完整的石化笔刷 ZIP 安装包')
    name = PRESET_GROUPS[preset]
    with bpy.data.libraries.load(path, link=False) as (source, destination):
        if name not in source.node_groups:
            raise RuntimeError('石头预设库不完整，请重新安装完整插件')
        destination.node_groups = [name]
    return destination.node_groups[0]


def create_stone(ob, preset='DEFAULT'):
    tree = bpy.data.node_groups.new(ob.name + ' · 石化外观', 'ShaderNodeTree')
    tree.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    alpha = tree.interface.new_socket(name='Alpha', in_out='INPUT', socket_type='NodeSocketFloat')
    alpha.default_value = 1; alpha.min_value = 0; alpha.max_value = 1
    configure_stone(tree, preset)
    tree.use_fake_user = False
    tree['petrify_owner'] = ob.name
    ob['petrify_stone'] = tree.name
    return tree


def default_control(tree, name):
    node_name, socket_name, interface_name, _ = DEFAULT_CONTROLS[name]
    if tree.get('petrify_controller'):
        return tree.nodes['Preset Material'].inputs[interface_name]
    return tree.nodes[node_name].inputs[socket_name]


def expose_default_controls(template):
    if template.get('petrify_default_inputs'):
        return
    inp = next((n for n in template.nodes if n.type == 'GROUP_INPUT'), None)
    if inp is None:
        inp = new_node(template, 'NodeGroupInput', 'Preset Inputs', (-700, 0))
    for name, (node_name, socket_name, interface_name, socket_type) in DEFAULT_CONTROLS.items():
        target_socket = template.nodes[node_name].inputs[socket_name]
        socket = template.interface.new_socket(name=interface_name, in_out='INPUT', socket_type=socket_type)
        socket.default_value = target_socket.default_value
        if name in {'roughness', 'bump'}:
            socket.min_value = 0; socket.max_value = 1
        template.links.new(inp.outputs[interface_name], target_socket)
    template['petrify_default_inputs'] = True


def configure_stone(tree, preset):
    management.remember(tree)
    template = stone_template(preset)
    if preset == 'DEFAULT':
        expose_default_controls(template)
    if tree == template:
        raise RuntimeError('不能将预设库本身作为模型石化组')
    if any(s.item_type == 'SOCKET' and s.in_out == 'INPUT' and s.name == resin.ORIGINAL for s in template.interface.items_tree):
        resin.expose_original(tree)
    if preset == ice.KEY:
        ice.expose_surface(tree)
    # The public Shader/Alpha interface is untouched, so material links and
    # per-material alpha values survive every switch, with a stable datablock ID.
    tree.nodes.clear()
    inp = new_node(tree, 'NodeGroupInput', 'Model Alpha', (-350, -120))
    material = new_node(tree, 'ShaderNodeGroup', 'Preset Material', (-100, 80))
    material.node_tree = template
    out = new_node(tree, 'NodeGroupOutput', 'Stone Output', (180, 80))
    tree.links.new(inp.outputs['Alpha'], material.inputs['Alpha'])
    if resin.ORIGINAL in material.inputs:
        tree.links.new(inp.outputs[resin.ORIGINAL], material.inputs[resin.ORIGINAL])
    if ice.SURFACE in material.inputs:
        tree.links.new(inp.outputs[ice.SURFACE], material.inputs[ice.SURFACE])
    tree.links.new(material.outputs['Shader'], out.inputs['Shader'])
    for key in ('petrify_preset_template', 'petrify_default_inputs', 'petrify_resin_color_mode', 'petrify_resin_blend'):
        if key in tree:
            del tree[key]
    tree['petrify_controller'] = True
    tree['petrify_preset'] = preset
    tree['petrify_preset_label'] = preset_label(preset)
    tree['petrify_preset_revision'] = 'CUSTOM1' if valid_custom_id(preset) else '3.4.0' if preset == ice.KEY else resin.REVISION if preset == resin.KEY else gold.REVISION if preset == gold.KEY else '2.0.6'
    normals.ensure_controller(tree)
    management.restore(tree, preset)


def cleanup_unused_stone_groups():
    # Restrict cleanup to orphaned plugin-owned roots and their dependencies.
    # Never purge arbitrary scene data or fake-user assets the user chose to keep.
    protected = {ob.get('petrify_stone', '') for ob in bpy.data.objects}
    roots = [g for g in bpy.data.node_groups if g.get('petrify_owner') and
             not g.library and not g.use_fake_user and g.users == 0 and g.name not in protected]
    groups, images = set(), set()
    def visit(tree):
        if tree.name in groups or tree.library or tree.use_fake_user:
            return
        groups.add(tree.name)
        for node in tree.nodes:
            if node.type == 'GROUP' and node.node_tree:
                visit(node.node_tree)
            elif node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT'} and node.image:
                images.add(node.image.name)
    for tree in roots:
        visit(tree)
    removed = 0
    while True:
        unused = [name for name in groups if (g := bpy.data.node_groups.get(name)) and
                  not g.library and not g.use_fake_user and g.users == 0 and name not in protected]
        if not unused:
            break
        for name in unused:
            bpy.data.node_groups.remove(bpy.data.node_groups[name]); groups.remove(name); removed += 1
    for name in images:
        image = bpy.data.images.get(name)
        if image and not image.library and not image.use_fake_user and image.users == 0:
            bpy.data.images.remove(image)
    return removed


def clone_shader_tree(source, created, copy_images=True, strip_metadata=True):
    """Copy nested groups and textures without editing the user's source assets."""
    trees, images = {}, {}
    def clone(tree):
        key = tree.as_pointer()
        if key in trees:
            return trees[key]
        if tree.animation_data and (tree.animation_data.action or tree.animation_data.drivers):
            raise RuntimeError('节点组含动画或驱动，请先制作不依赖动画的材质组：' + tree.name)
        result = tree.copy(); created.append(result); trees[key] = result
        result.use_fake_user = False
        if strip_metadata:
            for prop in list(result.keys()):
                if prop.startswith('petrify_') and prop not in {'petrify_normal_blend', 'petrify_normal_instrumented'}:
                    del result[prop]
        for node in result.nodes:
            for prop in node.bl_rna.properties:
                if prop.type == 'POINTER' and isinstance(getattr(node, prop.identifier, None), (bpy.types.Object, bpy.types.Collection)):
                    raise RuntimeError('节点依赖场景物体，请改用 UV 或生成坐标后保存：' + node.name)
            if node.type == 'SCRIPT' and node.mode == 'EXTERNAL':
                raise RuntimeError('请将外部 OSL 脚本转为内部文本后保存')
            if node.type == 'GROUP' and node.node_tree:
                node.node_tree = clone(node.node_tree)
            if copy_images and node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT'} and node.image:
                original = node.image
                image_key = original.as_pointer()
                if image_key not in images:
                    if original.source not in {'FILE', 'GENERATED', 'TILED'}:
                        raise RuntimeError('暂不支持序列或视频贴图：' + original.name)
                    absolute = bpy.path.abspath(original.filepath, library=original.library)
                    # Touch pixels to load lazy file textures before copying.
                    if not original.has_data and len(original.pixels) == 0:
                        raise RuntimeError('贴图无法读取：' + original.name)
                    if original.is_dirty:
                        if original.source == 'TILED':
                            raise RuntimeError('UDIM 贴图有未保存的修改，请先保存各个图块：' + original.name)
                        # Image.copy() reads the saved file, not unsaved pixel edits.
                        copied = bpy.data.images.new(original.name + ' · 预设',
                            width=original.size[0], height=original.size[1], alpha=original.channels == 4,
                            float_buffer=original.is_float)
                        created.append(copied)
                        copied.colorspace_settings.name = original.colorspace_settings.name
                        copied.alpha_mode = original.alpha_mode
                        pixels = array('f', [0]) * len(original.pixels)
                        original.pixels.foreach_get(pixels); copied.pixels.foreach_set(pixels)
                        copied.pack()
                    else:
                        copied = original.copy(); created.append(copied)
                        if original.filepath:
                            copied.filepath_raw = absolute
                        if not (copied.packed_file or len(copied.packed_files)):
                            copied.pack()
                    copied.use_fake_user = False
                    if not (copied.packed_file or len(copied.packed_files)):
                        raise RuntimeError('贴图无法打包：' + original.name)
                    images[image_key] = copied
                node.image = images[image_key]
        return result
    return clone(source)


def shader_outputs(group):
    if not group or group.bl_idname != 'ShaderNodeTree':
        return []
    return [socket for socket in group.interface.items_tree
            if socket.item_type == 'SOCKET' and socket.in_out == 'OUTPUT' and socket.socket_type == 'NodeSocketShader']


def save_custom_preset(source, name, output_name='', input_values=None):
    if source.get('petrify_preset') == ice.KEY:
        raise RuntimeError('冰层包含独立几何，不能仅存为材质预设；请保存 blend 工程以保留全部参数')
    name = name.strip()
    if not name:
        raise RuntimeError('请填写新预设名称')
    outputs = shader_outputs(source)
    if output_name:
        outputs = [socket for socket in outputs if socket.name == output_name]
    if len(outputs) != 1:
        raise RuntimeError('请选择带着色器输出的节点组；有多个输出时请填写唯一输出名称')
    output = outputs[0]
    active = next((node for node in source.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output), None)
    socket = next((socket for socket in active.inputs if socket.identifier == output.identifier), None) if active else None
    if not socket or not socket.is_linked:
        raise RuntimeError('该节点组的着色器输出尚未连接')
    output_identifier = output.identifier
    refresh_presets()
    existing = {item[1].casefold() for item in _preset_items}
    original_name, suffix = name, 2
    while name.casefold() in existing:
        name = original_name + ' (' + str(suffix) + ')'; suffix += 1
    directory = custom_preset_directory()
    directory.mkdir(parents=True, exist_ok=True)
    used_numbers = {item[3] for item in _preset_items}
    while True:
        key = 'USER_' + uuid.uuid4().hex
        if preset_number(key) not in used_numbers and not (directory / (key + '.blend')).exists():
            break
    blend_path = directory / (key + '.blend')
    meta_path = directory / (key + '.json')
    blend_tmp = directory / (key + '.tmp.blend')
    meta_tmp = directory / (key + '.tmp')
    created = []
    try:
        graph = clone_shader_tree(source, created)
        template = bpy.data.node_groups.new('PP Custom ' + key[5:], 'ShaderNodeTree')
        created.append(template)
        template.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
        alpha = template.interface.new_socket(name='Alpha', in_out='INPUT', socket_type='NodeSocketFloat')
        alpha.default_value = 1; alpha.min_value = 0; alpha.max_value = 1
        inp = new_node(template, 'NodeGroupInput', 'Model Alpha', (-420, -160))
        material = new_node(template, 'ShaderNodeGroup', 'Preset Material', (-420, 160))
        material.node_tree = graph
        for sock in material.inputs:
            if input_values and sock.identifier in input_values and hasattr(sock, 'default_value'):
                sock.default_value = input_values[sock.identifier]
        if resin.ORIGINAL in material.inputs:
            resin.expose_original(template)
            template.links.new(inp.outputs[resin.ORIGINAL], material.inputs[resin.ORIGINAL])
        transparent = new_node(template, 'ShaderNodeBsdfTransparent', 'Transparent', (-420, -320))
        mix = new_node(template, 'ShaderNodeMixShader', 'Preserve Model Alpha', (-100, 160))
        out = new_node(template, 'NodeGroupOutput', 'Preset Output', (160, 160))
        selected = next(sock for sock in material.outputs if sock.identifier == output_identifier)
        template.links.new(inp.outputs['Alpha'], mix.inputs[0])
        template.links.new(transparent.outputs[0], mix.inputs[1])
        template.links.new(selected, mix.inputs[2]); template.links.new(mix.outputs[0], out.inputs['Shader'])
        template['petrify_preset_template'] = key
        template['petrify_preset'] = key
        template['petrify_preset_label'] = name
        template['petrify_preset_revision'] = 'CUSTOM1'
        template.use_fake_user = True
        bpy.data.libraries.write(str(blend_tmp), {template}, path_remap='ABSOLUTE', fake_user=True, compress=True)
        meta_tmp.write_text(json.dumps({'id': key, 'name': name, 'schema': 1}, ensure_ascii=False, indent=2), encoding='utf8')
        # Publish metadata last: incomplete writes never enter the preset menu.
        os.replace(blend_tmp, blend_path)
        os.replace(meta_tmp, meta_path)
    except Exception:
        for path in (blend_tmp, meta_tmp, blend_path):
            if path.exists():
                path.unlink()
        # Remove all references together; never access RNA wrappers after removal.
        if created:
            bpy.data.batch_remove(ids=tuple(created))
        raise
    refresh_presets()
    return key


def replace_stone(ob, preset, reset=False):
    tree = stone_for(ob)
    current = tree.get('petrify_preset', 'DEFAULT') if tree else None
    management.remember(tree)
    management.switch_object_memory(ob, current, preset, sys.modules[__name__], reset)
    if reset and tree:
        # Prevent configure from immediately memorizing the values being reset.
        tree['petrify_controller'] = False
    # ensure_volume isolates linked duplicates before this function runs.
    if tree and not tree.library and not tree.get('petrify_preset_template') and tree.get('petrify_owner') == ob.name:
        configure_stone(tree, preset)
    else:
        tree = create_stone(ob, preset)
    for mat in ob.data.materials:
        if not mat or not mat.use_nodes:
            continue
        nodes = mat.node_tree
        rock = nodes.nodes.get('Petrify Stone')
        if not rock:
            continue
        if rock.node_tree == tree:
            resin.bind_original(mat, rock)
            ice.bind_surface(mat, rock)
            continue
        # Node-tree assignment may recreate sockets: retain only sockets on other nodes.
        alpha = rock.inputs.get('Alpha')
        value = alpha.default_value if alpha else 1.0
        source = alpha.links[0].from_socket if alpha and alpha.is_linked else None
        outputs = [link.to_socket for link in rock.outputs[0].links]
        rock.node_tree = tree
        rock.inputs['Alpha'].default_value = value
        resin.bind_original(mat, rock)
        ice.bind_surface(mat, rock)
        if source:
            nodes.links.new(source, rock.inputs['Alpha'])
        for output in outputs:
            nodes.links.new(rock.outputs['Shader'], output)
    normals.ensure(ob, tree, force=True)
    ice.refresh(ob, sys.modules[__name__])
    normals.cleanup_extractors()
    return tree


def wrap_material(original, image, stone):
    mat = original.copy()
    mat.name = original.name + ' · 石化'
    original.use_fake_user = True
    mat['petrify_original'] = original.name
    mat.use_nodes = True
    tree = mat.node_tree
    outputs = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.inputs['Surface'].is_linked]
    if not outputs:
        raise RuntimeError('材质没有连接表面输出：' + original.name)
    uv = new_node(tree, 'ShaderNodeUVMap', '石化专用 UV', (100, -750))
    uv.uv_map = UV_NAME
    mask = new_node(tree, 'ShaderNodeTexImage', MASK_NODE, (330, -650))
    mask.label = '只在这张蒙版上绘制：白色石化 / 黑色恢复'
    mask.image = image
    mask.interpolation = 'Linear'
    mask.extension = 'EXTEND'
    tree.links.new(uv.outputs[0], mask.inputs[0])
    strength = new_node(tree, 'ShaderNodeMath', 'Petrify Strength', (640, -560))
    strength.operation = 'MULTIPLY'
    strength.use_clamp = True
    strength.inputs[1].default_value = 1.0
    tree.links.new(mask.outputs['Color'], strength.inputs[0])
    rock = new_node(tree, 'ShaderNodeGroup', 'Petrify Stone', (650, -300))
    rock.node_tree = stone
    resin.bind_original(mat, rock)
    mmd = tree.nodes.get('mmd_shader')
    if mmd and 'Alpha' in mmd.outputs:
        tree.links.new(mmd.outputs['Alpha'], rock.inputs['Alpha'])
    else:
        # Preserve alpha for the ordinary Principled case, too.
        source = outputs[0].inputs['Surface'].links[0].from_node
        if source.type == 'BSDF_PRINCIPLED':
            sock = source.inputs['Alpha']
            if sock.is_linked:
                tree.links.new(sock.links[0].from_socket, rock.inputs['Alpha'])
            else:
                rock.inputs['Alpha'].default_value = sock.default_value
    for out in outputs:
        source = out.inputs['Surface'].links[0].from_socket
        mix = new_node(tree, 'ShaderNodeMixShader', 'Petrify Mix · ' + out.name, (950, out.location.y))
        tree.links.new(strength.outputs[0], mix.inputs[0])
        tree.links.new(source, mix.inputs[1])
        tree.links.new(rock.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], out.inputs['Surface'])
        out.location.x = 1210
    ice.bind_surface(mat, rock)
    normals.bind(mat, rock)
    for n in tree.nodes:
        n.select = False
    mask.select = True
    tree.nodes.active = mask
    return mat


def prepare_object(context, ob, resolution=4096):
    protect_source_images([ob])
    if mask_for(ob):
        return mask_for(ob)
    if ob.type != 'MESH' or not ob.data.polygons:
        raise RuntimeError('请先选择角色网格')
    if ob.library or ob.data.library:
        raise RuntimeError('请先将链接的模型设为本地数据')
    activate_object(context, ob)
    if ob.data.users > 1:
        ob.data = ob.data.copy()
    if len(ob.data.uv_layers) >= 8 and UV_NAME not in ob.data.uv_layers:
        raise RuntimeError('UV 层已满，无法添加独立蒙版 UV')
    originals = list(ob.data.materials)
    for mat in originals:
        if mat and mat.use_nodes and not any(n.type == 'OUTPUT_MATERIAL' and n.inputs['Surface'].is_linked for n in mat.node_tree.nodes):
            raise RuntimeError('材质没有表面输出：' + mat.name)
    old_render = next((u.name for u in ob.data.uv_layers if u.active_render), '')
    ob['petrify_original_uv'] = old_render
    uv = ob.data.uv_layers.get(UV_NAME) or ob.data.uv_layers.new(name=UV_NAME)
    ob.data.uv_layers.active = uv
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    try:
        bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.004, area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        if old_render:
            ob.data.uv_layers[old_render].active_render = True
    image = bpy.data.images.new(ob.name + ' · 石化蒙版', width=resolution, height=resolution, alpha=False)
    image.generated_color = (0, 0, 0, 1)
    image.colorspace_settings.name = 'Non-Color'
    image.use_fake_user = True
    image['petrify_mask'] = True
    ob['petrify_image'] = image.name
    stone = create_stone(ob)
    copies = {}
    if not originals:
        mat = bpy.data.materials.new('原始材质')
        mat.use_nodes = True
        ob.data.materials.append(mat)
        originals = [mat]
    for idx, mat in enumerate(originals):
        if mat is None:
            mat = bpy.data.materials.new('原始材质')
            mat.use_nodes = True
        if mat.name not in copies:
            copies[mat.name] = wrap_material(mat, image, stone)
        ob.material_slots[idx].material = copies[mat.name]
    ob['petrify_ready'] = True
    normals.ensure(ob, stone)
    image.pack()
    context.scene.petrify_settings.target = ob
    return image


STATIC_ATTR = 'Petrify_Volume'
DEFORM_MODIFIERS = {'ARMATURE', 'CAST', 'CORRECTIVE_SMOOTH', 'CURVE', 'DISPLACE',
    'HOOK', 'LAPLACIANDEFORM', 'LAPLACIANSMOOTH', 'LATTICE', 'MESH_DEFORM',
    'SHRINKWRAP', 'SIMPLE_DEFORM', 'SMOOTH', 'SURFACE_DEFORM', 'WARP', 'WAVE',
    'VERTEX_WEIGHT_EDIT', 'VERTEX_WEIGHT_MIX', 'VERTEX_WEIGHT_PROXIMITY',
    'NORMAL_EDIT', 'WEIGHTED_NORMAL', 'UV_WARP', 'UV_PROJECT', 'DATA_TRANSFER'}


def float_attribute(ob, name):
    mesh = topology.data_mesh(ob)
    attr = mesh.attributes.get(name)
    if attr and (attr.data_type != 'FLOAT' or attr.domain != 'POINT'):
        raise RuntimeError('属性名称冲突：' + name)
    return attr or mesh.attributes.new(name, 'FLOAT', 'POINT')


def read_attribute(ob, name):
    mesh = topology.data_mesh(ob)
    values = array('f', [0]) * len(mesh.vertices)
    attr = mesh.attributes.get(name)
    if attr:
        attr.data.foreach_get('value', values)
    return values


def write_attribute(ob, name, values):
    if len(values) != len(topology.data_mesh(ob).vertices):
        raise RuntimeError(ob.name + '：石化数据与当前网格顶点数量不一致')
    float_attribute(ob, name).data.foreach_set('value', values)
    topology.data_mesh(ob).update()


def math_node(tree, name, operation, a=0.0, b=0.0):
    n = new_node(tree, 'ShaderNodeMath', name, (0, 0))
    n.operation = operation
    for i, value in enumerate((a, b)):
        if isinstance(value, (int, float)):
            n.inputs[i].default_value = value
        else:
            tree.links.new(value, n.inputs[i])
    return n.outputs[0]


def ensure_volume(ob):
    # Each object owns its mesh, materials and stone group, even for linked duplicates.
    if ob.data.users > 1:
        ob.data = ob.data.copy()
    materials = {}
    for slot in ob.material_slots:
        mat = slot.material
        if mat and mat.users > 1:
            if mat.name not in materials:
                materials[mat.name] = mat.copy()
            slot.material = materials[mat.name]
    stone = stone_for(ob)
    if stone and stone.get('petrify_owner') != ob.name:
        stone = stone.copy()
        stone['petrify_owner'] = ob.name
        ob['petrify_stone'] = stone.name
        for mat in ob.data.materials:
            if mat and mat.use_nodes and (n := mat.node_tree.nodes.get('Petrify Stone')):
                n.node_tree = stone
    float_attribute(ob, STATIC_ATTR)
    for mat in ob.data.materials:
        if not mat or not mat.use_nodes:
            continue
        t = mat.node_tree
        if 'Petrify Volume Base' in t.nodes:
            continue
        strength = t.nodes.get('Petrify Strength')
        if not strength:
            continue
        attr = new_node(t, 'ShaderNodeAttribute', 'Petrify Volume Base', (350, -1000))
        attr.attribute_name = STATIC_ATTR
        original = strength.inputs[0].links[0].from_socket
        union = math_node(t, 'Petrify Volume Union', 'MAXIMUM', original, attr.outputs['Fac'])
        t.links.new(union, strength.inputs[0])
    ob['petrify_version'] = 2


def set_stroke_timing(tree, mode):
    """Reuse legacy sequential graphs; add the shared fade only when requested."""
    progress = tree.nodes['Progress'].outputs['Fac']
    if mode == 'PARALLEL':
        if not tree.get('segment_attr'):
            raise RuntimeError('旧路径没有记录分段，请使用新模式重新绘制')
        progress = tree.nodes['Segment Progress'].outputs['Fac']
    tree.links.new(progress, tree.nodes['Normalized progress'].inputs[0])
    timed = tree.nodes['Time mask'].outputs[0]
    if mode == 'SIMULTANEOUS':
        together = tree.nodes.get('Together time mask')
        if together is None:
            span = tree.nodes['Frame span'].outputs[0]
            elapsed = math_node(tree, 'Together elapsed', 'SUBTRACT',
                                tree.nodes['Frame'].outputs[0], tree.nodes['Start'].outputs[0])
            safe_span = math_node(tree, 'Together safe span', 'MAXIMUM', span, 0.001)
            ramp = math_node(tree, 'Together fraction', 'DIVIDE', elapsed, safe_span)
            ramp.node.use_clamp = True
            # A zero-length interval becomes an instantaneous change at Start.
            instant = math_node(tree, 'Together instant', 'LESS_THAN', span, 0.001)
            visible = math_node(tree, 'Together visible', 'MAXIMUM', ramp, instant)
            together = math_node(tree, 'Together time mask', 'MULTIPLY', visible,
                                 tree.nodes['After start'].outputs[0]).node
        timed = together.outputs[0]
    tree.links.new(timed, tree.nodes['Weighted mask'].inputs[0])
    tree['timing_mode'] = mode


def make_stroke_group(name, start, end, fade, timing_mode='SEQUENTIAL'):
    uid = uuid.uuid4().hex[:12]
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    tree['petrify_stroke'] = True
    tree['weight_attr'] = 'Petrify_W_' + uid
    tree['progress_attr'] = 'Petrify_P_' + uid
    tree['segment_attr'] = 'Petrify_S_' + uid
    tree.interface.new_socket(name='Mask', in_out='OUTPUT', socket_type='NodeSocketFloat')
    nodes = {}
    for key, val in [('Start', start), ('End', end), ('Fade', fade), ('Frame', start)]:
        n = new_node(tree, 'ShaderNodeValue', key, (-800, 0))
        n.outputs[0].default_value = val
        nodes[key] = n.outputs[0]
    curve = tree.nodes['Frame'].outputs[0].driver_add('default_value')
    curve.driver.expression = 'frame'
    for key, attr_name in [('Weight', tree['weight_attr']), ('Progress', tree['progress_attr']),
                           ('Segment Progress', tree['segment_attr'])]:
        n = new_node(tree, 'ShaderNodeAttribute', key, (-900, -300))
        n.attribute_name = attr_name
        nodes[key] = n.outputs['Fac']
    progress = math_node(tree, 'Normalized progress', 'DIVIDE', nodes['Progress'], nodes['Weight'])
    span = math_node(tree, 'Frame span', 'SUBTRACT', nodes['End'], nodes['Start'])
    offset = math_node(tree, 'Path time', 'MULTIPLY', progress, span)
    arrival = math_node(tree, 'Arrival', 'ADD', offset, nodes['Start'])
    distance = math_node(tree, 'Elapsed', 'SUBTRACT', nodes['Frame'], arrival)
    fade_safe = math_node(tree, 'Safe fade', 'MAXIMUM', nodes['Fade'], 0.001)
    shifted = math_node(tree, 'Fade interval', 'ADD', distance, fade_safe)
    ramp = math_node(tree, 'Visible fraction', 'DIVIDE', shifted, fade_safe)
    ramp.node.use_clamp = True
    # Suppress the whole stroke before Start, including the first brush sphere.
    before = math_node(tree, 'Before start', 'LESS_THAN', nodes['Frame'], nodes['Start'])
    after = math_node(tree, 'After start', 'SUBTRACT', 1, before)
    timed = math_node(tree, 'Time mask', 'MULTIPLY', ramp, after)
    final = math_node(tree, 'Weighted mask', 'MULTIPLY', timed, nodes['Weight'])
    out = new_node(tree, 'NodeGroupOutput', 'Output', (1700, 0))
    tree.links.new(final, out.inputs[0])
    set_stroke_timing(tree, timing_mode)
    for i, n in enumerate(tree.nodes):
        n.location = ((i // 4) * 220, -(i % 4) * 170)
    return tree


def attach_stroke(ob, tree):
    for mat in ob.data.materials:
        if not mat or not mat.use_nodes:
            continue
        t = mat.node_tree
        strength = t.nodes.get('Petrify Strength')
        if not strength:
            continue
        group = new_node(t, 'ShaderNodeGroup', tree.name, (450, -1200))
        group.node_tree = tree
        union = math_node(t, 'Petrify Stroke Union', 'MAXIMUM',
                          strength.inputs[0].links[0].from_socket, group.outputs[0])
        t.links.new(union, strength.inputs[0])


def stroke_groups(ob):
    groups = {}
    for mat in ob.data.materials:
        if mat and mat.use_nodes:
            for n in mat.node_tree.nodes:
                if n.type == 'GROUP' and n.node_tree and n.node_tree.get('petrify_stroke'):
                    groups[n.node_tree.name] = n.node_tree
    return list(groups.values())


def stroke_attributes(group):
    return tuple(group[key] for key in ('weight_attr', 'progress_attr', 'segment_attr') if group.get(key))


class VolumeCache:
    """Pose-aware, world-space vertices; BVH hit location and unoccluded sphere query."""
    def __init__(self, context, objects):
        self.entries = []
        depsgraph = context.evaluated_depsgraph_get()
        for ob in objects:
            issue = topology.settings_issue(ob, context.scene, DEFORM_MODIFIERS)
            if issue:
                raise RuntimeError(ob.name + '：' + issue)
            requires_store = topology.needs_store(ob, DEFORM_MODIFIERS)
            evaluated = ob.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
            try:
                if not requires_store and len(mesh.vertices) != len(ob.data.vertices):
                    raise RuntimeError(ob.name + ' 的计算后顶点数量不匹配')
                topology_signature = topology.validate_mesh(ob, mesh)
                inherited_attributes = topology.inherited_attributes(mesh) if requires_store else {}
                coords = [evaluated.matrix_world @ v.co for v in mesh.vertices]
                polygons = [tuple(p.vertices) for p in mesh.polygons]
                mesh.calc_loop_triangles()
                triangles = [tuple(t.vertices) for t in mesh.loop_triangles]
                normal_matrix = evaluated.matrix_world.to_3x3().inverted_safe().transposed()
                normals = [(normal_matrix @ v.normal).normalized() for v in mesh.vertices]
            finally:
                evaluated.to_mesh_clear()
            kd = KDTree(len(coords))
            for i, co in enumerate(coords):
                kd.insert(co, i)
            kd.balance()
            self.entries.append({'ob': ob, 'kd': kd, 'bvh': BVHTree.FromPolygons(coords, polygons),
                                 'count': len(coords), 'coords': coords, 'polygons': polygons,
                                 'triangles': triangles, 'normals': normals,
                                 'requires_store': requires_store, 'topology_signature': topology_signature,
                                 'inherited_attributes': inherited_attributes})

    def hit(self, origin, direction):
        hits = [entry['bvh'].ray_cast(origin, direction) for entry in self.entries]
        hits = [h for h in hits if h[0] is not None]
        return min(hits, key=lambda h: h[3])[0] if hits else None


class SurfaceCache(VolumeCache):
    """Target vertices plus a single BVH of visible mesh geometry for occlusion."""
    def __init__(self, context, objects):
        super().__init__(context, objects)
        coords, polygons, target_faces = [], [], []
        def append(vertices, faces, is_target):
            offset = len(coords)
            coords.extend(vertices)
            polygons.extend(tuple(offset + i for i in face) for face in faces)
            target_faces.extend([is_target] * len(faces))
        for entry in self.entries:
            append(entry['coords'], entry['polygons'], True)
        target_names = {ob.name for ob in objects}
        depsgraph = context.evaluated_depsgraph_get()
        for ob in context.visible_objects:
            if ob.type != 'MESH' or ob.name in target_names or getattr(ob, 'mmd_type', 'NONE') in {'RIGID_BODY', 'JOINT'}:
                continue
            evaluated = ob.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                append([evaluated.matrix_world @ v.co for v in mesh.vertices],
                       [tuple(face.vertices) for face in mesh.polygons], False)
            finally:
                evaluated.to_mesh_clear()
        self.target_faces = target_faces
        lower = Vector(tuple(min((v[i] for v in coords), default=0) for i in range(3)))
        upper = Vector(tuple(max((v[i] for v in coords), default=0) for i in range(3)))
        self.bounds_center = (lower + upper) * 0.5
        self.ray_span = max((upper - lower).length * 2, 1.0)
        # Expand ray/triangle boundaries slightly: exact shared edges can otherwise leak.
        self.geometry_epsilon = max(self.ray_span * 1e-7, 1e-7)
        self.surface_bvh = BVHTree.FromPolygons(coords, polygons, epsilon=self.geometry_epsilon)

    def hit(self, origin, direction):
        location, normal, index, distance = self.surface_bvh.ray_cast(origin, direction)
        return location if index is not None and self.target_faces[index] else None


class SphereStroke:
    """Accumulate coverage and nearest path position independently for every mesh."""
    brush_method = 'SPHERE'
    def __init__(self, cache, radius, hardness=0.7, strength=1.0):
        self.cache, self.radius, self.hardness, self.strength = cache, radius, hardness, strength
        self.length, self.last = 0.0, None
        self.started = False
        self.revision = 0
        self.segments = []
        self.weights = {e['ob'].name: array('f', [0]) * e['count'] for e in cache.entries}
        self.progress = {e['ob'].name: array('f', [0]) * e['count'] for e in cache.entries}
        self.segment_indices = {e['ob'].name: array('i', [-1]) * e['count'] for e in cache.entries}

    def dab(self, center, path_distance):
        for entry in self.cache.entries:
            weights, progress = self.weights[entry['ob'].name], self.progress[entry['ob'].name]
            for co, index, distance in entry['kd'].find_range(center, self.radius):
                ratio = distance / self.radius
                weight = self.strength * min(1.0, max(0.0, (1 - ratio) / max(1 - self.hardness, 0.001)))
                # Strongest encounter determines the final feather and arrival point.
                if weight > weights[index] + 1e-6 and self.accept_vertex(entry, co, index):
                    weights[index], progress[index] = weight, path_distance
                    self.segment_indices[entry['ob'].name][index] = len(self.segments) - 1
                    self.revision += 1

    def accept_vertex(self, entry, co, index):
        return True

    def sample(self, center):
        center = Vector(center)
        if self.last is None:
            if self.started:
                # A small logical step orders separate dabs without painting a bridge.
                self.length += self.radius * 0.15
            self.segments.append([self.length, self.length])
            self.dab(center, self.length)
            self.started = True
        else:
            distance = (center - self.last).length
            steps = max(1, math.ceil(distance / (self.radius * 0.15)))
            for i in range(1, steps + 1):
                self.dab(self.last.lerp(center, i / steps), self.length + distance * i / steps)
            self.length += distance
            self.segments[-1][1] = self.length
        self.last = center

    def break_segment(self):
        # Keep chronological progress but never paint across a pen-up/view change.
        self.last = None

    def affected_count(self):
        return sum(sum(w > 0 for w in weights) for weights in self.weights.values())

    def segment_progress(self, object_name):
        """Normalize each pen-down interval independently, retaining weighted coverage."""
        result = array('f', [0]) * len(self.weights[object_name])
        for i, (weight, distance, segment) in enumerate(zip(
                self.weights[object_name], self.progress[object_name], self.segment_indices[object_name])):
            if weight > 0 and segment >= 0:
                start, end = self.segments[segment]
                result[i] = weight * min(1.0, max(0.0, (distance - start) / max(end - start, 1e-8)))
        return result


class SurfaceStroke(SphereStroke):
    """Use the same stroke/timing records, restricted to front-facing visible vertices."""
    brush_method = 'SURFACE'

    def __init__(self, cache, radius, hardness=0.7, strength=1.0):
        super().__init__(cache, radius, hardness, strength)
        self.view_key, self.visibility = None, {}

    def set_view(self, origin, direction, perspective=True):
        direction = Vector(direction).normalized()
        key = (tuple(origin) if perspective else None, tuple(direction), bool(perspective))
        if key != self.view_key:
            self.visibility.clear()
            self.break_segment()
        self.view_key = key
        self.view_origin, self.view_direction, self.perspective = Vector(origin), direction, perspective

    def accept_vertex(self, entry, co, index):
        if self.view_key is None:
            raise RuntimeError('表面绘制需要当前视角')
        key = (entry['ob'].name, index)
        if key not in self.visibility:
            if self.perspective:
                origin = self.view_origin
                ray = co - origin; distance = ray.length
                direction = ray.normalized()
            else:
                distance = self.cache.ray_span
                direction = self.view_direction
                origin = co - direction * distance
            visible = distance > 0 and entry['normals'][index].dot(direction) < -1e-6
            if visible:
                epsilon = max(self.radius * 1e-5, max(abs(v) for v in co) * 5e-7,
                              self.cache.geometry_epsilon * 2)
                hit = self.cache.surface_bvh.ray_cast(origin, direction, max(0.0, distance - epsilon))
                visible = hit[0] is None
            self.visibility[key] = visible
        return self.visibility[key]


@full_materials_edit
def commit_stroke(context, stroke, animated=False, erase=False):
    """Commit once; a failed write must not leave a half-applied stroke."""
    settings = context.scene.petrify_settings
    if erase and settings.erase_scope == 'SELECTED' and not any(erase_groups(e['ob'], settings) for e in stroke.cache.entries):
        raise RuntimeError('请先在当前目标的路径列表勾选要擦除的路径')
    entries = [e for e in stroke.cache.entries if any(stroke.weights[e['ob'].name])]
    storage_tokens = topology.begin_commit(context, entries, DEFORM_MODIFIERS)
    try:
        return _commit_stroke_atomic(context, stroke, animated, erase, entries)
    except Exception:
        topology.rollback(storage_tokens)
        raise


def _commit_stroke_atomic(context, stroke, animated, erase, entries):
    settings = context.scene.petrify_settings
    attributes, materials = [], {}
    for entry in stroke.cache.entries:
        ob = entry['ob']
        if not any(stroke.weights[ob.name]):
            continue
        names = {STATIC_ATTR}
        if erase:
            names.update(name for group in stroke_groups(ob) for name in stroke_attributes(group))
        mesh = topology.data_mesh(ob)
        attributes.append((ob, set(mesh.attributes.keys()),
                           {name: read_attribute(ob, name) for name in names
                            if mesh.attributes.get(name) is not None}))
        if animated and not erase:
            for mat in ob.data.materials:
                if mat and mat.use_nodes and mat.name not in materials:
                    tree = mat.node_tree
                    materials[mat.name] = (set(tree.nodes.keys()), [
                        (link.from_node.name, list(link.from_node.outputs).index(link.from_socket),
                         link.to_node.name, list(link.to_node.inputs).index(link.to_socket))
                        for link in tree.links])
    old_groups = set(bpy.data.node_groups.keys())
    old_count, old_index = len(settings.strokes), settings.stroke_index
    old_frame, old_end = context.scene.frame_current, context.scene.frame_end
    try:
        result = _commit_stroke(context, stroke, animated, erase)
        for entry in entries:
            topology.sync_object(entry['ob'])
            if ice.companion(entry['ob']):
                ice.refresh(entry['ob'], sys.modules[__name__])
        return result
    except Exception:
        while len(settings.strokes) > old_count:
            settings.strokes.remove(len(settings.strokes) - 1)
        settings.stroke_index = old_index
        for name, (node_names, links) in materials.items():
            tree = bpy.data.materials[name].node_tree
            for node_name in tuple(tree.nodes.keys()):
                if node_name not in node_names:
                    tree.nodes.remove(tree.nodes[node_name])
            for source, output, dest, input_index in links:
                tree.links.new(tree.nodes[source].outputs[output], tree.nodes[dest].inputs[input_index])
        for ob, original_names, values in attributes:
            mesh = topology.data_mesh(ob)
            for name in tuple(mesh.attributes.keys()):
                if name not in original_names and name.startswith(('Petrify_',)):
                    mesh.attributes.remove(mesh.attributes[name])
            for name, data in values.items():
                float_attribute(ob, name).data.foreach_set('value', data)
            mesh.update()
            topology.sync_object(ob)
        for name in tuple(bpy.data.node_groups.keys()):
            group = bpy.data.node_groups[name]
            if name not in old_groups and group.get('petrify_stroke'):
                bpy.data.node_groups.remove(group)
        context.scene.frame_end = old_end
        context.scene.frame_set(old_frame)
        raise


def _commit_stroke(context, stroke, animated=False, erase=False):
    settings = context.scene.petrify_settings
    tree = None
    if animated and not erase:
        if settings.end_frame < settings.start_frame:
            raise RuntimeError('结束帧不能小于开始帧')
        if not stroke.affected_count():
            raise RuntimeError('笔刷范围内没有可绘制顶点，请增大半径或调整视角')
        tree = make_stroke_group('石化路径 %02d' % (len(settings.strokes) + 1),
                                  settings.start_frame, settings.end_frame, settings.fade_frames,
                                  settings.timing_mode)
        tree['segment_count'] = len(stroke.segments)
        tree['brush_method'] = stroke.brush_method
    for entry in stroke.cache.entries:
        ob = entry['ob']
        weights = stroke.weights[ob.name]
        if not any(weights):
            continue
        if tree:
            weighted_progress = array('f', (w * d / max(stroke.length, 1e-8)
                                      for w, d in zip(weights, stroke.progress[ob.name])))
            write_attribute(ob, tree['weight_attr'], weights)
            write_attribute(ob, tree['progress_attr'], weighted_progress)
            write_attribute(ob, tree['segment_attr'], stroke.segment_progress(ob.name))
            attach_stroke(ob, tree)
        else:
            if not erase or settings.erase_scope == 'ALL':
                old = read_attribute(ob, STATIC_ATTR)
                values = array('f', (a * (1 - b) if erase else max(a, b) for a, b in zip(old, weights)))
                write_attribute(ob, STATIC_ATTR, values)
            if erase:
                for group in erase_groups(ob, settings):
                    for name in stroke_attributes(group):
                        old = read_attribute(ob, name)
                        write_attribute(ob, name, array('f', (a * (1-b) for a, b in zip(old, weights))))
    if tree:
        item = settings.strokes.add()
        item.name, item.group = tree.name, tree
        item.start_frame, item.end_frame, item.fade_frames = settings.start_frame, settings.end_frame, settings.fade_frames
        item.timing_mode = settings.timing_mode
        settings.stroke_index = len(settings.strokes) - 1
        sync_path_visibility(item, context)
        context.scene.frame_end = max(context.scene.frame_end, settings.end_frame)
        context.scene.frame_set(settings.end_frame)
    return tree


def stroke_users(group, users=None):
    if not group:
        return [], []
    users = bpy.data.user_map() if users is None else users
    pending, seen, materials, objects = [group], set(), set(), set()
    while pending:
        data = pending.pop()
        if data in seen:
            continue
        seen.add(data)
        for user in users.get(data, ()):
            if isinstance(user, bpy.types.Material):
                materials.add(user); pending.append(user)
            elif isinstance(user, bpy.types.Mesh):
                pending.append(user)
            elif isinstance(user, bpy.types.Object):
                if user.type == 'MESH' and not user.get(ice.TAG) and not user.get(topology.TAG):
                    objects.add(user)
            elif isinstance(user, bpy.types.NodeTree):
                pending.append(user)
    return list(materials), list(objects)


def refresh_stroke_objects(groups, context):
    users = bpy.data.user_map() if groups else {}
    objects = {ob for group in groups for ob in stroke_users(group, users)[1]}
    lightweight.refresh(context)
    for ob in objects:
        if ice.companion(ob):
            ice.build_geometry(ob, sys.modules[__name__]); ice.apply_values(ob)


_syncing_paths = False


def sync_path_visibility(self, context):
    if _syncing_paths:
        return
    s = context.scene.petrify_settings
    solo = any(item.solo and item.enabled for item in s.strokes if item.group)
    changed = []
    for item in s.strokes:
        group = item.group
        if not group:
            continue
        factor = float(item.enabled and (not solo or item.solo))
        old = group.get('asfr_visible', 1.0)
        group['asfr_visible'] = factor
        gate = group.nodes.get('ASFR Enabled')
        if gate is None:
            output = group.nodes.get('Output')
            source = output.inputs[0].links[0].from_socket
            gate = math_node(group, 'ASFR Enabled', 'MULTIPLY', source, factor).node
            group.links.new(gate.outputs[0], output.inputs[0])
        gate.inputs[1].default_value = factor
        if old != factor: changed.append(group)
    refresh_stroke_objects(changed, context)


def erase_groups(ob, settings):
    groups = stroke_groups(ob)
    if settings.erase_scope == 'SELECTED':
        selected = {item.group for item in settings.strokes if item.selected and item.group}
        groups = [g for g in groups if g in selected]
    return groups


def sync_stroke(self, context):
    if _syncing_paths:
        return
    tree = self.group
    if tree:
        for name, value in [('Start', self.start_frame), ('End', max(self.start_frame, self.end_frame)), ('Fade', self.fade_frames)]:
            tree.nodes[name].outputs[0].default_value = value
        set_stroke_timing(tree, self.timing_mode)
        refresh_stroke_objects([tree], context)


def stroke_timing_modes(self, context):
    # Old files contain global progress, but no recoverable pen-up boundaries.
    return TIMING_MODES if self.group and self.group.get('segment_attr') else LEGACY_TIMING_MODES


class PETRIFY_Stroke(bpy.types.PropertyGroup):
    group: PointerProperty(type=bpy.types.NodeTree)
    selected: BoolProperty(name='选中此路径', default=False)
    enabled: BoolProperty(name='启用', default=True, update=sync_path_visibility)
    solo: BoolProperty(name='独显', description='只显示勾选独显且启用的时间路径；静态涂抹仍保留', default=False, update=sync_path_visibility)
    # Missing values in 2.0.0-2.0.3 files must retain their original playback.
    timing_mode: EnumProperty(name='时间模式', items=stroke_timing_modes, default=0, update=sync_stroke)
    start_frame: IntProperty(name='开始帧', default=1, min=-1048574, max=1048574, update=sync_stroke)
    end_frame: IntProperty(name='结束帧', default=30, min=-1048574, max=1048574, update=sync_stroke)
    fade_frames: FloatProperty(name='过渡帧数', default=2, min=0.01, max=120, update=sync_stroke)


def update_one_look(self, ob):
    stone = stone_for(ob)
    if stone and stone.get('petrify_preset', 'DEFAULT') == 'DEFAULT':
        for name in ('color', 'roughness', 'bump', 'scale'):
            default_control(stone, name).default_value = getattr(self, name)
        default_control(stone, 'fine_scale').default_value = self.scale * 5
    if stone and stone.get('petrify_preset') == resin.KEY:
        resin.update(self, stone)
    if stone and stone.get('petrify_preset') == gold.KEY:
        gold.update(self, stone)
    if ob and ob.type == 'MESH':
        for mat in ob.data.materials:
            if mat and mat.use_nodes and (n := mat.node_tree.nodes.get('Petrify Strength')):
                n.inputs[1].default_value = self.amount
    if stone:
        normals.update(self, ob, stone)
    if stone and stone.get('petrify_preset') == ice.KEY:
        c = ice.companion(ob)
        if c and c.get('petrify_normal_shell') != normals.VERSION:
            ice.refresh(ob, sys.modules[__name__])
        ice.update(self, ob)


_syncing_look = False


@full_materials_edit
def update_look(self, context):
    if _syncing_look:
        return
    for ob in target_meshes(context):
        update_one_look(self, ob)


def sync_target(self, context):
    global _syncing_look
    if lightweight.is_active():
        lightweight.exit()
    meshes = target_meshes(context)
    if not meshes:
        return
    stone = stone_for(meshes[0])
    _syncing_look = True
    try:
        if stone:
            normals.sync(self, meshes[0])
            key = stone.get('petrify_preset', 'DEFAULT')
            if key not in {item[0] for item in _preset_items}:
                refresh_presets()
            self.stone_preset = key if key in {item[0] for item in _preset_items} else 'DEFAULT'
            if key == 'DEFAULT':
                for name in ('color', 'roughness', 'bump', 'scale'):
                    setattr(self, name, default_control(stone, name).default_value)
            if key == resin.KEY:
                resin.sync(self, stone)
            if key == gold.KEY:
                gold.sync(self, stone)
            if key == ice.KEY:
                ice.sync(self, meshes[0])
            for mat in meshes[0].data.materials:
                if mat and mat.use_nodes and (n := mat.node_tree.nodes.get('Petrify Strength')):
                    self.amount = n.inputs[1].default_value
                    break
        self.radius = max(max(ob.dimensions) for ob in meshes) * 0.06
    finally:
        _syncing_look = False


def resin_color_mode_get(self):
    # Old 3.3.0 scenes only contain resin_keep_color. Read them without changing
    # any material or requiring the user to reapply the preset after upgrading.
    value=self.get('resin_color_mode')
    return value if value in (0,1,2) else (0 if self.resin_keep_color else 1)


def resin_color_mode_set(self, value):
    self['resin_color_mode']=value


def update_legacy_resin_color(self, context):
    # Preserve existing scripts that set the former checkbox programmatically.
    if not _syncing_look:
        self.resin_color_mode='ORIGINAL' if self.resin_keep_color else 'UNIFORM'


class PETRIFY_Settings(bpy.types.PropertyGroup):
    target: PointerProperty(name='目标模型', type=bpy.types.Object, poll=lambda self, ob: ob.type in {'MESH', 'ARMATURE', 'EMPTY'}, update=sync_target)
    include_children: BoolProperty(name='包含模型子物体', description='选角色根节点或骨骼可同时绘制身体、衣服等可见网格；排除 MMD 刚体', default=True, update=sync_target)
    brush_method: EnumProperty(name='使用方法', items=[('SPHERE', '球形穿透', '范围内的目标表面、内部和背面一起绘制'), ('SURFACE', '表面涂抹', '只绘制当前视角可见、朝向视角的目标表面；不穿透衣服或背面')], default='SPHERE')
    radius: FloatProperty(name='笔刷半径', description='世界空间半径；表面方式只影响范围内可见的外层，球形方式穿透绘制', default=0.2, min=0.00001, soft_max=10, subtype='DISTANCE')
    depth: FloatProperty(name='球心深入', description='以半径为单位，沿视线从表面向内移动球心；0 在表面，1 深入一个半径', default=0.5, min=-2, max=3)
    hardness: FloatProperty(name='边缘硬度', default=0.7, min=0, max=1)
    strength: FloatProperty(name='笔刷强度', default=1, min=0.01, max=1)
    draw_mode: EnumProperty(name='绘制方式', items=[('STATIC','静态涂抹','涂抹结果在所有帧显示'),('TIMELINE','时间路径','选择整体渐变、按绘制顺序推进或各段同时推进')], default='TIMELINE')
    multi_angle: BoolProperty(name='多角度连续绘制', description='松开左键暂停，可旋转视角再继续；Enter 完成本次绘制，时间模式合为一条路径；关闭则松开左键完成', default=True)
    timing_mode: EnumProperty(name='时间模式', items=TIMING_MODES, default='SIMULTANEOUS')
    start_frame: IntProperty(name='开始帧', default=1, min=-1048574, max=1048574)
    end_frame: IntProperty(name='结束帧', default=30, min=-1048574, max=1048574)
    fade_frames: FloatProperty(name='过渡帧数', description='每个区域在到达对应帧前逐渐显示效果的时间', default=2, min=0.01, max=120)
    strokes: CollectionProperty(type=PETRIFY_Stroke)
    stroke_index: IntProperty(default=0)
    show_look: BoolProperty(name='材质预设', default=False)
    show_scene_presets: BoolProperty(name='显示工程内预设', description='仅显示当前工程携带、未存入本机库的自定义预设；不会写回本机目录', default=False, update=scene_presets_changed)
    erase_scope: EnumProperty(name='擦除范围', items=[('ALL', '全部涂抹', '擦除静态范围及所有时间路径'), ('SELECTED', '勾选路径', '仅擦除路径列表中勾选的路径，不改其他路径或静态范围')], default='ALL')
    path_shift: IntProperty(name='整体偏移帧数', default=0, min=-1048574, max=1048574)
    stone_preset: EnumProperty(name='材质预设', items=preset_items, default=0, update=preset_selection_changed)
    preset_source_mode: EnumProperty(name='保存来源', items=[('GROUP', '自制节点组', '保存节点组本身设定的初始参数。要保存材质中某个组节点上调整过的参数，请在着色器编辑器选中该组，点击“将选中节点组存为预设”'), ('CURRENT', '当前材质外观', '保存目标模型当前使用的石头节点和参数')], default='GROUP')
    preset_source_group: PointerProperty(name='材质节点组', type=bpy.types.NodeTree, poll=lambda self, tree: tree.bl_idname == 'ShaderNodeTree' and not tree.is_embedded_data)
    preset_new_name: StringProperty(name='新预设名称', default='我的石材', maxlen=120)
    preset_output: StringProperty(name='着色器输出', description='有多个着色器输出时，填写要保存的输出名称；单个输出留空即可')
    # Retain the old RNA field for saved scenes; it no longer controls main-panel visibility.
    show_surface: BoolProperty(name='旧贴图蒙版维护', default=False)
    show_paths: BoolProperty(name='已有时间路径', default=True)
    is_erasing: BoolProperty(default=False)
    resolution: EnumProperty(name='蒙版分辨率', items=[('2048','2K',''),('4096','4K',''),('8192','8K','')], default='4096')
    gold_color: FloatVectorProperty(name='黄金颜色', subtype='COLOR', size=4, default=gold.COLOR, min=0, max=1, options=set(), update=update_look)
    gold_metallic: FloatProperty(name='金属度', description='1 为金属反射；降低后混入非金属响应，不是覆盖比例', default=1, min=0, max=1, options=set(), update=update_look)
    gold_roughness: FloatProperty(name='黄金粗糙度', description='低值为抛光镜面，高值为磨砂；反射仍依赖环境和灯光', default=.18, min=.02, max=1, options=set(), update=update_look)
    gold_texture: FloatProperty(name='金属微纹理强度', description='0 完全光滑；程序化细小凹凸，不增加模型面数或生成贴图', default=.06, min=0, max=1, options=set(), update=update_look)
    gold_scale: FloatProperty(name='金属微纹理尺度', description='使用生成坐标；数值越大纹理越细，非真实几何位移', default=80, min=1, max=500, options=set(), update=update_look)
    ice_clarity: FloatProperty(name='冰层通透度', subtype='PERCENTAGE', default=90, min=0, max=100, options=set(), update=update_look)
    ice_frost: FloatProperty(name='白霜覆盖量', subtype='PERCENTAGE', default=12, min=0, max=100, options=set(), update=update_look)
    ice_tint: FloatProperty(name='冰色叠加比例', subtype='PERCENTAGE', default=18, min=0, max=100, options=set(), update=update_look)
    ice_color: FloatVectorProperty(name='冰层颜色', subtype='COLOR', size=4, default=(.32,.7,.92,1), min=0, max=1, options=set(), update=update_look)
    ice_roughness: FloatProperty(name='冰面粗糙度', default=.16, min=.02, max=1, options=set(), update=update_look)
    ice_thickness: FloatProperty(name='冰层厚度 · 高度%', default=.16, min=.003, max=3, options=set(), update=update_look)
    ice_count: IntProperty(name='冰锥候选数量', description='均匀分布的候选点中，只在已涂抹区域长出；每个网格最多 1000 个', default=180, min=0, max=1000, options=set(), update=update_look)
    ice_length: FloatProperty(name='冰锥长度 · 高度%', default=6, min=.05, max=50, options=set(), update=update_look)
    ice_width: FloatProperty(name='冰锥半径 · 高度%', default=.65, min=.02, max=10, options=set(), update=update_look)
    ice_random: FloatProperty(name='大小随机程度', default=.6, min=0, max=1, options=set(), update=update_look)
    ice_direction_mode: EnumProperty(name='冰锥朝向', items=[('LEGACY','原有自然朝向','完整保留 3.4.0 的表面法线与向下混合，不额外改变原有方向'),('CUSTOM','统一指定方向','全部冰锥朝自定义方向'),('RANDOM','指定方向 + 随机','在自定义方向周围随机偏转；种子固定时不随帧跳动')], default='LEGACY', options=set(), update=update_look)
    ice_direction_space: EnumProperty(name='方向坐标系', items=[('WORLD','世界坐标','模型旋转后仍朝场景中设定的方向'),('LOCAL','模型局部坐标','方向随模型物体旋转')], default='WORLD', options=set(), update=update_look)
    ice_direction_azimuth: FloatProperty(name='水平角', description='0° 朝 +X，90° 朝 +Y，180° 朝 −X', subtype='ANGLE', default=0, min=-math.pi, max=math.pi, options=set(), update=update_look)
    ice_direction_elevation: FloatProperty(name='仰俯角', description='90° 向上，0° 水平，−90° 向下', subtype='ANGLE', default=-math.pi/2, min=-math.pi/2, max=math.pi/2, options=set(), update=update_look)
    ice_direction_spread: FloatProperty(name='随机最大偏转角', description='相对指定方向的圆锥半角；0° 完全一致，180° 全空间随机', subtype='ANGLE', default=math.pi/6, min=0, max=math.pi, options=set(), update=update_look)
    ice_direction_seed: IntProperty(name='朝向随机种子', description='仅改变朝向，不重新分布位置或改变大小；即时生效', default=0, min=0, max=999999, options=set(), update=update_look)
    ice_down: FloatProperty(name='朝向向下比例', description='0 沿表面朝外形成冰晶，1 朝世界向下形成垂挂冰锥', default=.85, min=0, max=1, options=set(), update=update_look)
    ice_spike_clarity: FloatProperty(name='冰锥通透度', subtype='PERCENTAGE', default=58, min=0, max=100, options=set(), update=update_look)
    ice_style: EnumProperty(name='冰锥外形', items=[('ICICLE','圆润冰锥','平滑锥状'),('CRYSTAL','棱面冰晶','五棱晶体；朝向向下比例设为 0 可向外生长')], default='ICICLE', options=set(), update=update_look)
    ice_exclude_materials: StringProperty(name='排除材质关键词', description='逗号分隔，例如 眼,脸；修改后点重新分布。只影响冰锥，不改变冰层涂抹', default='', options=set(), update=update_look)
    ice_seed: IntProperty(name='分布种子', description='改动后点击重新分布生效，不在拖动参数时重新计算模型', default=0, min=0, max=999999, options=set(), update=update_look)
    resin_keep_color: BoolProperty(name='保留模型原色', description='旧版兼容字段，界面请使用配色方式', default=True, update=update_legacy_resin_color)
    resin_color_mode: EnumProperty(name='配色方式', items=[('ORIGINAL', '模型原色', '保留模型的基础纹理和颜色', 0), ('UNIFORM', '统一颜色', '仅使用自选颜色', 1), ('MIX', '叠加颜色', '模型原色与自选颜色按比例混合', 2)], get=resin_color_mode_get, set=resin_color_mode_set, update=update_look)
    resin_blend: FloatProperty(name='颜色叠加比例', description='0% 为模型原色，100% 为自选颜色；中间值按比例混合，不改变涂抹范围', subtype='PERCENTAGE', default=50, min=0, max=100, update=update_look)
    resin_color: FloatVectorProperty(name='树脂颜色', subtype='COLOR', size=4, min=0, max=1, default=(0.8,0.65,0.58,1), update=update_look)
    resin_roughness: FloatProperty(name='树脂粗糙度', min=0.02, max=1, default=0.2, update=update_look)
    resin_specular: FloatProperty(name='高光强度', min=0, max=1, default=0.5, update=update_look)
    resin_coat: FloatProperty(name='透明涂层', min=0, max=1, default=0.65, update=update_look)
    resin_coat_roughness: FloatProperty(name='涂层粗糙度', min=0.02, max=1, default=0.12, update=update_look)
    color: FloatVectorProperty(name='石头颜色', subtype='COLOR', size=4, min=0, max=1, default=(0.39,0.415,0.44,1), update=update_look)
    roughness: FloatProperty(name='粗糙度', min=0, max=1, default=0.87, update=update_look)
    bump: FloatProperty(name='凹凸强度', min=0, max=1, default=0.22, update=update_look)
    scale: FloatProperty(name='颗粒密度', min=1, max=1000, default=85, update=update_look)
    normal_original_strength: FloatProperty(name='人物细节强度', description='在效果材质中保留已识别的原法线/凹凸；0 关闭，1 完整保留；未涂抹处不改动', default=1, min=0, max=1, options=set(), update=update_look)
    normal_detail_strength: FloatProperty(name='材质纹理强度', description='效果材质已有法线/凹凸的叠加强度；0 关闭细节，不改变真实几何', default=1, min=0, max=1, options=set(), update=update_look)
    amount: FloatProperty(name='效果强度', description='设为 0 可预览原样，涂抹记录不丢失', min=0, max=1, default=1, update=update_look)


class PETRIFY_OT_save_custom_preset(bpy.types.Operator):
    bl_idname = 'petrify.save_custom_preset'
    bl_label = '保存为新预设'
    bl_description = '复制节点组及嵌套组、打包贴图，永久保存到本机预设库；不会覆盖同名预设或改变当前模型'

    def execute(self, context):
        s = context.scene.petrify_settings
        source = s.preset_source_group
        if s.preset_source_mode == 'CURRENT':
            meshes = target_meshes(context)
            source = stone_for(meshes[0]) if meshes else None
        try:
            output_name = s.preset_output if s.preset_source_mode == 'GROUP' and len(shader_outputs(source)) > 1 else ''
            key = save_custom_preset(source, s.preset_new_name, output_name)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        s.stone_preset = key; s.show_look = True
        self.report({'INFO'}, '已保存“' + preset_label(key) + '”，可在材质预设中应用；重启后仍可使用')
        return {'FINISHED'}


class PETRIFY_OT_save_selected_preset(bpy.types.Operator):
    bl_idname = 'petrify.save_selected_preset'
    bl_label = '将选中节点组存为预设'
    bl_description = '保存选中组节点当前的输入数值；外部连线请先收入节点组'
    preset_name: StringProperty(name='新预设名称', maxlen=120)
    output_name: StringProperty(name='着色器输出', description='只有一个着色器输出时留空')
    source_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    input_values: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        node = getattr(context, 'active_node', None)
        return bool(node and node.type == 'GROUP' and node.node_tree and node.node_tree.bl_idname == 'ShaderNodeTree')

    def invoke(self, context, event):
        node = context.active_node
        if any(socket.is_linked for socket in node.inputs):
            self.report({'ERROR'}, '选中组节点有外部输入连线，请先将这些节点一起收入节点组，或在 3D 面板使用组的默认值保存')
            return {'CANCELLED'}
        self.source_name = node.node_tree.name; self.preset_name = node.node_tree.name; self.output_name = ''
        values = {}
        for sock in node.inputs:
            if hasattr(sock, 'default_value'):
                value = sock.default_value
                values[sock.identifier] = value if isinstance(value, (int, float, str, bool)) else list(value)
        self.input_values = json.dumps(values)
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, 'preset_name')
        if len(shader_outputs(bpy.data.node_groups.get(self.source_name))) > 1:
            self.layout.prop(self, 'output_name')
        self.layout.label(text='保存选中节点的输入数值和打包贴图')

    def execute(self, context):
        try:
            key = save_custom_preset(bpy.data.node_groups.get(self.source_name), self.preset_name, self.output_name, json.loads(self.input_values or '{}'))
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        context.scene.petrify_settings.stone_preset = key
        self.report({'INFO'}, '已保存“' + preset_label(key) + '”到本机预设库')
        return {'FINISHED'}


class PETRIFY_OT_refresh_presets(bpy.types.Operator):
    bl_idname = 'petrify.refresh_presets'
    bl_label = '刷新预设列表'

    def execute(self, context):
        refresh_presets()
        if _preset_library_errors:
            self.report({'WARNING'}, '部分预设未载入：' + _preset_library_errors[0])
        else:
            self.report({'INFO'}, '已刷新 %d 个自定义预设' % len(_custom_presets))
        return {'FINISHED'}


class PETRIFY_OT_delete_preset(bpy.types.Operator):
    bl_idname = 'petrify.delete_preset'
    bl_label = '删除选中预设'
    bl_description = '从预设库移除自定义预设，保留模型上已有的效果；可用恢复最近删除找回；内置预设不可删除'

    @classmethod
    def poll(cls, context):
        return bool(context.scene and valid_custom_id(context.scene.petrify_settings.stone_preset))

    def execute(self, context):
        try:
            name = delete_custom_preset(context.scene.petrify_settings.stone_preset)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        self.report({'INFO'}, '已删除“' + name + '”；模型外观保留，可恢复最近删除')
        return {'FINISHED'}


class PETRIFY_OT_restore_preset(bpy.types.Operator):
    bl_idname = 'petrify.restore_preset'
    bl_label = '恢复最近删除'
    bl_description = '恢复上次移入回收区的自定义预设，不改变模型外观'

    @classmethod
    def poll(cls, context):
        return bool(_deleted_presets)

    def execute(self, context):
        try:
            key = max(_deleted_presets, key=lambda k: _deleted_presets[k].get('deleted_at', 0))
            restore_deleted_preset(key)
            context.scene.petrify_settings.stone_preset = key
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        self.report({'INFO'}, '已恢复“' + preset_label(key) + '”')
        return {'FINISHED'}


class PETRIFY_OT_cleanup_stone_groups(bpy.types.Operator):
    bl_idname = 'petrify.cleanup_stone_groups'
    bl_label = '清理旧版闲置石化组'
    bl_description = '仅清理插件生成、无引用且未保留的旧石化组；正在使用的材质和用户节点组保持不变'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        count = cleanup_unused_stone_groups()
        self.report({'INFO'}, '已清理 %d 个闲置石化组' % count)
        return {'FINISHED'}


class PETRIFY_OT_open_preset_directory(bpy.types.Operator):
    bl_idname = 'petrify.open_preset_directory'
    bl_label = '打开预设目录'
    bl_description = '打开本机自定义预设库；备份或分享时同时复制同名 blend 和 json 文件'

    def execute(self, context):
        try:
            directory = custom_preset_directory()
            directory.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.path_open(filepath=str(directory))
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


class PETRIFY_OT_ice_direction_axis(bpy.types.Operator):
    bl_idname = 'petrify.ice_direction_axis'
    bl_label = '设置冰锥朝向'
    bl_description = '按所选世界/局部坐标设置方向，保留当前随机模式'
    bl_options = {'REGISTER', 'UNDO'}
    axis: EnumProperty(items=[(a,a,'') for a in ('PX','NX','PY','NY','PZ','NZ')])

    @full_materials_edit
    def execute(self,context):
        global _syncing_look
        s=context.scene.petrify_settings
        angles={'PX':(0,0),'NX':(math.pi,0),'PY':(math.pi/2,0),'NY':(-math.pi/2,0),'PZ':(0,math.pi/2),'NZ':(0,-math.pi/2)}
        before=_syncing_look;_syncing_look=True
        try:
            if s.ice_direction_mode=='LEGACY':s.ice_direction_mode='CUSTOM'
            s.ice_direction_azimuth,s.ice_direction_elevation=angles[self.axis]
        finally:_syncing_look=before
        update_look(s,context)
        return {'FINISHED'}


class PETRIFY_OT_reseed_ice(bpy.types.Operator):
    bl_idname = 'petrify.reseed_ice'
    bl_label = '重新分布冰锥'
    bl_description = '根据当前种子重建固定锚点；不会改动模型、涂抹范围或时间路径'
    bl_options = {'REGISTER', 'UNDO'}

    @full_materials_edit
    def execute(self, context):
        try:
            for ob in target_meshes(context):
                if ice.companion(ob):ice.refresh(ob, sys.modules[__name__], reseed=True)
        except Exception as exc:
            self.report({'ERROR'}, str(exc));return {'CANCELLED'}
        return {'FINISHED'}


class PETRIFY_OT_refresh_normals(bpy.types.Operator):
    bl_idname = 'petrify.refresh_normals'
    bl_label = '启用 / 重新识别人物法线'
    bl_description = '首次升级旧工程或编辑原材质后重新识别；不重置预设参数、涂抹和时间路径'
    bl_options = {'REGISTER', 'UNDO'}

    @full_materials_edit
    def execute(self, context):
        meshes = target_meshes(context)
        try:
            normals.migrate_legacy_sources()
            for ob in meshes:
                ensure_volume(ob)
                stone = stone_for(ob)
                if not stone: continue
                normals.ensure(ob, stone, force=True)
                if ice.companion(ob): ice.refresh(ob, sys.modules[__name__])
            normals.cleanup_extractors()
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        self.report({'INFO'}, '已更新法线连接；无贴图使用表面法线，复杂多分支材质请检查提示')
        return {'FINISHED'}


class PETRIFY_OT_apply_preset(bpy.types.Operator):
    bl_idname = 'petrify.apply_preset'
    bl_label = '应用材质预设'
    bl_description = '切换当前目标的材质外观并恢复该预设上次参数，保留原贴图、涂抹范围和时间路径；可撤销'
    bl_options = {'REGISTER', 'UNDO'}

    @full_materials_edit
    def execute(self, context):
        s = context.scene.petrify_settings
        meshes = target_meshes(context)
        if not meshes or any(not stone_for(ob) for ob in meshes):
            self.report({'ERROR'}, '请先初始化目标模型')
            return {'CANCELLED'}
        try:
            key = s.stone_preset
            management.preflight(context, meshes, sys.modules[__name__])
            stone_template(key)
            with management.batch(context, meshes, sys.modules[__name__]):
                for ob in meshes:
                    ensure_volume(ob)
                    replace_stone(ob, key)
            radius = s.radius
            sync_target(s, context)
            s.radius = radius
            protect_source_images(meshes)
            cleanup_unused_stone_groups()
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, '已应用材质预设；涂抹和时间路径保持不变')
        return {'FINISHED'}


class PETRIFY_OT_reset_look(bpy.types.Operator):
    bl_idname = 'petrify.reset_look'
    bl_label = '重置当前材质参数'
    bl_description = '重置当前已应用预设的外观和法线参数；保留涂抹、时间路径和效果强度'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    @full_materials_edit
    def execute(self, context):
        objects = target_meshes(context)
        if not objects or any(not stone_for(ob) for ob in objects): return {'CANCELLED'}
        try:
            with management.batch(context, objects, sys.modules[__name__]):
                for ob in objects:
                    replace_stone(ob, stone_for(ob).get('petrify_preset', 'DEFAULT'), reset=True)
            sync_target(context.scene.petrify_settings, context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


class PETRIFY_OT_shift_paths(bpy.types.Operator):
    bl_idname = 'petrify.shift_paths'
    bl_label = '平移勾选路径'
    bl_description = '同时平移当前目标勾选路径的起止帧，不改变时长；可撤销'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _syncing_paths
        s = context.scene.petrify_settings
        available = {g for ob in target_meshes(context) for g in stroke_groups(ob)}
        items = [i for i in s.strokes if i.selected and i.group in available]
        if not items:
            self.report({'WARNING'}, '请先勾选路径'); return {'CANCELLED'}
        delta = s.path_shift
        if any(not -1048574 <= frame + delta <= 1048574 for i in items for frame in (i.start_frame, i.end_frame)):
            self.report({'WARNING'}, '偏移后的帧数超出范围'); return {'CANCELLED'}
        _syncing_paths = True
        try:
            for i in items:
                i.start_frame += delta; i.end_frame += delta
                i.group.nodes['Start'].outputs[0].default_value = i.start_frame
                i.group.nodes['End'].outputs[0].default_value = max(i.start_frame, i.end_frame)
        finally:
            _syncing_paths = False
        refresh_stroke_objects([i.group for i in items], context)
        return {'FINISHED'}


class PETRIFY_OT_prepare(bpy.types.Operator):
    bl_idname = 'petrify.prepare'
    bl_label = '初始化 / 升级目标模型'
    bl_options = {'REGISTER', 'UNDO'}

    @full_materials_edit
    def execute(self, context):
        root = target(context)
        meshes = target_meshes(context)
        if not meshes:
            self.report({'ERROR'}, '目标没有可见网格，请选择网格或角色根节点')
            return {'CANCELLED'}
        try:
            management.preflight(context, meshes, sys.modules[__name__], prepare=True)
            stone_template('DEFAULT')
            with management.batch(context, meshes, sys.modules[__name__], prepare=True):
                for ob in meshes:
                    prepare_object(context, ob, int(context.scene.petrify_settings.resolution))
                    ensure_volume(ob)
                context.scene.petrify_settings.target = root
                update_look(context.scene.petrify_settings, context)
        except Exception as e:
            context.scene.petrify_settings.target = root
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, '已准备 %d 个网格；可选择球形穿透或表面涂抹' % len(meshes))
        return {'FINISHED'}


class PETRIFY_OT_select_target(bpy.types.Operator):
    bl_idname = 'petrify.select_target'
    bl_label = '使用当前选择的模型'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.active_object
        if not ob or ob.type not in {'MESH', 'ARMATURE', 'EMPTY'}:
            self.report({'ERROR'}, '请先选择网格、骨骼或角色根节点')
            return {'CANCELLED'}
        candidate, parent = ob, ob.parent
        while parent:
            if getattr(parent, 'mmd_type', '') == 'ROOT':
                candidate = parent
                break
            if parent.type == 'ARMATURE':
                candidate = parent
            parent = parent.parent
        context.scene.petrify_settings.target = candidate
        return {'FINISHED'}


_active_brushes = set()


@persistent
def cancel_active_brushes(_=None):
    for operator in tuple(_active_brushes):
        try:
            operator._cancelled = True
            operator.cleanup(bpy.context)
        except (ReferenceError, RuntimeError, ValueError):
            _active_brushes.discard(operator)


class PETRIFY_OT_light_preview(bpy.types.Operator):
    bl_idname = 'petrify.light_preview'
    bl_label = '切换轻量预览'
    bl_description = '轻量模式简化角色和石头材质，保留绘制范围与时间变化；保存和正式渲染自动恢复完整材质'

    def execute(self, context):
        try:
            if lightweight.is_active():
                lightweight.exit()
                message = '已恢复完整材质'
            else:
                meshes = target_meshes(context)
                if not meshes:
                    self.report({'ERROR'}, '请先选择目标模型')
                    return {'CANCELLED'}
                if context.object and context.object.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                lightweight.enter(context, meshes)
                message = '轻量预览已开启；检查最终外观时切回完整材质'
            for area in context.screen.areas if context.screen else ():
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            self.report({'INFO'}, message)
            return {'FINISHED'}
        except Exception as exc:
            lightweight.exit()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class PETRIFY_OT_match_subdivision(bpy.types.Operator):
    bl_idname = 'petrify.match_subdivision'
    bl_label = '预览匹配渲染设置'
    bl_description = '绘制前，按照渲染设置调整修改器的视图开关和预览细分级别；保留渲染质量'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        meshes = target_meshes(context)
        if any(topology.store_for(ob) for ob in meshes):
            self.report({'WARNING'}, '已有兼容绘制数据，请保持或恢复绘制时的修改器设置')
            return {'CANCELLED'}
        for ob in meshes:
            for mod in ob.modifiers:
                if mod.type in topology.GENERATORS:
                    mod.show_viewport = mod.show_render
                    if mod.type in {'SUBSURF', 'MULTIRES'}:
                        mod.levels = mod.render_levels
        render = context.scene.render
        if render.use_simplify:
            render.simplify_subdivision = render.simplify_subdivision_render
        self.report({'INFO'}, '已同步预览设置；细分较高时首次准备需要更多时间')
        return {'FINISHED'}


class PETRIFY_OT_sphere(bpy.types.Operator):
    bl_idname = 'petrify.sphere'
    bl_label = '开始石化绘制'
    bl_description = '左键涂抹，中键旋转，Shift+中键平移，滚轮缩放；连续绘制时 Enter 完成、Esc 取消'
    bl_options = {'REGISTER', 'UNDO'}
    erase: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        s = context.scene.petrify_settings
        meshes = target_meshes(context)
        if not meshes or any(ob.get('petrify_version', 0) < 2 for ob in meshes):
            self.report({'ERROR'}, '请先初始化 / 升级目标模型')
            return {'CANCELLED'}
        if not self.erase and s.draw_mode == 'TIMELINE' and s.end_frame < s.start_frame:
            self.report({'ERROR'}, '结束帧不能小于开始帧')
            return {'CANCELLED'}
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            protect_source_images(meshes)
            self.surface = s.brush_method == 'SURFACE'
            self.cache = (SurfaceCache if self.surface else VolumeCache)(context, meshes)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.area = context.area
        self.region = next(r for r in self.area.regions if r.type == 'WINDOW')
        self.rv3d = self.area.spaces.active.region_3d
        self.stroke = (SurfaceStroke if self.surface else SphereStroke)(self.cache, s.radius, s.hardness, s.strength)
        self.animated = s.draw_mode == 'TIMELINE' and not self.erase
        self.multi_angle = s.multi_angle
        self.depth = 0.0 if self.surface else s.depth
        self.dragging, self.center = False, None
        self.overlay = OverlayPreview(self.stroke, erase=self.erase, area=self.area, region=self.region)
        self._preview_revision = -1
        self._redraw_pending = False
        self._next_redraw = 0.0
        self._draw_error = None
        self._cancelled = False
        self._cursor_shader = self._cursor_batch = None
        self._window, self._wm = context.window, context.window_manager
        self.area.spaces.active.shading.type = 'MATERIAL'
        self._handle = self._timer = None
        self._overlay_state = ViewportOverlayState(self.area.spaces.active)
        try:
            self._overlay_state.enable()
            self._handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_cursor, (), 'WINDOW', 'POST_VIEW')
            self._timer = self._wm.event_timer_add(1.0 / 30, window=self._window)
            _active_brushes.add(self)
            finish_hint = '松开暂停，Enter 完成' if self.multi_angle else '松开完成一笔'
            self.area.header_text_set('左键涂抹；' + finish_hint + ' | 中键旋转；Shift+中键平移；滚轮缩放 | Esc / 右键取消')
            context.window.cursor_modal_set('PAINT_BRUSH')
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            self.cleanup(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def draw_cursor(self):
        try:
            if bpy.context.area != self.area or bpy.context.region != self.region:
                return
            self.overlay.draw(bpy.context)
            if self.center is not None:
                self.draw_cursor_ring()
        except Exception as exc:
            self._draw_error = str(exc)

    def draw_cursor_ring(self):
        import gpu
        from gpu_extras.batch import batch_for_shader
        from mathutils import Matrix
        if self._cursor_shader is None:
            positions = []
            for plane in range(1 if self.surface else 3):
                for i in range(64):
                    for j in (i, i + 1):
                        a = j * math.tau / 64
                        co = [0.0, 0.0, 0.0]
                        co[plane], co[(plane + 1) % 3] = math.cos(a), math.sin(a)
                        positions.append(co)
            self._cursor_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            self._cursor_batch = batch_for_shader(self._cursor_shader, 'LINES', {'pos': positions})
        blend, depth, mask = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            rotation = self.rv3d.view_rotation.to_matrix().to_4x4() if self.surface else Matrix.Identity(4)
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(Matrix.Translation(self.center) @ rotation @ Matrix.Scale(self.stroke.radius, 4))
                self._cursor_shader.bind()
                self._cursor_shader.uniform_float('color', (1.0, 0.35, 0.1, 0.8) if self.erase else (0.15, 0.8, 1.0, 0.8))
                self._cursor_batch.draw(self._cursor_shader)
        finally:
            gpu.state.blend_set(blend)
            gpu.state.depth_test_set(depth)
            gpu.state.depth_mask_set(mask)

    def locate(self, event):
        from bpy_extras import view3d_utils
        xy = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        if not (0 <= xy[0] < self.region.width and 0 <= xy[1] < self.region.height):
            return None
        direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, xy)
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, xy)
        if self.surface:
            # Orthographic origins can be extremely far away with a large clip_end.
            if not self.rv3d.is_perspective:
                origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, xy, clamp=0)
                origin += direction * ((self.cache.bounds_center - origin).dot(direction) - self.cache.ray_span)
            self.stroke.set_view(self.rv3d.view_matrix.inverted().translation,
                                 self.rv3d.view_rotation @ Vector((0, 0, -1)), self.rv3d.is_perspective)
        hit = self.cache.hit(origin, direction)
        if hit is not None:
            return hit + direction * self.depth * self.stroke.radius
        if not self.surface and self.dragging and self.center is not None:
            return view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, xy, self.center)
        return None

    def preview(self):
        if self.stroke.revision != self._preview_revision:
            self.overlay.mark_dirty()
            self._preview_revision = self.stroke.revision
        self._redraw_pending = True

    def restore(self):
        # Painting is an independent overlay until commit. Cancelling writes nothing.
        pass

    def cleanup(self, context):
        _active_brushes.discard(self)
        timer, handle = getattr(self, '_timer', None), getattr(self, '_handle', None)
        self._timer = self._handle = None
        if timer:
            try:
                self._wm.event_timer_remove(timer)
            except (ReferenceError, RuntimeError, ValueError):
                pass
        if handle:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except (ReferenceError, RuntimeError, ValueError):
                pass
        if getattr(self, 'overlay', None):
            self.overlay.dispose()
        if getattr(self, '_overlay_state', None):
            self._overlay_state.restore()
        self._cursor_shader = self._cursor_batch = None
        try:
            self.area.header_text_set(None)
            self._window.cursor_modal_restore()
            self.area.tag_redraw()
        except (ReferenceError, RuntimeError):
            pass

    def pause_drawing(self):
        self.dragging = False
        self.stroke.break_segment()
        self.center = None
        self.area.tag_redraw()

    def finish_drawing(self, context):
        count = self.stroke.affected_count()
        if count:
            commit_stroke(context, self.stroke, self.animated, self.erase)
        self.cleanup(context)
        if not count:
            self.report({'WARNING'}, '笔刷范围内没有可绘制顶点，请增大半径或调整视角')
            return {'CANCELLED'}
        self.report({'INFO'}, ('时间路径已记录，拖动时间轴预览' if self.animated else '笔触已完成') + '；Ctrl+Z 撤销')
        return {'FINISHED'}

    def modal(self, context, event):
        try:
            if getattr(self, '_cancelled', False):
                return {'CANCELLED'}
            if getattr(self, '_draw_error', None):
                raise RuntimeError('绘制覆盖层无法显示：' + self._draw_error)
            if event.type == 'TIMER':
                # Blender 4.5 Event has no timer identity property. Limit refresh
                # by time so other modal tools' TIMER events cannot flood redraws.
                now = time.monotonic()
                if now >= getattr(self, '_next_redraw', 0.0) and (self._redraw_pending or self.overlay.needs_redraw):
                    self.area.tag_redraw()
                    self._redraw_pending = False
                    self._next_redraw = now + 1.0 / 30
                return {'PASS_THROUGH'}
            # Let Blender's native navigation operator own the entire gesture.
            # In particular, do not turn navigation mouse moves into paint samples.
            navigation = event.type in {
                'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                'WHEELLEFTMOUSE', 'WHEELRIGHTMOUSE',
                'WHEELINMOUSE', 'WHEELOUTMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
                'MOUSEROTATE', 'NUMPAD_0', 'NUMPAD_1', 'NUMPAD_2', 'NUMPAD_3',
                'NUMPAD_4', 'NUMPAD_5', 'NUMPAD_6', 'NUMPAD_7', 'NUMPAD_8',
                'NUMPAD_9', 'NUMPAD_PERIOD', 'NUMPAD_PLUS', 'NUMPAD_MINUS',
            } or event.type.startswith('NDOF_')
            emulate_middle = (event.type == 'LEFTMOUSE' and event.alt
                              and context.preferences.inputs.use_mouse_emulate_3_button)
            if navigation or emulate_middle:
                self.pause_drawing()
                return {'PASS_THROUGH'}
            if event.type == 'TIMER1':
                return {'PASS_THROUGH'}  # Blender's animated view transitions.
            if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
                self.cleanup(context)
                return {'CANCELLED'}
            if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
                return self.finish_drawing(context)
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self.dragging:
                end = self.locate(event)
                if end is not None:
                    self.stroke.sample(end)
                self.preview()
                self.pause_drawing()
                if self.multi_angle:
                    return {'RUNNING_MODAL'}
                return self.finish_drawing(context)
            if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE'}:
                self.center = self.locate(event)
                if self.surface and self.center is None:
                    self.stroke.break_segment()
                if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                    self.dragging = self.center is not None
                if self.dragging and self.center is not None:
                    self.stroke.sample(self.center)
                    self.preview()
                self._redraw_pending = True
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self.cleanup(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def remove_stroke_nodes(tree, group):
    # RNA wrappers are pointers into Blender-owned memory. A snapshot of *all*
    # nodes still contains union nodes that we delete below; even reading their
    # .type later can crash Blender rather than raise a Python exception.
    # Keep identifiers only, and resolve them again after each mutation.
    group_names = tuple(n.name for n in tree.nodes
                        if n.type == 'GROUP' and n.node_tree == group)
    for group_name in group_names:
        node = tree.nodes.get(group_name)
        if node is None:
            continue
        union_names = tuple(dict.fromkeys(
            link.to_node.name for link in node.outputs[0].links
            if link.to_node.type == 'MATH'
            and link.to_node.operation == 'MAXIMUM'
            and link.to_socket == link.to_node.inputs[1]
            and link.to_node.inputs[0].is_linked
            and link.to_node.inputs[0].links[0].from_node != node))
        for union_name in union_names:
            union = tree.nodes.get(union_name)
            if union is None:
                continue
            previous = union.inputs[0].links[0].from_socket
            # Creating a replacement link can free the old link immediately.
            destinations = tuple((link.to_node.name,
                                  list(link.to_node.inputs).index(link.to_socket))
                                 for link in union.outputs[0].links)
            for destination_name, socket_index in destinations:
                destination = tree.nodes.get(destination_name)
                if destination is not None and destination != union:
                    tree.links.new(previous, destination.inputs[socket_index])
            tree.nodes.remove(union)
        # If the user wired this mask somewhere else, its disconnected value
        # should be zero rather than a socket's unrelated default value.
        for link in node.outputs[0].links:
            socket = link.to_socket
            if socket.type == 'VALUE':
                socket.default_value = 0.0
            elif socket.type == 'RGBA':
                socket.default_value = (0.0, 0.0, 0.0, 1.0)
            elif socket.type == 'VECTOR':
                socket.default_value = (0.0, 0.0, 0.0)
        tree.nodes.remove(node)


class PETRIFY_OT_remove_stroke(bpy.types.Operator):
    bl_idname = 'petrify.remove_stroke'
    bl_label = '删除这条时间路径'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    @full_materials_edit
    def execute(self, context):
        s = context.scene.petrify_settings
        if self.index < 0 or self.index >= len(s.strokes):
            return {'CANCELLED'}
        group = s.strokes[self.index].group
        if group:
            attribute_names = stroke_attributes(group)
            materials, objects = stroke_users(group)
            meshes = {topology.data_mesh(ob) for ob in objects}
            for mat in materials:
                if mat.use_nodes and mat.node_tree:
                    remove_stroke_nodes(mat.node_tree, group)
            for mesh in meshes:
                for name in attribute_names:
                    if name and (attr := mesh.attributes.get(name)):
                        mesh.attributes.remove(attr)
            # A path may also be listed in another scene; remove stale entries
            # by stable group identity before freeing its datablock.
            for scene in bpy.data.scenes:
                if not hasattr(scene, 'petrify_settings'): continue
                items = scene.petrify_settings.strokes
                for i in reversed(range(len(items))):
                    if items[i].group == group: items.remove(i)
            bpy.data.node_groups.remove(group)
            for ob in objects:
                topology.sync_object(ob)
                if ice.companion(ob): ice.refresh(ob, sys.modules[__name__])
        else:
            s.strokes.remove(self.index)
        s.stroke_index = min(s.stroke_index, max(0, len(s.strokes) - 1))
        sync_path_visibility(None, context)
        return {'FINISHED'}


def configure_canvas(context, ob):
    lightweight.exit()
    image = mask_for(ob)
    if not image:
        raise RuntimeError('请先初始化石化')
    protect_source_images([ob])
    activate_object(context, ob)
    ob.data.uv_layers.active = ob.data.uv_layers[UV_NAME]
    old_uv = ob.data.uv_layers.get(ob.get('petrify_original_uv', ''))
    if old_uv:
        old_uv.active_render = True
    for mat in ob.data.materials:
        if mat and mat.use_nodes and (n := mat.node_tree.nodes.get(MASK_NODE)):
            mat.node_tree.nodes.active = n
    paint = context.scene.tool_settings.image_paint
    paint.mode = 'IMAGE'
    paint.canvas = image
    paint.seam_bleed = 8
    paint.use_occlude = True
    paint.use_backface_culling = True
    paint.use_normal_falloff = True
    ob.data.use_paint_mask = False
    ob.data.use_mirror_x = False
    ob.data.use_mirror_y = False
    ob.data.use_mirror_z = False
    bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
    for area in context.screen.areas if context.screen else []:
        if area.type == 'VIEW_3D':
            area.spaces.active.shading.type = 'MATERIAL'
            area.spaces.active.overlay.show_overlays = False
            area.spaces.active.show_region_ui = True
        elif area.type == 'IMAGE_EDITOR':
            area.spaces.active.image = image
    return paint


def set_brush(context, erase=False):
    bpy.ops.brush.asset_activate(asset_library_type='ESSENTIALS',
        relative_asset_identifier='brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Soft')
    paint = context.scene.tool_settings.image_paint
    brush = paint.brush
    if not brush:
        raise RuntimeError('请在内置笔刷资产中选择 Paint Soft')
    brush.blend = 'MIX'
    brush.use_pressure_strength = False
    brush.use_pressure_size = False
    context.scene.petrify_settings.is_erasing = erase
    unified = context.scene.tool_settings.unified_paint_settings
    unified.use_unified_color = True
    unified.color = (0,0,0) if erase else (1,1,1)
    unified.secondary_color = (1,1,1) if erase else (0,0,0)
    unified.use_unified_size = True
    unified.use_unified_strength = True
    if unified.size < 2:
        unified.size = 65
    for area in context.screen.areas if context.screen else []:
        if area.type == 'VIEW_3D':
            with context.temp_override(area=area):
                bpy.ops.wm.tool_set_by_id(name='builtin.brush')
    return brush


class PETRIFY_OT_paint(bpy.types.Operator):
    bl_idname = 'petrify.paint'
    bl_label = '开始石化绘制'
    erase: BoolProperty(default=False)

    def execute(self, context):
        meshes = target_meshes(context)
        ob = context.active_object if context.active_object in meshes else (meshes[0] if meshes else None)
        try:
            configure_canvas(context, ob)
            set_brush(context, self.erase)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, '左键拖动：' + ('恢复原材质' if self.erase else '涂抹石化') + '；中键旋转；F 调大小')
        return {'FINISHED'}


class PETRIFY_OT_finish(bpy.types.Operator):
    bl_idname = 'petrify.finish'
    bl_label = '结束绘制'

    def execute(self, context):
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                a.spaces.active.overlay.show_overlays = True
        return {'FINISHED'}


@persistent
def pack_masks_before_save(_):
    protect_source_images(ob for ob in bpy.data.objects if ob.get('petrify_ready'))
    for image in bpy.data.images:
        if image.get('petrify_mask') and (image.is_dirty or not image.packed_file):
            image.pack()


class PETRIFY_OT_save(bpy.types.Operator):
    bl_idname = 'petrify.save'
    bl_label = '保存工程和石化蒙版'

    def execute(self, context):
        pack_masks_before_save(None)
        if bpy.data.filepath:
            bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
        else:
            bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')
        self.report({'INFO'}, '石化蒙版已打包进工程')
        return {'FINISHED'}


def legacy_paint_active(context):
    if context.mode != 'PAINT_TEXTURE':
        return False
    image = mask_for(context.active_object)
    return image is not None and context.scene.tool_settings.image_paint.canvas == image


class PETRIFY_PT_panel(bpy.types.Panel):
    bl_label = DISPLAY_NAME
    bl_idname = 'PETRIFY_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ASFR'

    def draw(self, context):
        layout = self.layout
        s = context.scene.petrify_settings
        meshes = target_meshes(context)
        layout.prop(s, 'target')
        layout.operator('petrify.select_target', icon='RESTRICT_SELECT_OFF')
        layout.prop(s, 'include_children')
        layout.label(text='本次范围：%d 个可见网格' % len(meshes), icon='OUTLINER_OB_MESH')
        # Keep an exit reachable even if Advanced Tools is collapsed, including
        # pre-volume legacy objects which return early from this main panel.
        if legacy_paint_active(context):
            notice = layout.box()
            notice.label(text='旧蒙版绘制中，不记录时间路径', icon='INFO')
            notice.operator('petrify.finish', text='结束旧蒙版绘制')
        if any(topology.needs_store(ob, DEFORM_MODIFIERS) for ob in meshes):
            compat = layout.box()
            compat.label(text='修改器兼容：直接绘制最终网格', icon='MODIFIER')
            compat.label(text='镜像 / 阵列各部分可独立绘制')
            compat.label(text='绘制后保持拓扑与细分级别不变')
            if not any(topology.store_for(ob) for ob in meshes):
                compat.operator('petrify.match_subdivision', icon='FILE_REFRESH')
            for ob in meshes:
                issue = topology.settings_issue(ob, context.scene, DEFORM_MODIFIERS)
                if issue:
                    compat.label(text=ob.name, icon='ERROR')
                    compat.label(text=issue)
        if not meshes or any(ob.get('petrify_version', 0) < 2 for ob in meshes):
            layout.prop(s, 'resolution')
            layout.operator('petrify.prepare', icon='SHADING_TEXTURE')
            layout.label(text='可选择网格、骨骼或角色根节点')
            return
        performance = layout.box()
        active = lightweight.is_active()
        performance.operator('petrify.light_preview',
                             text='轻量预览中 · 切回完整材质' if active else '开启轻量预览',
                             icon='SHADING_SOLID' if active else 'SHADING_RENDERED', depress=active)
        performance.label(text='简化角色和效果外观，便于绘制和拖动时间轴')
        if active:
            performance.label(text='保存和正式渲染会自动恢复完整材质')
            performance.label(text='细节与材质动画请切回完整模式查看')
        box = layout.box()
        box.prop(s, 'brush_method', expand=True)
        box.label(text='只画可见外层；转动视角画背面' if s.brush_method == 'SURFACE' else '球形范围内一起生效，包括内部')
        box.prop(s, 'radius')
        if s.brush_method == 'SPHERE':
            box.prop(s, 'depth')
        box.prop(s, 'hardness', slider=True)
        box.prop(s, 'strength', slider=True)
        box.prop(s, 'draw_mode', expand=True)
        box.prop(s, 'multi_angle')
        if s.draw_mode == 'TIMELINE':
            box.column(align=True).prop(s, 'timing_mode', expand=True)
            row = box.row(align=True)
            row.prop(s, 'start_frame', text='从')
            row.prop(s, 'end_frame', text='到')
            if s.timing_mode != 'SIMULTANEOUS':
                box.prop(s, 'fade_frames')
                if s.timing_mode == 'PARALLEL':
                    box.label(text='松开左键分段；各段同时推进')
            else:
                box.label(text='所有区域一起渐变，一起完成')
        row = box.row()
        row.scale_y = 1.5
        row.operator('petrify.sphere', text='画一条时间路径' if s.draw_mode == 'TIMELINE' else '开始涂抹', icon='BRUSH_DATA').erase = False
        box.prop(s, 'erase_scope', expand=True)
        box.operator('petrify.sphere', text='擦除勾选路径（所有帧）' if s.erase_scope == 'SELECTED' else '擦除全部涂抹（所有帧）', icon='LOOP_BACK').erase = True
        box.label(text='左键涂抹；松开暂停；Enter 完成' if s.multi_angle else '左键拖动；松开完成一笔')
        box.label(text='中键旋转；Shift+中键平移')
        box.label(text='滚轮缩放；Esc 取消本次')
        box.label(text='完成后 Ctrl+Z 撤销')
        box.label(text='绘制中显示灰色范围；橙色表示擦除范围')
        box.label(text='完成本次绘制后显示当前材质')
        available = {g.name for ob in meshes for g in stroke_groups(ob)}
        layout.prop(s, 'show_paths', icon='TRIA_DOWN' if s.show_paths else 'TRIA_RIGHT', emboss=False)
        if s.show_paths:
            tools = layout.row(align=True)
            tools.prop(s, 'path_shift', text='偏移帧')
            tools.operator('petrify.shift_paths', text='平移勾选')
            layout.label(text='独显仅筛选时间路径，静态范围仍保留')
            found = False
            for index, item in enumerate(s.strokes):
                if not item.group or item.group.name not in available:
                    continue
                found = True
                box = layout.box()
                row = box.row()
                row.prop(item, 'selected', text='')
                row.prop(item, 'name', text='')
                controls = box.row(align=True)
                controls.prop(item, 'enabled')
                controls.prop(item, 'solo')
                row.operator('petrify.remove_stroke', text='', icon='X').index = index
                box.prop(item, 'timing_mode', text='')
                box.label(text='表面涂抹' if item.group.get('brush_method') == 'SURFACE' else '球形穿透')
                row = box.row(align=True)
                row.prop(item, 'start_frame', text='从')
                row.prop(item, 'end_frame', text='到')
                if item.timing_mode != 'SIMULTANEOUS':
                    box.prop(item, 'fade_frames')
                if not item.group.get('segment_attr'):
                    box.label(text='旧路径未记录分段，新模式需重画')
            if not found:
                layout.label(text='尚未记录路径')
        layout.prop(s, 'show_look', icon='TRIA_DOWN' if s.show_look else 'TRIA_RIGHT', emboss=False)
        if s.show_look:
            box = layout.box()
            box.prop(s, 'stone_preset', text='')
            box.operator('petrify.apply_preset', icon='MATERIAL')
            box.operator('petrify.reset_look', icon='LOOP_BACK')
            stone = stone_for(meshes[0])
            applied = stone.get('petrify_preset', 'DEFAULT') if stone else 'DEFAULT'
            box.label(text='当前：' + preset_label(applied, stone))
            if applied not in {item[0] for item in _preset_items}:
                box.label(text='工程内材质仍保留，当前列表未提供', icon='INFO')
            if applied == 'DEFAULT':
                for name in ('color', 'roughness', 'bump', 'scale'):
                    box.prop(s, name)
            elif applied == gold.KEY:
                gold.draw(box, s)
            elif applied == ice.KEY:
                ice.draw(box, s)
            elif applied == resin.KEY:
                box.prop(s, 'resin_color_mode', text='')
                row = box.row(); row.enabled = s.resin_color_mode != 'ORIGINAL'
                row.prop(s, 'resin_color', text='叠加颜色' if s.resin_color_mode == 'MIX' else '树脂颜色')
                if s.resin_color_mode == 'MIX':
                    box.prop(s, 'resin_blend', slider=True)
                    box.label(text='0% 模型原色 / 100% 自选颜色')
                for name in ('resin_roughness', 'resin_specular', 'resin_coat', 'resin_coat_roughness'):
                    box.prop(s, name)
                box.label(text='高光依赖灯光；轻量预览不显示树脂光泽')
                if any(mat and mat.get('petrify_resin_color_fallback') for ob in meshes for mat in ob.data.materials):
                    box.label(text='部分复杂材质使用显示颜色', icon='INFO')
            else:
                box.label(text='保留源材质参数和贴图')
            normal_box = box.box()
            normal_box.label(text='人物细节 + 材质纹理', icon='NORMALS_VERTEX')
            normal_box.prop(s, 'normal_original_strength', slider=True)
            normal_box.prop(s, 'normal_detail_strength', slider=True)
            normal_box.operator('petrify.refresh_normals', icon='FILE_REFRESH')
            statuses = [m.get('petrify_normal_status', 'LEGACY') for ob in meshes for m in ob.data.materials if m]
            if 'LEGACY' in statuses:
                normal_box.label(text='旧工程：点击上方按钮启用', icon='INFO')
            if 'DETAIL' in statuses:
                normal_box.label(text='已识别人物法线 / 凹凸')
            if 'GEOMETRY' in statuses:
                normal_box.label(text='无原细节的材质使用表面法线')
            if any(x in {'AMBIGUOUS', 'UNSUPPORTED'} for x in statuses):
                normal_box.label(text='部分复杂材质无法唯一识别', icon='ERROR')
                normal_box.label(text='已回退表面法线，不猜测贴图')
            if active:
                normal_box.label(text='轻量预览不显示法线细节')
            box.prop(s, 'amount')
        layout.operator('petrify.save', icon='FILE_TICK')
        layout.label(text='两种方式的精度都取决于网格密度')


class PETRIFY_PT_advanced(bpy.types.Panel):
    bl_label = '高级工具'
    bl_idname = 'PETRIFY_PT_advanced'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ASFR'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 100

    def draw(self, context):
        self.layout.label(text='兼容与维护，日常绘制无需使用', icon='INFO')


class PETRIFY_PT_legacy_surface(bpy.types.Panel):
    bl_label = '旧版工程维护'
    bl_idname = 'PETRIFY_PT_legacy_surface'
    bl_parent_id = 'PETRIFY_PT_advanced'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ASFR'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.label(text='仅修补早期工程的贴图蒙版')
        layout.label(text='不记录或擦除新版时间路径')
        layout.label(text='新绘制请用主面板的「使用方法」')
        meshes = target_meshes(context)
        ob = context.active_object if context.active_object in meshes else (meshes[0] if meshes else None)
        if not mask_for(ob):
            layout.label(text='请先选择带有石化蒙版的目标', icon='INFO')
            return
        # Do not require version 2: old texture-only projects must remain editable
        # without reinitialization, pixel scans, or rebuilding material nodes.
        layout.label(text='维护目标：' + ob.name)
        layout.operator('petrify.paint', text='修补旧贴图蒙版').erase = False
        layout.operator('petrify.paint', text='擦除旧贴图蒙版').erase = True
        if legacy_paint_active(context):
            unified = context.scene.tool_settings.unified_paint_settings
            layout.prop(unified, 'size', text='笔刷大小')
            layout.prop(unified, 'strength', text='笔刷强度')
            layout.operator('petrify.finish', text='结束旧蒙版绘制')


class PETRIFY_PT_preset_library(bpy.types.Panel):
    bl_label = '预设管理 · 添加 / 删除'
    bl_idname = 'PETRIFY_PT_preset_library'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ASFR'

    def draw(self, context):
        layout = self.layout
        s = context.scene.petrify_settings
        self.layout.prop(s, 'show_scene_presets')
        box = layout.box()
        box.label(text='管理已有预设')
        box.prop(s, 'stone_preset', text='')
        box.operator('petrify.delete_preset', icon='TRASH')
        if s.stone_preset in PRESET_GROUPS:
            box.label(text='内置预设受保护', icon='LOCKED')
        else:
            box.label(text='删除不改变模型现有效果')
        box.operator('petrify.restore_preset', icon='LOOP_BACK')
        box = layout.box()
        box.label(text='添加新预设', icon='ADD')
        box.prop(s, 'preset_source_mode', text='')
        if s.preset_source_mode == 'GROUP':
            box.prop(s, 'preset_source_group', text='节点组')
            if len(shader_outputs(s.preset_source_group)) > 1:
                box.prop(s, 'preset_output')
            box.label(text='保存组本身设定的初始参数')
            box.label(text='要保存节点上调整后的参数：')
            box.label(text='着色器编辑器 → 选中组节点')
            box.label(text='N → 石化 → 将选中组存为预设')
        else:
            box.label(text='保存当前目标的材质外观')
        box.prop(s, 'preset_new_name', text='名称')
        box.operator('petrify.save_custom_preset', text='添加为新预设', icon='FILE_TICK')
        box.label(text='永久保存；贴图一起打包')
        row = layout.row(align=True)
        row.operator('petrify.refresh_presets', text='刷新', icon='FILE_REFRESH')
        row.operator('petrify.open_preset_directory', text='预设目录', icon='FILE_FOLDER')
        layout.operator('petrify.cleanup_stone_groups', icon='BRUSH_DATA')
        if _preset_library_errors:
            layout.label(text='部分预设未载入，点击刷新查看', icon='ERROR')


class PETRIFY_PT_shader_preset(bpy.types.Panel):
    bl_label = '材质预设'
    bl_idname = 'PETRIFY_PT_shader_preset'
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'ASFR'

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'ShaderNodeTree'

    def draw(self, context):
        self.layout.operator('petrify.save_selected_preset', icon='FILE_TICK')
        self.layout.label(text='选择已做好的着色器组节点')
        self.layout.label(text='保留选中节点的输入数值')


CLASSES = (PETRIFY_OT_reset_look, PETRIFY_OT_shift_paths, PETRIFY_OT_ice_direction_axis, PETRIFY_OT_reseed_ice, PETRIFY_Stroke, PETRIFY_Settings, PETRIFY_OT_select_target, PETRIFY_OT_refresh_normals, PETRIFY_OT_apply_preset,
           PETRIFY_OT_delete_preset, PETRIFY_OT_restore_preset, PETRIFY_OT_cleanup_stone_groups,
           PETRIFY_OT_save_custom_preset, PETRIFY_OT_save_selected_preset, PETRIFY_OT_refresh_presets, PETRIFY_OT_open_preset_directory,
           PETRIFY_OT_prepare, PETRIFY_OT_light_preview, PETRIFY_OT_match_subdivision, PETRIFY_OT_sphere, PETRIFY_OT_remove_stroke,
           PETRIFY_OT_paint, PETRIFY_OT_finish, PETRIFY_OT_save, PETRIFY_PT_panel, PETRIFY_PT_preset_library, PETRIFY_PT_shader_preset, PETRIFY_PT_advanced, PETRIFY_PT_legacy_surface)

SOURCE_HANDLERS = (
    (bpy.app.handlers.load_pre, cancel_active_brushes),
    (bpy.app.handlers.undo_pre, cancel_active_brushes),
    (bpy.app.handlers.redo_pre, cancel_active_brushes),
    (bpy.app.handlers.undo_pre, remember_source_paths),
    (bpy.app.handlers.redo_pre, remember_source_paths),
    (bpy.app.handlers.undo_post, restore_source_images_after_undo),
    (bpy.app.handlers.redo_post, restore_source_images_after_undo),
    (bpy.app.handlers.load_pre, clear_source_paths),
    (bpy.app.handlers.load_post, remember_source_paths),
    (bpy.app.handlers.load_post, refresh_presets_after_load),
    (bpy.app.handlers.undo_post, refresh_presets_after_load),
    (bpy.app.handlers.redo_post, refresh_presets_after_load),
)


def register():
    refresh_presets()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.petrify_settings = PointerProperty(type=PETRIFY_Settings)
    lightweight.register(sys.modules[__name__])
    normals.register()
    # Existing scene ID properties become accessible only after registration.
    for scene in getattr(bpy.data, 'scenes', ()):
        migrate_preset_selection(scene.petrify_settings, _preset_items)
    if pack_masks_before_save not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(pack_masks_before_save)
    for handlers, callback in SOURCE_HANDLERS:
        if callback not in handlers:
            handlers.append(callback)
    # add-on enable runs under Blender's _RestrictData context.
    if not hasattr(bpy.data, 'node_groups') and not bpy.app.timers.is_registered(refresh_presets_deferred):
        bpy.app.timers.register(refresh_presets_deferred, first_interval=0.1)


def unregister():
    cancel_active_brushes()
    lightweight.unregister()
    normals.unregister()
    if bpy.app.timers.is_registered(refresh_presets_deferred):
        bpy.app.timers.unregister(refresh_presets_deferred)
    for handlers, callback in SOURCE_HANDLERS:
        if callback in handlers:
            handlers.remove(callback)
    _source_paths.clear()
    if pack_masks_before_save in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(pack_masks_before_save)
    if hasattr(bpy.types.Scene, 'petrify_settings'):
        del bpy.types.Scene.petrify_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
